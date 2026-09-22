"""Per-camera background recording.

Hourly:  recordings/<name>/<YYYY-MM-DD>/<HH>.h264 → .mp4
1-min:   recordings/<name>/<YYYY-MM-DD>/<HH>/<MM>.h264 → .mp4

The open chunk stays raw. Only a finished slot is remuxed.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REC_TZ_NAME = os.environ.get("V380_TZ", "Asia/Karachi")


def _tz():
    try:
        return ZoneInfo(REC_TZ_NAME)
    except Exception:
        return timezone(timedelta(hours=5))


def _now() -> datetime:
    return datetime.now(_tz())

from v380.client.extras import get_relay_ip, remux_annexb
from v380.client.v380_client import V380SnapshotClient
from v380.paths import DATA_DIR, REC_DIR, ROOT as APP_DIR, WORKER_SCRIPT
from v380.store.camera_store import Camera, CameraStore
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(10)}
_FOLDER_LOCK = threading.Lock()
_REMUX_Q: queue.Queue[tuple[Path, str] | None] = queue.Queue()
_REMUX_STARTED = False
_REMUX_WORKERS = 4


def sanitize_folder(name: str, device_id: str) -> str:
    cleaned = _WIN_BAD.sub("_", (name or "").strip()).strip(" .")
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned.upper() in _RESERVED:
        cleaned = f"cam_{device_id}"
    return cleaned[:80]


def _read_marker(path: Path) -> str | None:
    marker = path / ".device_id"
    try:
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return None


def _write_marker(path: Path, device_id: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".device_id"
    if _read_marker(path) != device_id:
        marker.write_text(device_id, encoding="utf-8")


def _find_folder_by_id(root: Path, device_id: str) -> Path | None:
    try:
        for child in root.iterdir():
            if child.is_dir() and _read_marker(child) == device_id:
                return child
    except OSError:
        return None
    return None


def camera_folder(cam: Camera, rec_dir: Path | None = None) -> Path:
    """Stable folder for this device: reuse by .device_id, then name, then name_id."""
    root = rec_dir or REC_DIR
    with _FOLDER_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        existing = _find_folder_by_id(root, cam.device_id)
        base = sanitize_folder(cam.name, cam.device_id)
        preferred = root / base
        alt = root / f"{base}_{cam.device_id}"

        if existing is not None:
            if existing in (preferred, alt):
                return existing
            target = preferred if not preferred.exists() else alt
            if target != existing and not target.exists():
                try:
                    existing.rename(target)
                    _write_marker(target, cam.device_id)
                    return target
                except OSError:
                    return existing
            return existing

        path = preferred
        if path.exists() and (_read_marker(path) not in (None, cam.device_id) or path.is_file()):
            path = alt
        _write_marker(path, cam.device_id)
        if _read_marker(path) != cam.device_id:
            path = alt
            _write_marker(path, cam.device_id)
        return path


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HH_RE = re.compile(r"^\d{2}$")


def chunk_mode(value: str | None) -> str:
    return "minute" if value == "minute" else "hour"


def _chunk_stamp(now: datetime, mode: str) -> tuple[str, str, str | None, datetime]:
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%H")
    if mode == "minute":
        minute = now.strftime("%M")
        until = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return day, hour, minute, until
    until = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return day, hour, None, until


def _raw_ext(fmt: str) -> str:
    return "h265" if fmt == "hevc" else "h264"


def _slot_stem(hour: str, minute: str | None) -> str:
    if minute is None:
        return f"{hour}-00-00"
    return f"{hour}-{minute}-00"


def _chunk_raw_path(folder: Path, day: str, hour: str, minute: str | None, fmt: str) -> Path:
    """Hourly: date/HH-00-00.h264. Minute: date/HH/HH-MM-00.h264."""
    folder.mkdir(parents=True, exist_ok=True)
    day_dir = folder / day
    day_dir.mkdir(parents=True, exist_ok=True)
    ext = _raw_ext(fmt)
    if minute is None:
        return day_dir / f"{_slot_stem(hour, None)}.{ext}"
    hour_dir = day_dir / hour
    hour_dir.mkdir(parents=True, exist_ok=True)
    return hour_dir / f"{_slot_stem(hour, minute)}.{ext}"


def is_current_slot(path: Path, now: datetime | None = None) -> bool:
    """True if this raw file is the still-open clock slot (must stay .h264)."""
    now = now or _now()
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%H")
    minute = now.strftime("%M")
    if path.suffix not in (".h264", ".h265"):
        return False
    stem = path.stem
    parent = path.parent.name
    grand = path.parent.parent.name if path.parent.parent else ""
    if parent == day and stem == _slot_stem(hour, None):
        return True
    if parent == hour and grand == day and stem == _slot_stem(hour, minute):
        return True
    if _DATE_RE.match(parent) and _HH_RE.match(stem):
        return parent == day and stem == hour
    if _HH_RE.match(parent) and _DATE_RE.match(grand) and _HH_RE.match(stem):
        return grand == day and parent == hour and stem == minute
    return False


def should_leave_raw(path: Path, now: datetime | None = None) -> bool:
    if path.name.startswith("rec_"):
        return True
    return is_current_slot(path, now)


def rename_legacy_names(root: Path | None = None) -> int:
    """Turn old 27.mp4 / 14.mp4 names into 14-27-00.mp4 / 14-00-00.mp4."""
    root = root or REC_DIR
    if not root.exists():
        return 0
    clock = _now()
    done = 0
    for path in list(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".mp4", ".h264", ".h265"):
            continue
        stem = path.stem
        if not _HH_RE.match(stem):
            continue
        if path.suffix.lower() != ".mp4" and should_leave_raw(path, clock):
            continue
        parent = path.parent.name
        if _HH_RE.match(parent):
            new_name = f"{parent}-{stem}-00{path.suffix.lower()}"
        elif _DATE_RE.match(parent):
            new_name = f"{stem}-00-00{path.suffix.lower()}"
        else:
            continue
        dest = path.with_name(new_name)
        if dest.exists():
            continue
        try:
            path.rename(dest)
            done += 1
        except OSError:
            pass
    return done


_OPEN_RAW: set[str] = set()
_OPEN_LOCK = threading.Lock()


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _mark_open(path: Path | None, open_: bool) -> None:
    if path is None:
        return
    keys = {_path_key(path), str(path)}
    with _OPEN_LOCK:
        if open_:
            _OPEN_RAW.update(keys)
        else:
            _OPEN_RAW.difference_update(keys)


def remux_orphans(rec_dir: Path | None, now: datetime | None = None) -> int:
    """Remux finished leftover raw files. Never touch the current slot or rec_*."""
    root = rec_dir or REC_DIR
    if not root.exists():
        return 0
    rename_legacy_names(root)
    clock = now or _now()
    done = 0
    for ext, fmt in ((".h264", "h264"), (".h265", "hevc")):
        for path in root.rglob(f"*{ext}"):
            if should_leave_raw(path, clock):
                continue
            with _OPEN_LOCK:
                busy = str(path) in _OPEN_RAW or _path_key(path) in _OPEN_RAW
            if busy:
                continue
            try:
                if path.stat().st_size <= 0:
                    path.unlink(missing_ok=True)
                    continue
                out = remux_annexb(path, fmt)
                if out is not None and out.suffix == ".mp4":
                    done += 1
            except OSError:
                pass
    return done


def _ensure_remux_workers() -> None:
    global _REMUX_STARTED
    if _REMUX_STARTED:
        return
    _REMUX_STARTED = True
    for _ in range(_REMUX_WORKERS):
        threading.Thread(target=_remux_worker, daemon=True).start()


def _remux_worker() -> None:
    while True:
        item = _REMUX_Q.get()
        try:
            if item is None:
                return
            path, fmt = item
            for _ in range(5):
                try:
                    out = remux_annexb(path, fmt)
                    if out is None or out.suffix == ".mp4" or not path.exists():
                        break
                except Exception:
                    pass
                time.sleep(1.0)
        except Exception:
            pass
        finally:
            _REMUX_Q.task_done()


def _schedule_remux(path: Path, fmt: str) -> Path:
    _ensure_remux_workers()
    _REMUX_Q.put((path, fmt))
    return path.with_suffix(".mp4")


def wait_remux(timeout: float = 180.0) -> None:
    done = threading.Event()

    def _join() -> None:
        _REMUX_Q.join()
        done.set()

    threading.Thread(target=_join, daemon=True).start()
    done.wait(timeout)


class _ChunkWriter:
    """Writes one stable slot file. Splits on the first I-frame after the clock."""

    # While an hour file is still open, refresh a playable .partial.mp4 so users can
    # watch the last ~10–15 minutes without waiting for the hour to finish.
    PARTIAL_EVERY_SEC = 5 * 60
    PARTIAL_FIRST_SEC = 60

    def __init__(self, cam: Camera, rec_dir: Path | None = None, remux=None):
        self._cam = cam
        self._rec_dir = rec_dir
        self._remux = remux or _schedule_remux
        self._mode = chunk_mode(getattr(cam, "record_chunk", "hour"))
        self._fh = None
        self._raw: Path | None = None
        self._fmt = "h264"
        self._until = _now()
        self._bytes = 0
        self._opened_at = 0.0
        self._last_partial = 0.0

    def feed(self, now: datetime, fmt: str, is_iframe: bool, annexb: bytes) -> None:
        if not annexb:
            return
        want = "hevc" if fmt == "hevc" else "h264"
        if self._fh is None:
            if not is_iframe:
                return
            self._open(now, want)
            self._write(annexb)
            return
        rotate = is_iframe and (now >= self._until or want != self._fmt)
        if rotate:
            self.close()
            self._open(now, want)
        self._write(annexb)
        self._maybe_partial()

    def _open(self, now: datetime, fmt: str) -> None:
        day, hour, minute, until = _chunk_stamp(now, self._mode)
        raw = _chunk_raw_path(camera_folder(self._cam, self._rec_dir), day, hour, minute, fmt)
        mode = "ab" if raw.exists() else "wb"
        self._raw = raw
        self._fh = raw.open(mode)
        self._bytes = raw.stat().st_size
        self._fmt = fmt
        self._until = until
        self._opened_at = time.time()
        self._last_partial = 0.0
        self._last_flush = time.time()
        _mark_open(raw, True)

    def _write(self, annexb: bytes) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(annexb)
            self._bytes += len(annexb)
            # Flush occasionally — every-frame flush stalls live decode on the same disk.
            now = time.time()
            if now - getattr(self, "_last_flush", 0) >= 1.0 or self._bytes % (2 * 1024 * 1024) < len(annexb):
                self._fh.flush()
                self._last_flush = now
        except OSError:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            _mark_open(self._raw, False)

    def _maybe_partial(self) -> None:
        if self._mode != "hour" or self._raw is None or self._bytes < 50_000:
            return
        now_ts = time.time()
        age = now_ts - self._opened_at
        due_first = self._last_partial <= 0 and age >= self.PARTIAL_FIRST_SEC
        due_next = self._last_partial > 0 and (now_ts - self._last_partial) >= self.PARTIAL_EVERY_SEC
        if not (due_first or due_next):
            return
        self._last_partial = now_ts
        try:
            if self._fh is not None:
                self._fh.flush()
                os.fsync(self._fh.fileno())
        except OSError:
            pass
        path = self._raw
        fmt = self._fmt

        def work() -> None:
            try:
                from v380.client.extras import remux_snapshot

                remux_snapshot(path, fmt)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="partial-remux").start()

    def close(self) -> Path | None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        path = self._raw
        self._raw = None
        wrote = self._bytes
        self._bytes = 0
        _mark_open(path, False)
        if path is None:
            return None
        # Drop stale partial when the real hour remux finishes.
        try:
            path.with_name(path.stem + ".partial.mp4").unlink(missing_ok=True)
        except OSError:
            pass
        if wrote <= 0:
            path.unlink(missing_ok=True)
            return None
        return self._remux(path, self._fmt)


# Older tests / imports
_HourWriter = _ChunkWriter


class AutoRecordManager:
    def __init__(self, rec_dir: Path | None = None):
        self._rec_dir = rec_dir
        self._stop = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._flags: dict[int, threading.Event] = {}
        self._alive: dict[int, bool] = {}
        self._spec: dict[int, tuple] = {}
        self._lock = threading.Lock()

    def sync(self, cameras: list[Camera]) -> None:
        wanted = {c.id: c for c in cameras if c.auto_record and c.id}
        with self._lock:
            for cam_id, flag in list(self._flags.items()):
                if cam_id not in wanted:
                    flag.set()
                    self._spec.pop(cam_id, None)
            for cam in wanted.values():
                spec = (
                    cam.ip,
                    cam.port,
                    cam.username,
                    cam.password,
                    cam.quality,
                    cam.source,
                    chunk_mode(getattr(cam, "record_chunk", "hour")),
                    cam.name,
                )
                alive = cam.id in self._threads and self._threads[cam.id].is_alive()
                if alive and self._spec.get(cam.id) == spec:
                    continue
                old = self._flags.get(cam.id)
                if old is not None:
                    old.set()
                flag = threading.Event()
                self._flags[cam.id] = flag
                self._spec[cam.id] = spec
                t = threading.Thread(target=self._run, args=(cam, flag), daemon=True, name=f"auto-rec-{cam.id}")
                self._threads[cam.id] = t
                t.start()

    def is_recording(self, camera_id: int) -> bool:
        return bool(self._alive.get(camera_id))

    def snapshot(self) -> dict[int, bool]:
        return {cam_id: bool(flag) for cam_id, flag in self._alive.items()}

    def stop_all(self) -> None:
        self._stop.set()
        with self._lock:
            for flag in self._flags.values():
                flag.set()
            threads = list(self._threads.values())
        for t in threads:
            t.join(timeout=8)
        wait_remux(120)
        with self._lock:
            self._flags.clear()
            self._threads.clear()
            self._spec.clear()
            self._alive.clear()

    def _run(self, cam: Camera, flag: threading.Event) -> None:
        if getattr(cam, "is_ezviz", False) or str(getattr(cam, "brand", "") or "").lower() == "ezviz":
            self._run_ezviz(cam, flag)
            return
        writer = _ChunkWriter(cam, self._rec_dir)
        self._alive[cam.id] = False
        try:
            while not self._stop.is_set() and not flag.is_set():
                client = None
                try:
                    host = cam.ip
                    if cam.source == "cloud":
                        host = get_relay_ip(int(cam.device_id))
                        if not host:
                            raise RuntimeError("No cloud relay")
                    client = V380SnapshotClient(
                        host,
                        int(cam.device_id),
                        cam.username,
                        cam.password,
                        cam.port,
                        quality=cam.quality,
                        source=cam.source,
                    )
                    client.connect()
                    need_key = True
                    for kind, is_iframe, payload in client.iter_video_frames(flag):
                        if self._stop.is_set() or flag.is_set():
                            break
                        if kind != "video":
                            continue
                        if need_key and not is_iframe:
                            continue
                        need_key = False
                        rec = client.h264_for_decode(payload) if is_iframe else payload
                        writer.feed(_now(), client.video_codec, bool(is_iframe), rec)
                        self._alive[cam.id] = True
                except Exception as exc:
                    self._alive[cam.id] = False
                    _log(_data_dir(), f"record {cam.device_id}: {type(exc).__name__}: {exc}")
                    if self._stop.is_set() or flag.is_set():
                        break
                    time.sleep(1.0)
                finally:
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass
        finally:
            writer.close()
            with self._lock:
                if self._flags.get(cam.id) is flag:
                    self._alive[cam.id] = False
                    self._flags.pop(cam.id, None)
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)

    def _run_ezviz(self, cam: Camera, flag: threading.Event) -> None:
        """EZVIZ: FFmpeg copy from RTSP into timed .mp4 chunks."""
        from v380.client.ezviz_rtsp import resolve_rtsp_url
        from v380.client.v380_client import _ffmpeg_exe

        self._alive[cam.id] = False
        exe = _ffmpeg_exe()
        url = resolve_rtsp_url(cam)
        if not exe or not url:
            _log(_data_dir(), f"ezviz record {cam.device_id}: missing ffmpeg or RTSP URL")
            with self._lock:
                if self._flags.get(cam.id) is flag:
                    self._alive[cam.id] = False
                    self._flags.pop(cam.id, None)
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)
            return
        mode = chunk_mode(getattr(cam, "record_chunk", "hour"))
        try:
            while not self._stop.is_set() and not flag.is_set():
                now = _now()
                day, hour, minute, until = _chunk_stamp(now, mode)
                folder = camera_folder(cam, self._rec_dir)
                if mode == "minute":
                    dest = folder / day / hour / f"{minute}.mp4"
                else:
                    dest = folder / day / f"{hour}.mp4"
                dest.parent.mkdir(parents=True, exist_ok=True)
                kwargs = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                if sys.platform == "win32":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = None
                try:
                    proc = subprocess.Popen(
                        [
                            exe,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-rtsp_transport",
                            "tcp",
                            "-i",
                            url,
                            "-c",
                            "copy",
                            "-movflags",
                            "+faststart",
                            "-y",
                            str(dest),
                        ],
                        **kwargs,
                    )
                    self._alive[cam.id] = True
                    while not self._stop.is_set() and not flag.is_set() and _now() < until:
                        if proc.poll() is not None:
                            break
                        time.sleep(1.0)
                except Exception as exc:
                    self._alive[cam.id] = False
                    _log(_data_dir(), f"ezviz record {cam.device_id}: {type(exc).__name__}: {exc}")
                    if self._stop.is_set() or flag.is_set():
                        break
                    time.sleep(2.0)
                finally:
                    if proc is not None and proc.poll() is None:
                        try:
                            proc.terminate()
                            proc.wait(timeout=5)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                    self._alive[cam.id] = False
        finally:
            with self._lock:
                if self._flags.get(cam.id) is flag:
                    self._alive[cam.id] = False
                    self._flags.pop(cam.id, None)
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)


TASK_NAME = "V380StudioAutoRecord"
WATCH_TASK_NAME = "V380StudioAutoRecordWatch"
_MUTEX_HANDLE = None
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_HEARTBEAT_MAX_AGE = 15.0
_IDLE_EXIT_SEC = 120.0


def _data_dir(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("V380_DATA_DIR")
    return Path(env) if env else DATA_DIR


def _rec_dir(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("V380_REC_DIR")
    return Path(env) if env else REC_DIR


def _pid_path(data_dir: Path) -> Path:
    return data_dir / "record_worker.pid"


def _status_path(data_dir: Path) -> Path:
    return data_dir / "record_worker.json"


def _python_exe() -> str:
    return sys.executable


def _worker_script() -> Path:
    return WORKER_SCRIPT


def _log(data_dir: Path, message: str) -> None:
    path = data_dir / "record_worker.log"
    line = f"{_now().isoformat(timespec='seconds')} {message}\n"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.write_text(line, encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint(32768)
        query = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if query is not None and query(handle, 0, buf, ctypes.byref(size)):
            name = buf.value.lower()
            return "python" in Path(name).name
        return True
    finally:
        kernel32.CloseHandle(handle)


def read_worker_status(data_dir: Path | None = None) -> dict:
    path = _status_path(_data_dir(data_dir))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_worker_status(data_dir: Path, payload: dict) -> None:
    path = _status_path(data_dir)
    tmp = path.with_name(f"record_worker.{os.getpid()}.tmp")
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload)
    tmp.write_text(text, encoding="utf-8")
    last_err: OSError | None = None
    for _ in range(25):
        try:
            os.replace(str(tmp), str(path))
            return
        except OSError as exc:
            last_err = exc
            time.sleep(0.04)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        last_err = exc
    tmp.unlink(missing_ok=True)
    if last_err is not None:
        raise last_err


def _lease_path(data_dir: Path) -> Path:
    return data_dir / "studio.lease"


def touch_studio_lease(data_dir: Path | None = None) -> None:
    root = _data_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    _lease_path(root).write_text(json.dumps({"pid": os.getpid(), "heartbeat": time.time()}), encoding="utf-8")


def clear_studio_lease(data_dir: Path | None = None) -> None:
    _lease_path(_data_dir(data_dir)).unlink(missing_ok=True)


def studio_lease_fresh(data_dir: Path | None = None, max_age: float = 6.0) -> bool:
    path = _lease_path(_data_dir(data_dir))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        beat = float(raw.get("heartbeat") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return bool(beat) and (time.time() - beat) <= max_age


def is_worker_alive(data_dir: Path | None = None) -> bool:
    root = _data_dir(data_dir)
    status = read_worker_status(root)
    try:
        beat = float(status.get("heartbeat") or 0)
    except (TypeError, ValueError):
        beat = 0.0
    if beat and (time.time() - beat) <= _HEARTBEAT_MAX_AGE:
        return True
    try:
        pid = int(_pid_path(root).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = int(status.get("pid") or 0)
    return _pid_running(pid)


def _start_worker_process(data_dir: Path | None = None, rec_dir: Path | None = None) -> None:
    root = _data_dir(data_dir)
    rec = _rec_dir(rec_dir)
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["V380_DATA_DIR"] = str(root)
    env["V380_REC_DIR"] = str(rec)
    args = [_python_exe(), str(_worker_script())]
    kwargs: dict = {
        "cwd": str(APP_DIR),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        detached = 0x00000008
        flag_sets = (
            _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB | detached,
            _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB | _CREATE_NO_WINDOW,
            _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            _CREATE_NEW_PROCESS_GROUP,
        )
        last_err: OSError | None = None
        for flags in flag_sets:
            try:
                subprocess.Popen(args, creationflags=flags, **kwargs)
                return
            except OSError as exc:
                last_err = exc
        try:
            subprocess.Popen(
                f'start "" /B "{args[0]}" "{args[1]}"',
                shell=True,
                cwd=str(APP_DIR),
                env=env,
                creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError as exc:
            last_err = exc
        if last_err is not None:
            raise last_err
        return
    subprocess.Popen(args, start_new_session=True, **kwargs)


def ensure_worker(data_dir: Path | None = None, rec_dir: Path | None = None) -> bool:
    root = _data_dir(data_dir)
    if is_worker_alive(root):
        return True
    _start_worker_process(root, rec_dir)
    for _ in range(25):
        time.sleep(0.1)
        if is_worker_alive(root):
            return True
    return is_worker_alive(root)


def logon_task_command() -> str:
    return f'"{_python_exe()}" "{_worker_script()}"'


def _run_schtasks(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)


def _sync_logon_task(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        if enabled:
            cmd = logon_task_command()
            _run_schtasks(
                [
                    "schtasks", "/Create", "/F",
                    "/TN", TASK_NAME,
                    "/SC", "ONLOGON",
                    "/RL", "LIMITED",
                    "/TR", cmd,
                ]
            )
            _run_schtasks(
                [
                    "schtasks", "/Create", "/F",
                    "/TN", WATCH_TASK_NAME,
                    "/SC", "MINUTE",
                    "/MO", "5",
                    "/RL", "LIMITED",
                    "/TR", cmd,
                ]
            )
        else:
            _run_schtasks(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
            _run_schtasks(["schtasks", "/Delete", "/F", "/TN", WATCH_TASK_NAME])
    except Exception:
        pass


def _acquire_worker_mutex() -> bool:
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, "Local\\V380StudioAutoRecord")
    if not handle:
        return False
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def run_worker_forever(data_dir: Path | None = None, rec_dir: Path | None = None) -> None:
    root = _data_dir(data_dir)
    rec = _rec_dir(rec_dir)
    root.mkdir(parents=True, exist_ok=True)
    if not _acquire_worker_mutex():
        return
    _pid_path(root).write_text(str(os.getpid()), encoding="utf-8")
    _log(root, "worker started")
    try:
        remux_orphans(rec)
    except Exception as exc:
        _log(root, f"startup remux failed: {exc}")
    store = CameraStore(root)
    mgr = AutoRecordManager(rec)
    idle_since: float | None = None
    try:
        while True:
            try:
                cameras = store.list()
            except Exception as exc:
                _log(root, f"db read failed: {exc}")
                time.sleep(1.5)
                continue
            enabled = [c for c in cameras if c.auto_record]
            try:
                mgr.sync(cameras)
            except Exception as exc:
                _log(root, f"sync failed: {exc}")
            try:
                write_worker_status(
                    root,
                    {
                        "pid": os.getpid(),
                        "heartbeat": time.time(),
                        "enabled": [c.id for c in enabled],
                        "recording": {str(k): v for k, v in mgr.snapshot().items()},
                    },
                )
            except Exception as exc:
                _log(root, f"status write failed: {exc}")
            if enabled:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since >= _IDLE_EXIT_SEC:
                    _log(root, "idle exit — no cameras enabled")
                    break
            time.sleep(1.0)
    except KeyboardInterrupt:
        _log(root, "worker interrupted")
    except Exception as exc:
        _log(root, f"worker crash: {type(exc).__name__}: {exc}")
    finally:
        mgr.stop_all()
        store.close()
        try:
            _pid_path(root).unlink(missing_ok=True)
            _status_path(root).unlink(missing_ok=True)
        except OSError:
            pass
        _log(root, "worker stopped")


class RecordSupervisor:
    """GUI handle only. The detached worker is the single recorder."""

    def __init__(self, data_dir: Path | None = None, rec_dir: Path | None = None, remux=None):
        self._data_dir = _data_dir(data_dir)
        self._rec_dir = rec_dir
        self._enabled: set[int] = set()
        self._watch_at = 0.0
        self._status: dict = {}
        self._status_at = 0.0
        self._status_mtime = -1.0

    def _kick_worker(self) -> None:
        data_dir = self._data_dir
        rec_dir = self._rec_dir
        enabled = bool(self._enabled)

        def work() -> None:
            _sync_logon_task(enabled)
            if enabled:
                ensure_worker(data_dir, rec_dir)

        threading.Thread(target=work, daemon=True, name="rec-supervisor").start()

    def sync(self, cameras: list[Camera]) -> None:
        self._enabled = {c.id for c in cameras if c.auto_record}
        self._status_at = 0.0
        self._kick_worker()

    def watch(self) -> None:
        if not self._enabled:
            return
        now = time.time()
        if now - self._watch_at < 5.0:
            return
        self._watch_at = now
        if not is_worker_alive(self._data_dir):
            self._kick_worker()

    def on_video(self, cam: Camera, is_iframe: bool, payload: bytes, client) -> None:
        return None

    def _status_fresh(self) -> dict:
        now = time.time()
        path = _status_path(self._data_dir)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if now - self._status_at >= 0.25 or mtime != self._status_mtime:
            self._status = read_worker_status(self._data_dir)
            self._status_at = now
            self._status_mtime = mtime
        return self._status

    def service_running(self) -> bool:
        status = self._status_fresh()
        try:
            beat = float(status.get("heartbeat") or 0)
        except (TypeError, ValueError):
            return False
        return bool(beat) and (time.time() - beat) <= _HEARTBEAT_MAX_AGE

    def is_recording(self, camera_id: int) -> bool:
        if not self.service_running():
            return False
        rec = self._status_fresh().get("recording") or {}
        if isinstance(rec, dict):
            return bool(rec.get(str(camera_id)) or rec.get(camera_id))
        return False

    def camera_status(self, camera_id: int, enabled: bool) -> tuple[str, str]:
        """Human status for one camera: (text, level off|ok|wait|down)."""
        if not enabled:
            return "Service: off", "off"
        if self.is_recording(camera_id):
            return "Service: recording", "ok"
        if self.service_running():
            return "Service: starting…", "wait"
        return "Service: not running", "down"

    def service_summary(self, cameras: list[Camera]) -> str:
        wanted = [c for c in cameras if c.auto_record]
        if not wanted:
            return "Record service: off"
        writing = sum(1 for c in wanted if self.is_recording(c.id))
        if self.service_running():
            return f"Record service: running  ·  {writing}/{len(wanted)} camera(s) saving"
        return "Record service: not running  ·  waiting to start"

    def release_to_worker(self) -> None:
        if self._enabled:
            self._kick_worker()

    def stop_all(self) -> None:
        return None
