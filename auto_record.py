"""Per-camera background recording: recordings/<name>/<YYYY-MM-DD>/<HH>.mp4."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from camera_store import Camera, CameraStore
from extras import get_relay_ip, remux_annexb
from v380_client import V380SnapshotClient

REC_DIR = Path(__file__).with_name("recordings")
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


def _hour_stamp(now: datetime) -> tuple[str, str, datetime]:
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%H")
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return day, hour, next_hour


def _stem_taken(day_dir: Path, stem: str) -> bool:
    if not day_dir.exists():
        return False
    try:
        for item in day_dir.iterdir():
            if item.is_file() and (item.name == stem or item.stem == stem):
                return True
    except OSError:
        return False
    return False


def _segment_path(folder: Path, day: str, hour: str, now: datetime) -> Path:
    """Return a unique stem (no suffix): HH, or HH-MM, or HH-MM-SS[-n] if those exist."""
    folder.mkdir(parents=True, exist_ok=True)
    day_dir = folder / day
    day_dir.mkdir(parents=True, exist_ok=True)
    if not _stem_taken(day_dir, hour):
        return day_dir / hour
    minute = f"{hour}-{now.strftime('%M')}"
    if not _stem_taken(day_dir, minute):
        return day_dir / minute
    second = f"{minute}-{now.strftime('%S')}"
    if not _stem_taken(day_dir, second):
        return day_dir / second
    n = 2
    while _stem_taken(day_dir, f"{second}-{n}"):
        n += 1
        if n > 999:
            return day_dir / f"{second}-{int(now.timestamp())}"
    return day_dir / f"{second}-{n}"


def _hour_raw_path(folder: Path, day: str, hour: str, fmt: str, now: datetime) -> Path:
    """Keep one in-progress HH.h264; split only if that hour was already remuxed to mp4."""
    folder.mkdir(parents=True, exist_ok=True)
    day_dir = folder / day
    day_dir.mkdir(parents=True, exist_ok=True)
    ext = "h265" if fmt == "hevc" else "h264"
    primary = day_dir / f"{hour}.{ext}"
    mp4 = day_dir / f"{hour}.mp4"
    if mp4.exists() and primary.exists():
        return _segment_path(folder, day, hour, now).with_suffix(f".{ext}")
    if mp4.exists():
        return _segment_path(folder, day, hour, now).with_suffix(f".{ext}")
    return primary


_OPEN_RAW: set[str] = set()
_OPEN_LOCK = threading.Lock()


def _mark_open(path: Path | None, open_: bool) -> None:
    if path is None:
        return
    key = str(path.resolve()) if path.exists() or open_ else str(path)
    with _OPEN_LOCK:
        if open_:
            _OPEN_RAW.add(str(path))
        else:
            _OPEN_RAW.discard(str(path))
            try:
                _OPEN_RAW.discard(str(path.resolve()))
            except OSError:
                pass


def remux_orphans(rec_dir: Path | None) -> int:
    """Turn leftover .h264/.h265 into .mp4 after a crash or GUI close."""
    root = rec_dir or REC_DIR
    if not root.exists():
        return 0
    done = 0
    for ext, fmt in ((".h264", "h264"), (".h265", "hevc")):
        for path in root.rglob(f"*{ext}"):
            key = str(path)
            with _OPEN_LOCK:
                busy = key in _OPEN_RAW or str(path.resolve()) in _OPEN_RAW
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


class _HourWriter:
    def __init__(self, cam: Camera, rec_dir: Path | None = None, remux=None):
        self._cam = cam
        self._rec_dir = rec_dir
        self._remux = remux or _schedule_remux
        self._fh = None
        self._raw: Path | None = None
        self._fmt = "h264"
        self._day = ""
        self._hour = ""
        self._until = datetime.now()
        self._bytes = 0
        self._flush_at = 0.0

    def ensure(self, now: datetime, fmt: str, force: bool = False) -> None:
        day, hour, until = _hour_stamp(now)
        want = "hevc" if fmt == "hevc" else "h264"
        if self._fh is not None and day == self._day and hour == self._hour and want == self._fmt:
            return
        if self._fh is not None:
            self.close()
        self._fmt = want
        raw = _hour_raw_path(camera_folder(self._cam, self._rec_dir), day, hour, want, now)
        mode = "ab" if raw.exists() else "wb"
        self._raw = raw
        self._fh = raw.open(mode)
        self._bytes = raw.stat().st_size
        self._day = day
        self._hour = hour
        self._until = until
        self._flush_at = time.monotonic()
        _mark_open(raw, True)

    def due(self, now: datetime) -> bool:
        return self._fh is not None and now >= self._until

    def write(self, annexb: bytes) -> None:
        if self._fh is None or not annexb:
            return
        self._fh.write(annexb)
        self._bytes += len(annexb)
        now = time.monotonic()
        if now - self._flush_at >= 1.0:
            try:
                self._fh.flush()
            except OSError:
                pass
            self._flush_at = now

    def close(self) -> Path | None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except OSError:
                pass
            self._fh.close()
            self._fh = None
        path = self._raw
        self._raw = None
        wrote = self._bytes
        self._bytes = 0
        _mark_open(path, False)
        if path is None:
            return None
        if wrote <= 0:
            path.unlink(missing_ok=True)
            return None
        return self._remux(path, self._fmt)


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
                spec = (cam.ip, cam.port, cam.username, cam.password, cam.quality, cam.source)
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
        writer = _HourWriter(cam, self._rec_dir)
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
                        now = datetime.now()
                        fmt = client.video_codec
                        if writer.due(now):
                            writer.close()
                            need_key = True
                        if need_key and not is_iframe:
                            continue
                        writer.ensure(now, fmt)
                        need_key = False
                        rec = client.h264_for_decode(payload) if is_iframe else payload
                        writer.write(rec)
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
            self._alive[cam.id] = False
            with self._lock:
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)
                if self._flags.get(cam.id) is flag:
                    self._flags.pop(cam.id, None)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
TASK_NAME = "V380StudioAutoRecord"
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
    return APP_DIR / "record_worker.py"


def _log(data_dir: Path, message: str) -> None:
    path = data_dir / "record_worker.log"
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
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


def _sync_logon_task(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        if enabled:
            subprocess.run(
                [
                    "schtasks", "/Create", "/F",
                    "/TN", TASK_NAME,
                    "/SC", "ONLOGON",
                    "/RL", "LIMITED",
                    "/TR", logon_task_command(),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        else:
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
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
            if int(time.time()) % 60 < 2:
                try:
                    remux_orphans(rec)
                except Exception as exc:
                    _log(root, f"orphan remux failed: {exc}")
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

    def sync(self, cameras: list[Camera]) -> None:
        self._enabled = {c.id for c in cameras if c.auto_record}
        _sync_logon_task(bool(self._enabled))
        if self._enabled:
            ensure_worker(self._data_dir, self._rec_dir)

    def watch(self) -> None:
        if not self._enabled:
            return
        now = time.time()
        if now - self._watch_at < 5.0:
            return
        self._watch_at = now
        if not is_worker_alive(self._data_dir):
            ensure_worker(self._data_dir, self._rec_dir)

    def on_video(self, cam: Camera, is_iframe: bool, payload: bytes, client) -> None:
        return None

    def is_recording(self, camera_id: int) -> bool:
        status = read_worker_status(self._data_dir)
        rec = status.get("recording") or {}
        if isinstance(rec, dict):
            return bool(rec.get(str(camera_id)) or rec.get(camera_id))
        return False

    def release_to_worker(self) -> None:
        if self._enabled:
            ensure_worker(self._data_dir, self._rec_dir)

    def stop_all(self) -> None:
        return None
