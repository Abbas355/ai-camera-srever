"""Ensure finished recordings start on a video keyframe.

EZVIZ RTSP is copy-muxed into per-minute files. Minute boundaries often cut mid-GOP,
so the first IDR can sit ~4–10s into the file. Players then jump there (looks like
"starts at 10 seconds"). Trim to the first keyframe and reset timestamps.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

_PTS_RE = re.compile(r"pts_time:(?P<t>-?\d+(?:\.\d+)?)")


def first_video_keyframe_sec(path: Path, *, timeout: int = 90) -> float | None:
    """Return PTS (seconds) of the first video I-frame, or None if unknown."""
    exe = _ffmpeg_exe()
    if not exe or not path.is_file():
        return None
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    kwargs.update(_win_hide_kwargs())
    try:
        proc = subprocess.run(
            [
                exe,
                "-hide_banner",
                "-i",
                str(path),
                "-vf",
                "select=eq(pict_type\\,I),showinfo",
                "-an",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            **kwargs,
        )
    except Exception:
        return None
    err = (proc.stderr or b"").decode("utf-8", "replace")
    m = _PTS_RE.search(err)
    if not m:
        return None
    try:
        return float(m.group("t"))
    except ValueError:
        return None


def remux_starting_at(
    src: Path,
    dst: Path,
    *,
    start_sec: float = 0.0,
    timeout: int = 180,
) -> bool:
    """Copy-remux ``src`` → ``dst``, optionally skipping to ``start_sec`` (keyframe)."""
    exe = _ffmpeg_exe()
    if not exe or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-analyzeduration",
        "50M",
        "-probesize",
        "50M",
    ]
    if start_sec > 0.05:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    cmd.extend(
        [
            "-i",
            str(src),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(dst),
        ]
    )
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    kwargs.update(_win_hide_kwargs())
    try:
        proc = subprocess.run(cmd, **kwargs)
    except Exception:
        return False
    return dst.is_file() and dst.stat().st_size > 1024 and proc.returncode == 0


def _marker_for(path: Path) -> Path:
    return path.with_name(path.name + ".kfok")


def ensure_starts_on_keyframe(path: Path, *, min_skew: float = 0.45) -> bool:
    """Rewrite ``path`` in place if the first video keyframe is late.

    Returns True when the file was rewritten.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 2048:
        return False
    # Still-growing / incomplete names — leave alone.
    lower = path.name.lower()
    if lower.endswith((".partial.mp4", ".tmp.mp4")) or lower.endswith(".ts"):
        return False

    marker = _marker_for(path)
    try:
        if marker.is_file() and marker.stat().st_mtime + 0.5 >= path.stat().st_mtime:
            return False
    except OSError:
        pass

    kf = first_video_keyframe_sec(path)
    if kf is None or kf <= min_skew:
        try:
            marker.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass
        return False

    tmp = path.with_name(path.stem + ".kftrim.tmp.mp4")
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    ok = remux_starting_at(path, tmp, start_sec=kf)
    if not ok:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        tmp.replace(path)
    except OSError:
        try:
            shutil.move(str(tmp), str(path))
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # Stale HLS would still start mid-GOP — drop it so the next ensure rebuilds.
    hls = path.with_name(path.stem + ".hls")
    if hls.is_dir():
        shutil.rmtree(hls, ignore_errors=True)
    try:
        marker.write_text(f"trimmed ss={kf:.3f}\n", encoding="utf-8")
    except OSError:
        pass
    return True
