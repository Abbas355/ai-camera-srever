"""Build HLS (YouTube-style short segments) next to a finished recording for smooth remote play."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

_HLS_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()

# ~4s segments — fewer pieces so background HLS builds finish much faster.
HLS_TIME = "4"


def hls_dir_for(src: Path) -> Path:
    return src.with_name(src.stem + ".hls")


def playlist_for(src: Path) -> Path:
    return hls_dir_for(src) / "index.m3u8"


def _is_partial(src: Path) -> bool:
    name = src.name.lower()
    return name.endswith(".partial.mp4") or name.endswith(".tmp.mp4") or name.endswith(".ts")


def hls_ready(src: Path) -> bool:
    playlist = playlist_for(src)
    if not playlist.is_file() or playlist.stat().st_size < 32:
        return False
    try:
        if playlist.stat().st_mtime + 0.5 < src.stat().st_mtime:
            return False  # source newer — rebuild
    except OSError:
        return False
    # At least one segment present
    return any(hls_dir_for(src).glob("seg_*.ts")) or any(hls_dir_for(src).glob("seg_*.m4s"))


def ensure_hls(src: Path | None, *, force: bool = False) -> Path | None:
    """Create / reuse VOD HLS beside ``src``. Returns playlist path or None."""
    if src is None:
        return None
    src = Path(src)
    if not src.is_file() or src.stat().st_size < 2048:
        return None
    # Mid-GOP minute cuts: rewrite so playback can start at t=0.
    if src.suffix.lower() == ".mp4" and not _is_partial(src):
        try:
            from v380.record.keyframe_fix import ensure_starts_on_keyframe

            if ensure_starts_on_keyframe(src):
                force = True
        except Exception:
            pass
    # Still-open partials: allow HLS build for smoother scrub; rebuild when file grows.
    playlist = playlist_for(src)
    if not force and hls_ready(src):
        return playlist

    exe = _ffmpeg_exe()
    if not exe:
        return None

    key = str(src.resolve())
    with _HLS_LOCK:
        if key in _INFLIGHT:
            # Another thread building — wait briefly for existing playlist
            pass
        else:
            _INFLIGHT.add(key)

    try:
        if not force and hls_ready(src):
            return playlist
        out_dir = hls_dir_for(src)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Clear old segments if rebuilding
        for old in out_dir.glob("seg_*"):
            try:
                old.unlink()
            except OSError:
                pass
        if playlist.is_file():
            try:
                playlist.unlink()
            except OSError:
                pass

        seg_pat = str(out_dir / "seg_%04d.ts")
        # Remux only (no re-encode) — fast on the recording HDD.
        # Dense keyframes come from recording (libx264 g≈2s) so 2s HLS cuts cleanly.
        cmd = [
            exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-analyzeduration",
            "10M",
            "-probesize",
            "10M",
            "-i",
            str(src),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            HLS_TIME,
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            seg_pat,
            str(playlist),
        ]
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "timeout": 600,
            "check": False,
        }
        kwargs.update(_win_hide_kwargs())
        proc = subprocess.run(cmd, **kwargs)
        if proc.returncode != 0 or not playlist.is_file():
            cmd2 = cmd.copy()
            i = cmd2.index("-c")
            cmd2[i : i + 2] = ["-c", "copy", "-bsf:v", "h264_mp4toannexb"]
            proc = subprocess.run(cmd2, **kwargs)
        if proc.returncode != 0 or not playlist.is_file():
            cmd3 = cmd.copy()
            i = cmd3.index("-c")
            cmd3[i : i + 2] = ["-c", "copy", "-bsf:v", "hevc_mp4toannexb"]
            proc = subprocess.run(cmd3, **kwargs)
        if not playlist.is_file() or playlist.stat().st_size < 32:
            return None
        return playlist
    except Exception:
        return None
    finally:
        with _HLS_LOCK:
            _INFLIGHT.discard(key)


def ensure_hls_async(src: Path | None) -> None:
    if src is None:
        return

    def work() -> None:
        try:
            ensure_hls(Path(src))
        except Exception:
            pass

    threading.Thread(target=work, daemon=True, name="hls-build").start()


def playlist_rel(src_rel: str) -> str:
    """Map ``cam/day/11-00-00.mp4`` → ``cam/day/11-00-00.hls/index.m3u8``."""
    p = Path(src_rel.replace("\\", "/"))
    return str(p.with_name(p.stem + ".hls") / "index.m3u8").replace("\\", "/")
