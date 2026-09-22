"""EZVIZ LAN RTSP helper — live JPEG frames via PyAV / FFmpeg."""

from __future__ import annotations

import io
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from v380.client.v380_client import _ffmpeg_exe


def normalize_brand(value: str | None) -> str:
    return "ezviz" if str(value or "").strip().lower() == "ezviz" else "v380"


def synthetic_device_id(ip: str, rtsp_url: str = "") -> str:
    raw = (rtsp_url or ip or "ezviz").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return ("ezviz_" + (cleaned or "cam"))[:48]


def build_rtsp_url(
    ip: str,
    password: str,
    *,
    username: str = "admin",
    port: int = 554,
    path: str = "/Streaming/Channels/101",
    existing: str = "",
) -> str:
    """Build a standard EZVIZ RTSP URL (verification code = password)."""
    existing = (existing or "").strip()
    if existing.lower().startswith("rtsp://"):
        return existing
    ip = (ip or "").strip()
    if not ip:
        return ""
    user = quote(username or "admin", safe="")
    code = quote(password or "", safe="")
    path = path if path.startswith("/") else f"/{path}"
    return f"rtsp://{user}:{code}@{ip}:{int(port or 554)}{path}"


def resolve_rtsp_url(cam) -> str:
    brand = normalize_brand(getattr(cam, "brand", None))
    if brand != "ezviz":
        return ""
    url = str(getattr(cam, "rtsp_url", "") or "").strip()
    if url.lower().startswith("rtsp://"):
        return url
    return build_rtsp_url(
        str(getattr(cam, "ip", "") or ""),
        str(getattr(cam, "password", "") or ""),
        username=str(getattr(cam, "username", "") or "admin"),
        port=int(getattr(cam, "port", 554) or 554),
        existing=url,
    )


def probe_rtsp(url: str, timeout: float = 6.0) -> dict:
    """Quick reachability check: TCP :554 then optional FFmpeg frame grab."""
    t0 = time.time()
    url = (url or "").strip()
    if not url.lower().startswith("rtsp://"):
        return {"ok": False, "reachable": False, "online": False, "error": "Missing RTSP URL"}
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = int(parsed.port or 554)
    if not host:
        return {"ok": False, "reachable": False, "online": False, "error": "Bad RTSP host"}
    try:
        with socket.create_connection((host, port), timeout=min(3.0, timeout)):
            pass
    except OSError as exc:
        return {
            "ok": False,
            "reachable": False,
            "online": False,
            "host": host,
            "port": port,
            "error": str(exc),
            "ms": int((time.time() - t0) * 1000),
        }
    jpeg = grab_jpeg(url, timeout=timeout)
    if jpeg:
        return {
            "ok": True,
            "reachable": True,
            "online": True,
            "host": host,
            "port": port,
            "ms": int((time.time() - t0) * 1000),
        }
    return {
        "ok": False,
        "reachable": True,
        "online": False,
        "host": host,
        "port": port,
        "error": "RTSP open failed — enable LAN Live View / RTSP in EZVIZ app",
        "ms": int((time.time() - t0) * 1000),
    }


def grab_jpeg(url: str, timeout: float = 8.0) -> bytes | None:
    """Grab one JPEG from RTSP using PyAV, then FFmpeg fallback."""
    try:
        import av

        container = av.open(
            url,
            options={
                "rtsp_transport": "tcp",
                "stimeout": str(int(timeout * 1_000_000)),
            },
            timeout=timeout,
        )
        try:
            for frame in container.decode(video=0):
                img = frame.to_image().convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                return buf.getvalue()
        finally:
            container.close()
    except Exception:
        pass
    exe = _ffmpeg_exe()
    if not exe:
        return None
    import subprocess
    import sys
    import tempfile

    dest = Path(tempfile.gettempdir()) / f"ezviz_probe_{int(time.time()*1000)}.jpg"
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": timeout + 2,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                url,
                "-frames:v",
                "1",
                "-q:v",
                "5",
                "-y",
                str(dest),
            ],
            check=False,
            **kwargs,
        )
        if dest.is_file() and dest.stat().st_size > 100:
            data = dest.read_bytes()
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            return data
    except Exception:
        pass
    return None


class EzvizRtspClient:
    """Minimal RTSP client producing JPEG frames for Hub preview / MJPEG live."""

    def __init__(self, url: str):
        self.url = (url or "").strip()
        self.video_codec = "jpeg"
        self.audio_codec = ""
        self._sps = None
        self._pps = None
        self._vps = None
        self._stop = threading.Event()

    def connect(self) -> None:
        if not self.url.lower().startswith("rtsp://"):
            raise RuntimeError("EZVIZ RTSP URL required")
        # Probe fails fast if RTSP is off.
        jpeg = grab_jpeg(self.url, timeout=6.0)
        if not jpeg:
            raise RuntimeError("Cannot open EZVIZ RTSP — enable RTSP in EZVIZ app (LAN Live View)")

    def close(self) -> None:
        self._stop.set()

    def h264_for_decode(self, payload: bytes) -> bytes:
        return payload

    def send_control(self, _name: str) -> bool:
        return False

    def iter_video_frames(self, stop_event: threading.Event | None = None):
        """Yield ('video', True, jpeg_bytes) continuously."""
        stop = stop_event or self._stop
        try:
            import av
        except Exception as exc:
            raise RuntimeError(f"PyAV required for EZVIZ: {exc}") from exc

        while not stop.is_set() and not self._stop.is_set():
            container = None
            try:
                container = av.open(
                    self.url,
                    options={
                        "rtsp_transport": "tcp",
                        "stimeout": "5000000",
                    },
                    timeout=8.0,
                )
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                for frame in container.decode(stream):
                    if stop.is_set() or self._stop.is_set():
                        break
                    img = frame.to_image().convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=78)
                    yield ("video", True, buf.getvalue())
            except Exception:
                if stop.is_set() or self._stop.is_set():
                    break
                time.sleep(1.5)
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass
