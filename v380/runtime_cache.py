"""Runtime caches for faster live + playback (durations, last preview frames)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from v380.paths import DATA_DIR

_DUR_FILE = DATA_DIR / "duration_cache.json"
_PREVIEW_DIR = DATA_DIR / "preview_cache"
_lock = threading.Lock()
_dur: dict[str, dict] = {}
_dur_loaded = False


def _load_dur() -> None:
    global _dur, _dur_loaded
    if _dur_loaded:
        return
    _dur_loaded = True
    try:
        if _DUR_FILE.is_file():
            raw = json.loads(_DUR_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _dur = raw
    except Exception:
        _dur = {}


def _save_dur() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _DUR_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_dur, indent=0), encoding="utf-8")
        tmp.replace(_DUR_FILE)
    except OSError:
        pass


def get_duration(path: Path | str) -> float | None:
    """Cached duration if file size/mtime still match."""
    p = Path(path)
    key = str(p)
    with _lock:
        _load_dur()
        rec = _dur.get(key)
    if not rec:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    if int(rec.get("size") or 0) != int(st.st_size):
        return None
    if abs(float(rec.get("mtime") or 0) - float(st.st_mtime)) > 0.5:
        return None
    try:
        d = float(rec.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def set_duration(path: Path | str, seconds: float) -> None:
    p = Path(path)
    if seconds <= 0:
        return
    try:
        st = p.stat()
    except OSError:
        return
    with _lock:
        _load_dur()
        _dur[str(p)] = {
            "duration": float(seconds),
            "size": int(st.st_size),
            "mtime": float(st.st_mtime),
            "at": time.time(),
        }
        # Cap cache size
        if len(_dur) > 2000:
            items = sorted(_dur.items(), key=lambda kv: float(kv[1].get("at") or 0))
            for k, _ in items[: len(_dur) - 1500]:
                _dur.pop(k, None)
        _save_dur()


def preview_path(camera_id: int) -> Path:
    _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    return _PREVIEW_DIR / f"{int(camera_id)}.jpg"


def save_preview(camera_id: int, jpeg: bytes) -> None:
    if not jpeg or len(jpeg) < 200:
        return
    try:
        path = preview_path(camera_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(jpeg)
        tmp.replace(path)
    except OSError:
        pass


def load_preview(camera_id: int, max_age_sec: float = 600.0) -> bytes | None:
    path = preview_path(camera_id)
    try:
        if not path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        if age > max_age_sec:
            return None
        data = path.read_bytes()
        return data if len(data) > 200 else None
    except OSError:
        return None
