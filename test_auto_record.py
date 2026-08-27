"""Unit tests for hourly / 1-minute auto-record, remux rules, and the worker."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from auto_record import (
    AutoRecordManager,
    RecordSupervisor,
    _ChunkWriter,
    _chunk_raw_path,
    _chunk_stamp,
    camera_folder,
    chunk_mode,
    ensure_worker,
    is_current_slot,
    is_worker_alive,
    logon_task_command,
    read_worker_status,
    remux_orphans,
    sanitize_folder,
    should_leave_raw,
    write_worker_status,
)
from camera_store import Camera, CameraStore
from db import connect


def _cam(
    cam_id: int = 1,
    name: str = "Lab",
    device_id: str = "101",
    auto_record: bool = True,
    ip: str = "192.168.1.7",
    record_chunk: str = "hour",
) -> Camera:
    return Camera(
        id=cam_id,
        name=name,
        device_id=device_id,
        mac="",
        ip=ip,
        port=8800,
        username="u",
        password="p",
        source="lan",
        quality=1,
        auto_record=auto_record,
        record_chunk=record_chunk,
        created_at="",
        updated_at="",
    )


def _fake_remux(path: Path, fmt: str) -> Path:
    mp4 = path.with_suffix(".mp4")
    if path.exists():
        path.replace(mp4)
    return mp4


class SanitizeTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(sanitize_folder("Lab", "1"), "Lab")

    def test_unsafe_windows_chars(self):
        self.assertEqual(sanitize_folder('Lab / A<>:"|?*', "1"), "Lab _ A")

    def test_reserved_and_empty(self):
        self.assertEqual(sanitize_folder("CON", "99"), "cam_99")
        self.assertEqual(sanitize_folder("   ", "99"), "cam_99")
        self.assertEqual(sanitize_folder("...", "99"), "cam_99")
        self.assertEqual(sanitize_folder("COM1", "99"), "cam_99")

    def test_trailing_dots_spaces(self):
        self.assertEqual(sanitize_folder("Lab. ", "1"), "Lab")

    def test_long_name_trimmed(self):
        self.assertEqual(len(sanitize_folder("x" * 200, "1")), 80)


class ChunkPathTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 8, 17, 14, 37, 5)

    def test_hourly_stamp(self):
        day, hour, minute, until = _chunk_stamp(self.now, "hour")
        self.assertEqual((day, hour, minute), ("2026-08-17", "14", None))
        self.assertEqual(until, datetime(2026, 8, 17, 15, 0, 0))

    def test_minute_stamp(self):
        day, hour, minute, until = _chunk_stamp(self.now, "minute")
        self.assertEqual((day, hour, minute), ("2026-08-17", "14", "37"))
        self.assertEqual(until, datetime(2026, 8, 17, 14, 38, 0))

    def test_hourly_path_is_date_hour(self):
        p = _chunk_raw_path(self.td / "Lab", "2026-08-17", "14", None, "h264")
        self.assertEqual(p, self.td / "Lab" / "2026-08-17" / "14-00-00.h264")

    def test_minute_path_is_date_hour_minute(self):
        p = _chunk_raw_path(self.td / "Lab", "2026-08-17", "14", "37", "h264")
        self.assertEqual(p, self.td / "Lab" / "2026-08-17" / "14" / "14-37-00.h264")

    def test_existing_mp4_does_not_rename_slot(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        (day / "14-00-00.mp4").write_bytes(b"old")
        p = _chunk_raw_path(self.td / "Lab", "2026-08-17", "14", None, "h264")
        self.assertEqual(p.name, "14-00-00.h264")
        self.assertFalse(any(day.glob("14-37*")))

    def test_hevc_extension(self):
        p = _chunk_raw_path(self.td / "Lab", "2026-08-17", "08", None, "hevc")
        self.assertEqual(p.name, "08-00-00.h265")

    def test_midnight_hour_stamp(self):
        now = datetime(2026, 8, 17, 23, 59, 1)
        day, hour, minute, until = _chunk_stamp(now, "hour")
        self.assertEqual((day, hour, minute), ("2026-08-17", "23", None))
        self.assertEqual(until, datetime(2026, 8, 18, 0, 0, 0))
        day2, hour2, _, _ = _chunk_stamp(until, "hour")
        self.assertEqual((day2, hour2), ("2026-08-18", "00"))

    def test_chunk_mode_normalizes(self):
        self.assertEqual(chunk_mode("minute"), "minute")
        self.assertEqual(chunk_mode("hour"), "hour")
        self.assertEqual(chunk_mode("nope"), "hour")


class CurrentSlotTests(unittest.TestCase):
    def test_hourly_current_and_past(self):
        now = datetime(2026, 8, 17, 14, 10)
        cur = Path("Lab") / "2026-08-17" / "14-00-00.h264"
        past = Path("Lab") / "2026-08-17" / "13-00-00.h264"
        self.assertTrue(is_current_slot(cur, now))
        self.assertFalse(is_current_slot(past, now))

    def test_minute_current_and_past(self):
        now = datetime(2026, 8, 17, 14, 7, 30)
        cur = Path("Lab") / "2026-08-17" / "14" / "14-07-00.h264"
        past = Path("Lab") / "2026-08-17" / "14" / "14-06-00.h264"
        self.assertTrue(is_current_slot(cur, now))
        self.assertFalse(is_current_slot(past, now))

    def test_manual_rec_file_is_left_raw(self):
        path = Path("recordings") / "rec_20260817_140000.h264"
        self.assertTrue(should_leave_raw(path, datetime(2026, 8, 17, 14, 1)))

    def test_legacy_split_name_is_not_current(self):
        now = datetime(2026, 8, 17, 14, 37)
        self.assertFalse(is_current_slot(Path("Lab") / "2026-08-17" / "14-37.h264", now))


class CameraFolderTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_creates_named_folder_with_marker(self):
        path = camera_folder(_cam(), self.td)
        self.assertEqual(path.name, "Lab")
        self.assertEqual((path / ".device_id").read_text(encoding="utf-8"), "101")

    def test_same_name_second_camera_gets_suffix(self):
        camera_folder(_cam(1, "Lab", "101"), self.td)
        path = camera_folder(_cam(2, "Lab", "202"), self.td)
        self.assertEqual(path.name, "Lab_202")

    def test_rename_moves_existing_folder(self):
        first = camera_folder(_cam(1, "Lab", "101"), self.td)
        (first / "keep.txt").write_text("x", encoding="utf-8")
        moved = camera_folder(_cam(1, "Office", "101"), self.td)
        self.assertEqual(moved.name, "Office")
        self.assertTrue((moved / "keep.txt").exists())

    def test_rename_keeps_old_if_new_name_taken(self):
        camera_folder(_cam(1, "Lab", "101"), self.td)
        camera_folder(_cam(2, "Office", "202"), self.td)
        path = camera_folder(_cam(1, "Office", "101"), self.td)
        self.assertEqual(path.name, "Office_101")


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.cam = _cam()
        self.w = _ChunkWriter(self.cam, self.td, remux=_fake_remux)

    def test_first_hour_file_and_empty_delete(self):
        now = datetime(2026, 8, 17, 14, 5, 0)
        self.w.feed(now, "h264", True, b"")
        self.assertIsNone(self.w._raw)
        self.w.feed(now, "h264", True, b"\x00\x00\x00\x01I")
        raw = self.td / "Lab" / "2026-08-17" / "14-00-00.h264"
        self.assertTrue(raw.exists())
        self.w._bytes = 0
        raw.write_bytes(b"")
        self.assertIsNone(self.w.close())
        self.assertFalse(raw.exists())

    def test_current_stays_h264_until_close(self):
        now = datetime(2026, 8, 17, 14, 5, 0)
        self.w.feed(now, "h264", True, b"I")
        day = self.td / "Lab" / "2026-08-17"
        self.assertTrue((day / "14-00-00.h264").exists())
        self.assertFalse((day / "14-00-00.mp4").exists())
        out = self.w.close()
        self.assertEqual(out.name, "14-00-00.mp4")
        self.assertTrue(out.exists())
        self.assertFalse((day / "14-00-00.h264").exists())

    def test_restart_appends_same_hour_file(self):
        now = datetime(2026, 8, 17, 14, 10, 0)
        self.w.feed(now, "h264", True, b"AAA")
        self.w.close()
        later = _ChunkWriter(self.cam, self.td, remux=_fake_remux)
        later.feed(now.replace(minute=37), "h264", True, b"BBB")
        later.close()
        day = self.td / "Lab" / "2026-08-17"
        # first close remuxed to mp4; second open in same hour writes 14.h264 again
        # then close remuxes (replaces) 14.mp4 — after a *finished* hour that is correct.
        # Mid-hour restart without remux:
        raw = day / "14-00-00.h264"
        w1 = _ChunkWriter(self.cam, self.td, remux=_fake_remux)
        w1.feed(now, "h264", True, b"AAA")
        w1._fh.close()
        w1._fh = None
        _mark = __import__("auto_record", fromlist=["_mark_open"])._mark_open
        _mark(w1._raw, False)
        w2 = _ChunkWriter(self.cam, self.td, remux=_fake_remux)
        w2.feed(now.replace(minute=37), "h264", True, b"BBB")
        out = w2.close()
        self.assertEqual(out.name, "14-00-00.mp4")
        self.assertEqual(out.read_bytes(), b"AAABBB")
        self.assertEqual(len(list(day.glob("14-00-00*"))), 1)
        self.assertFalse(any(day.glob("14-37*")))

    def test_hour_rollover_on_iframe_only(self):
        start = datetime(2026, 8, 17, 14, 59, 50)
        later = datetime(2026, 8, 17, 15, 0, 0)
        frames = [
            (start, True, b"I14"),
            (start + timedelta(seconds=2), False, b"P14"),
            (later, False, b"Pcross"),
            (later + timedelta(seconds=1), True, b"I15"),
            (later + timedelta(seconds=2), False, b"P15"),
        ]
        for now, key, data in frames:
            self.w.feed(now, "h264", key, data)
        self.w.close()
        day = self.td / "Lab" / "2026-08-17"
        self.assertEqual((day / "14-00-00.mp4").read_bytes(), b"I14P14Pcross")
        self.assertEqual((day / "15-00-00.mp4").read_bytes(), b"I15P15")
        self.assertFalse(any(day.glob("14-37*")))

    def test_no_frame_dropped_across_boundary(self):
        start = datetime(2026, 8, 17, 14, 59, 50)
        payloads = [b"I0", b"P1", b"P2", b"I3", b"P4"]
        times = [
            start,
            start + timedelta(seconds=1),
            datetime(2026, 8, 17, 15, 0, 0),
            datetime(2026, 8, 17, 15, 0, 1),
            datetime(2026, 8, 17, 15, 0, 2),
        ]
        keys = [True, False, False, True, False]
        for now, key, data in zip(times, keys, payloads):
            self.w.feed(now, "h264", key, data)
        self.w.close()
        day = self.td / "Lab" / "2026-08-17"
        combined = (day / "14-00-00.mp4").read_bytes() + (day / "15-00-00.mp4").read_bytes()
        self.assertEqual(combined, b"".join(payloads))

    def test_day_rollover_new_date_folder(self):
        start = datetime(2026, 8, 17, 23, 59, 0)
        nxt = datetime(2026, 8, 18, 0, 0, 1)
        self.w.feed(start, "h264", True, b"late")
        self.w.feed(nxt, "h264", True, b"early")
        self.w.close()
        self.assertTrue((self.td / "Lab" / "2026-08-17" / "23-00-00.mp4").exists())
        self.assertTrue((self.td / "Lab" / "2026-08-18" / "00-00-00.mp4").exists())

    def test_minute_mode_tree(self):
        cam = _cam(record_chunk="minute")
        w = _ChunkWriter(cam, self.td, remux=_fake_remux)
        t0 = datetime(2026, 8, 17, 14, 7, 10)
        t1 = datetime(2026, 8, 17, 14, 8, 0)
        w.feed(t0, "h264", True, b"M7")
        w.feed(t0 + timedelta(seconds=20), "h264", False, b"P")
        w.feed(t1, "h264", False, b"Pcross")
        w.feed(t1 + timedelta(seconds=1), "h264", True, b"M8")
        w.close()
        hour = self.td / "Lab" / "2026-08-17" / "14"
        self.assertEqual((hour / "14-07-00.mp4").read_bytes(), b"M7PPcross")
        self.assertEqual((hour / "14-08-00.mp4").read_bytes(), b"M8")

    def test_hevc_uses_h265_raw(self):
        now = datetime(2026, 8, 17, 8, 0, 0)
        self.w.feed(now, "hevc", True, b"hevc")
        self.assertTrue((self.td / "Lab" / "2026-08-17" / "08-00-00.h265").exists())
        out = self.w.close()
        self.assertEqual(out.suffix, ".mp4")

    def test_start_waits_for_iframe(self):
        now = datetime(2026, 8, 17, 14, 0, 0)
        self.w.feed(now, "h264", False, b"Pskip")
        self.assertIsNone(self.w._raw)
        self.w.feed(now, "h264", True, b"I")
        self.assertEqual((self.td / "Lab" / "2026-08-17" / "14-00-00.h264").read_bytes(), b"I")
        self.w.close()

    def test_orphan_past_hour_becomes_mp4(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        raw = day / "11-00-00.h264"
        raw.write_bytes(b"leftover")
        with mock.patch("auto_record.remux_annexb", _fake_remux):
            self.assertEqual(remux_orphans(self.td, datetime(2026, 8, 17, 14, 0)), 1)
        self.assertTrue((day / "11-00-00.mp4").exists())
        self.assertFalse(raw.exists())

    def test_orphan_skips_current_hour(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        raw = day / "14-00-00.h264"
        raw.write_bytes(b"live")
        with mock.patch("auto_record.remux_annexb", _fake_remux):
            self.assertEqual(remux_orphans(self.td, datetime(2026, 8, 17, 14, 37)), 0)
        self.assertTrue(raw.exists())
        self.assertFalse((day / "14-00-00.mp4").exists())

    def test_orphan_skips_manual_rec_file(self):
        rec = self.td / "rec_20260817_140000.h264"
        rec.write_bytes(b"manual")
        with mock.patch("auto_record.remux_annexb", _fake_remux):
            self.assertEqual(remux_orphans(self.td, datetime(2026, 8, 17, 14, 1)), 0)
        self.assertTrue(rec.exists())


class StoreTests(unittest.TestCase):
    def test_upsert_and_toggle(self):
        td = Path(tempfile.mkdtemp())
        store = CameraStore(td)
        saved = store.upsert(_cam(0, auto_record=True, record_chunk="minute"))
        self.assertTrue(saved.auto_record)
        self.assertEqual(saved.record_chunk, "minute")
        store.set_auto_record(saved.id, False)
        self.assertFalse(store.get(saved.id).auto_record)
        again = store.upsert(_cam(0, auto_record=True, record_chunk="hour"))
        self.assertTrue(again.auto_record)
        self.assertEqual(again.record_chunk, "hour")
        store.close()

    def test_migrate_old_db_adds_columns(self):
        td = Path(tempfile.mkdtemp())
        db = td / "v380.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE cameras (
                id INTEGER PRIMARY KEY,
                name TEXT, device_id TEXT UNIQUE, mac TEXT, ip TEXT, port INTEGER,
                username TEXT, password_enc BLOB, source TEXT, quality INTEGER,
                created_at TEXT, updated_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO cameras VALUES (1,'Lab','101','','1.1.1.1',8800,'u',x'00','lan',1,'t','t')"
        )
        conn.commit()
        conn.close()
        conn = connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
        self.assertIn("auto_record", cols)
        self.assertIn("record_chunk", cols)
        row = conn.execute("SELECT auto_record, record_chunk FROM cameras WHERE id=1").fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "hour")
        conn.close()


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.video_codec = "h264"

    def connect(self):
        return None

    def close(self):
        return None

    def h264_for_decode(self, iframe: bytes) -> bytes:
        return iframe

    def iter_video_frames(self, stop_event=None):
        yield "video", True, b"\x00\x00\x00\x01I"
        yield "video", False, b"\x00\x00\x00\x01P"
        if stop_event is not None:
            stop_event.wait(3)


class ManagerTests(unittest.TestCase):
    def test_restart_does_not_kill_new_worker(self):
        td = Path(tempfile.mkdtemp())
        mgr = AutoRecordManager(td)
        cam = _cam(5, ip="192.168.1.7")
        with mock.patch("auto_record.V380SnapshotClient", _FakeClient), mock.patch(
            "auto_record.remux_annexb", _fake_remux
        ):
            mgr.sync([cam])
            time.sleep(0.15)
            cam.ip = "192.168.1.8"
            mgr.sync([cam])
            time.sleep(0.2)
            self.assertTrue(any(t.is_alive() for t in mgr._threads.values()))
            self.assertIn(5, mgr._threads)
            mgr.stop_all()
        day = datetime.now().strftime("%Y-%m-%d")
        files = list((td / "Lab" / day).glob("*.mp4")) + list((td / "Lab" / day).glob("*.h264"))
        self.assertTrue(files)

    def test_disable_stops_worker(self):
        td = Path(tempfile.mkdtemp())
        mgr = AutoRecordManager(td)
        cam = _cam(6)
        with mock.patch("auto_record.V380SnapshotClient", _FakeClient), mock.patch(
            "auto_record.remux_annexb", _fake_remux
        ):
            mgr.sync([cam])
            time.sleep(0.15)
            cam.auto_record = False
            mgr.sync([cam])
            deadline = time.time() + 2
            while time.time() < deadline and mgr._threads.get(6) and mgr._threads[6].is_alive():
                time.sleep(0.05)
            self.assertFalse(mgr.is_recording(6))
            mgr.stop_all()

    def test_old_thread_does_not_clear_new_alive(self):
        td = Path(tempfile.mkdtemp())
        mgr = AutoRecordManager(td)
        cam = _cam(9)
        with mock.patch("auto_record.V380SnapshotClient", _FakeClient), mock.patch(
            "auto_record.remux_annexb", _fake_remux
        ):
            mgr.sync([cam])
            time.sleep(0.15)
            cam.record_chunk = "minute"
            mgr.sync([cam])
            time.sleep(0.25)
            self.assertTrue(mgr.is_recording(9) or any(t.is_alive() for t in mgr._threads.values()))
            mgr.stop_all()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_status_roundtrip_and_alive_heartbeat(self):
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time(), "recording": {"7": True}})
        self.assertTrue(is_worker_alive(self.td))
        self.assertTrue(read_worker_status(self.td)["recording"]["7"])
        sup = RecordSupervisor(self.td)
        self.assertTrue(sup.is_recording(7))
        self.assertFalse(sup.is_recording(8))

    def test_stale_heartbeat_is_not_recording(self):
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time() - 60, "recording": {"7": True}})
        (self.td / "record_worker.pid").write_text("1", encoding="utf-8")
        self.assertFalse(is_worker_alive(self.td))
        sup = RecordSupervisor(self.td)
        self.assertFalse(sup.is_recording(7))

    def test_camera_status_shows_service_state(self):
        sup = RecordSupervisor(self.td)
        self.assertEqual(sup.camera_status(1, False), ("Service: off", "off"))
        self.assertEqual(sup.camera_status(1, True)[1], "down")
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time(), "recording": {}})
        sup = RecordSupervisor(self.td)
        self.assertEqual(sup.camera_status(1, True), ("Service: starting…", "wait"))
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time(), "recording": {"1": True}})
        sup = RecordSupervisor(self.td)
        self.assertEqual(sup.camera_status(1, True), ("Service: recording", "ok"))
        self.assertIn("running", sup.service_summary([_cam(1, auto_record=True)]))
        self.assertIn("off", sup.service_summary([_cam(1, auto_record=False)]))

    def test_sync_starts_worker_only_when_enabled(self):
        sup = RecordSupervisor(self.td)
        with mock.patch("auto_record.ensure_worker") as ensure, mock.patch("auto_record._sync_logon_task") as task:
            sup.sync([_cam(1, auto_record=False)])
            time.sleep(0.05)
            ensure.assert_not_called()
            task.assert_called_once_with(False)
            sup.sync([_cam(1, auto_record=True)])
            time.sleep(0.05)
            ensure.assert_called_once()
            task.assert_called_with(True)

    def test_stop_all_does_not_kill_background_worker(self):
        write_worker_status(self.td, {"pid": 99, "heartbeat": time.time(), "recording": {"1": True}})
        sup = RecordSupervisor(self.td)
        sup.stop_all()
        self.assertTrue(is_worker_alive(self.td))
        self.assertTrue(sup.is_recording(1))

    def test_logon_command_points_at_worker(self):
        cmd = logon_task_command()
        self.assertIn("record_worker.py", cmd)

    def test_ensure_worker_skips_spawn_when_alive(self):
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time()})
        with mock.patch("auto_record._start_worker_process") as start:
            self.assertTrue(ensure_worker(self.td))
            start.assert_not_called()

    def test_gui_does_not_write_local_files(self):
        cam = _cam(3)
        sup = RecordSupervisor(self.td, self.td)
        with mock.patch("auto_record.ensure_worker"), mock.patch("auto_record._sync_logon_task"):
            sup.sync([cam])
        client = _FakeClient()
        sup.on_video(cam, True, b"\x00\x00\x00\x01Ixxxx", client)
        self.assertFalse(any(self.td.rglob("*.h264")))
        self.assertFalse(sup.is_recording(3))
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time(), "recording": {"3": True}})
        self.assertTrue(sup.is_recording(3))
        with mock.patch("auto_record.ensure_worker") as ensure, mock.patch("auto_record._sync_logon_task"):
            sup.release_to_worker()
            time.sleep(0.05)
            ensure.assert_called()

    def test_status_write_survives_replace_lock(self):
        path = self.td / "record_worker.json"
        self.td.mkdir(parents=True, exist_ok=True)
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(32, "locked")
            Path(dst).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
            Path(src).unlink(missing_ok=True)

        with mock.patch("auto_record.os.replace", flaky_replace):
            write_worker_status(self.td, {"pid": 4, "heartbeat": time.time(), "recording": {}})
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
