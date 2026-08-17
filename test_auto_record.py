"""Unit tests for hourly auto-record paths, store, writer, and worker lifecycle."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from auto_record import (
    AutoRecordManager,
    RecordSupervisor,
    _HourWriter,
    _hour_raw_path,
    _hour_stamp,
    _segment_path,
    camera_folder,
    ensure_worker,
    is_worker_alive,
    logon_task_command,
    read_worker_status,
    remux_orphans,
    sanitize_folder,
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


class SegmentPathTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 8, 17, 14, 37, 5)

    def test_first_segment_is_hour(self):
        p = _segment_path(self.td / "Lab", "2026-08-17", "14", self.now)
        self.assertEqual(p.name, "14")
        self.assertTrue((self.td / "Lab" / "2026-08-17").is_dir())

    def test_existing_mp4_uses_hour_minute(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        (day / "14.mp4").write_bytes(b"old")
        p = _segment_path(self.td / "Lab", "2026-08-17", "14", self.now)
        self.assertEqual(p.name, "14-37")
        raw = _hour_raw_path(self.td / "Lab", "2026-08-17", "14", "h264", self.now)
        self.assertEqual(raw.name, "14-37.h264")

    def test_existing_raw_is_reused(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        (day / "14.h264").write_bytes(b"old")
        p = _hour_raw_path(self.td / "Lab", "2026-08-17", "14", "h264", self.now)
        self.assertEqual(p.name, "14.h264")

    def test_same_minute_uses_seconds(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        (day / "14.mp4").write_bytes(b"a")
        (day / "14-37.mp4").write_bytes(b"b")
        p = _segment_path(self.td / "Lab", "2026-08-17", "14", self.now)
        self.assertEqual(p.name, "14-37-05")

    def test_same_second_increments(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        for name in ("14.mp4", "14-37.mp4", "14-37-05.mp4"):
            (day / name).write_bytes(b"x")
        p = _segment_path(self.td / "Lab", "2026-08-17", "14", self.now)
        self.assertEqual(p.name, "14-37-05-2")

    def test_midnight_hour_stamp(self):
        now = datetime(2026, 8, 17, 23, 59, 1)
        day, hour, until = _hour_stamp(now)
        self.assertEqual(day, "2026-08-17")
        self.assertEqual(hour, "23")
        self.assertEqual(until, datetime(2026, 8, 18, 0, 0, 0))
        day2, hour2, _ = _hour_stamp(until)
        self.assertEqual((day2, hour2), ("2026-08-18", "00"))


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
        self.assertTrue((self.td / "Lab").is_dir())
        self.assertTrue((self.td / "Lab_202").is_dir())

    def test_rename_moves_existing_folder(self):
        first = camera_folder(_cam(1, "Lab", "101"), self.td)
        (first / "keep.txt").write_text("x", encoding="utf-8")
        moved = camera_folder(_cam(1, "Office", "101"), self.td)
        self.assertEqual(moved.name, "Office")
        self.assertTrue((moved / "keep.txt").exists())
        self.assertFalse((self.td / "Lab").exists())

    def test_rename_keeps_old_if_new_name_taken(self):
        camera_folder(_cam(1, "Lab", "101"), self.td)
        camera_folder(_cam(2, "Office", "202"), self.td)
        path = camera_folder(_cam(1, "Office", "101"), self.td)
        self.assertEqual(path.name, "Office_101")
        self.assertTrue((self.td / "Office" / ".device_id").read_text(encoding="utf-8") == "202")


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.cam = _cam()
        self.w = _HourWriter(self.cam, self.td, remux=_fake_remux)

    def test_first_hour_file_and_empty_delete(self):
        now = datetime(2026, 8, 17, 14, 5, 0)
        self.w.ensure(now, "h264")
        raw = self.td / "Lab" / "2026-08-17" / "14.h264"
        self.assertTrue(raw.exists())
        self.assertIsNone(self.w.close())
        self.assertFalse(raw.exists())

    def test_writes_then_remuxes_to_mp4(self):
        now = datetime(2026, 8, 17, 14, 5, 0)
        self.w.ensure(now, "h264")
        self.w.write(b"\x00\x00\x00\x01frame")
        out = self.w.close()
        self.assertEqual(out.name, "14.mp4")
        self.assertTrue(out.exists())
        self.assertFalse((self.td / "Lab" / "2026-08-17" / "14.h264").exists())

    def test_restart_does_not_overwrite(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        (day / "14.mp4").write_bytes(b"first-hour")
        now = datetime(2026, 8, 17, 14, 37, 0)
        self.w.ensure(now, "h264")
        self.w.write(b"second")
        out = self.w.close()
        self.assertEqual(out.name, "14-37.mp4")
        self.assertEqual((day / "14.mp4").read_bytes(), b"first-hour")

    def test_hour_rollover_starts_new_file(self):
        start = datetime(2026, 8, 17, 14, 59, 50)
        self.w.ensure(start, "h264")
        self.w.write(b"hour14")
        later = datetime(2026, 8, 17, 15, 0, 0)
        self.assertTrue(self.w.due(later))
        self.w.close()
        self.w.ensure(later, "h264", force=True)
        self.w.write(b"hour15")
        out = self.w.close()
        self.assertEqual(out.name, "15.mp4")
        self.assertTrue((self.td / "Lab" / "2026-08-17" / "14.mp4").exists())

    def test_day_rollover_new_date_folder(self):
        start = datetime(2026, 8, 17, 23, 59, 0)
        self.w.ensure(start, "h264")
        self.w.write(b"late")
        self.w.close()
        nxt = datetime(2026, 8, 18, 0, 0, 1)
        self.w.ensure(nxt, "h264", force=True)
        self.w.write(b"early")
        out = self.w.close()
        self.assertEqual(out.parent.name, "2026-08-18")
        self.assertEqual(out.name, "00.mp4")

    def test_hevc_uses_h265_raw(self):
        now = datetime(2026, 8, 17, 8, 0, 0)
        self.w.ensure(now, "hevc")
        self.w.write(b"hevc")
        self.assertTrue((self.td / "Lab" / "2026-08-17" / "08.h265").exists())
        out = self.w.close()
        self.assertEqual(out.suffix, ".mp4")

    def test_due_false_before_next_hour(self):
        now = datetime(2026, 8, 17, 14, 0, 0)
        self.w.ensure(now, "h264")
        self.assertFalse(self.w.due(now + timedelta(minutes=59)))
        self.assertTrue(self.w.due(now + timedelta(hours=1)))
        self.w.close()

    def test_reconnect_appends_same_hour_file(self):
        now = datetime(2026, 8, 17, 14, 10, 0)
        self.w.ensure(now, "h264")
        self.w.write(b"AAA")
        self.w._fh.close()
        self.w._fh = None
        self.w._raw = None
        self.w._bytes = 0
        later = _HourWriter(self.cam, self.td, remux=_fake_remux)
        later.ensure(now.replace(minute=37), "h264", force=True)
        later.write(b"BBB")
        out = later.close()
        self.assertEqual(out.name, "14.mp4")
        self.assertEqual(out.read_bytes(), b"AAABBB")
        files = list((self.td / "Lab" / "2026-08-17").glob("14*"))
        self.assertEqual(len(files), 1)

    def test_force_same_hour_does_not_split_file(self):
        now = datetime(2026, 8, 17, 14, 5, 0)
        self.w.ensure(now, "h264")
        self.w.write(b"A")
        self.w.ensure(now.replace(minute=40), "h264", force=True)
        self.w.write(b"B")
        self.w._fh.flush()
        raws = list((self.td / "Lab" / "2026-08-17").glob("*.h264"))
        self.assertEqual(len(raws), 1)
        self.assertEqual(raws[0].read_bytes(), b"AB")
        self.w.close()

    def test_orphan_h264_becomes_mp4(self):
        day = self.td / "Lab" / "2026-08-17"
        day.mkdir(parents=True)
        raw = day / "11.h264"
        raw.write_bytes(b"leftover")
        with mock.patch("auto_record.remux_annexb", _fake_remux):
            self.assertEqual(remux_orphans(self.td), 1)
        self.assertTrue((day / "11.mp4").exists())
        self.assertFalse(raw.exists())


class StoreTests(unittest.TestCase):
    def test_upsert_and_toggle(self):
        td = Path(tempfile.mkdtemp())
        store = CameraStore(td)
        saved = store.upsert(_cam(0, auto_record=True))
        self.assertTrue(saved.auto_record)
        store.set_auto_record(saved.id, False)
        self.assertFalse(store.get(saved.id).auto_record)
        again = store.upsert(_cam(0, auto_record=True))
        self.assertTrue(again.auto_record)
        store.close()

    def test_migrate_old_db_adds_column(self):
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
        self.assertEqual(conn.execute("SELECT auto_record FROM cameras WHERE id=1").fetchone()[0], 0)
        conn.close()


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.video_codec = "h264"
        self._hold = kwargs.pop("_hold", None)

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

    def test_stale_heartbeat_without_process_is_dead(self):
        write_worker_status(self.td, {"pid": 1, "heartbeat": time.time() - 60, "recording": {"7": True}})
        (self.td / "record_worker.pid").write_text("1", encoding="utf-8")
        self.assertFalse(is_worker_alive(self.td))

    def test_sync_starts_worker_only_when_enabled(self):
        sup = RecordSupervisor(self.td)
        with mock.patch("auto_record.ensure_worker") as ensure, mock.patch("auto_record._sync_logon_task") as task:
            sup.sync([_cam(1, auto_record=False)])
            ensure.assert_not_called()
            task.assert_called_once_with(False)
            sup.sync([_cam(1, auto_record=True)])
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
        with mock.patch("auto_record.ensure_worker") as ensure:
            sup.release_to_worker()
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
