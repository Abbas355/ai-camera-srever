"""Small JPEG preview for a recording, cached beside the source file."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

_SEM = threading.Semaphore(2)
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def thumb_path_for(src: Path) -> Path:
    return src.with_name(src.stem + ".thumb.jpg")


def _file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _fresh(src: Path, thumb: Path) -> bool:
    try:
        if not thumb.is_file() or thumb.stat().st_size < 200:
            return False
        # Open .ts keeps growing — refresh the picture about twice a minute.
        if src.suffix.lower() == ".ts":
            return time.time() - thumb.stat().st_mtime < 30
        return thumb.stat().st_mtime + 0.5 >= src.stat().st_mtime
    except OSError:
        return False


def ensure_thumb(src: Path) -> Path | None:
    """Return a cached preview JPEG, creating it with ffmpeg when needed."""
    src = Path(src)
    if not src.is_file() or src.stat().st_size < 2048:
        return None
    if src.suffix.lower() not in {".mp4", ".ts", ".h264", ".h265"}:
        return None
    thumb = thumb_path_for(src)
    if _fresh(src, thumb):
        return thumb

    lock = _file_lock(thumb)
    with lock:
        if _fresh(src, thumb):
            return thumb
        exe = _ffmpeg_exe()
        if not exe:
            return thumb if thumb.is_file() else None
        with _SEM:
            cmd = [
                exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "1",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                "scale=160:-2",
                "-q:v",
                "7",
                str(thumb),
            ]
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 25,
                "check": False,
            }
            kwargs.update(_win_hide_kwargs())
            try:
                subprocess.run(cmd, **kwargs)
            except Exception:
                return thumb if thumb.is_file() else None
        if thumb.is_file() and thumb.stat().st_size > 200:
            return thumb
        return None
