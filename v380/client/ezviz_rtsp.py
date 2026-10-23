"""EZVIZ LAN RTSP helper — discover, live JPEG frames via PyAV / FFmpeg."""

from __future__ import annotations

import concurrent.futures
import io
import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

from v380.client.v380_client import _ffmpeg_exe


def normalize_brand(value: str | None) -> str:
    return "ezviz" if str(value or "").strip().lower() == "ezviz" else "v380"


def synthetic_device_id(ip: str, rtsp_url: str = "") -> str:
    raw = (rtsp_url or ip or "ezviz").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return ("ezviz_" + (cleaned or "cam"))[:48]


@dataclass
class EzvizDeviceInfo:
    mac: str
    dev_id: str
    ip: str
    brand: str = "ezviz"
    source: str = "rtsp"  # onvif | rtsp | sdk
    rtsp_ready: bool = False


def _local_ipv4s() -> list[str]:
    ips: list[str] = []
    seen: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                ips.append(ip)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        if ip and not ip.startswith("127.") and ip not in seen:
            ips.append(ip)
    except OSError:
        pass
    return ips


def _tcp_open(ip: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_host_ports(ip: str, ports: tuple[int, ...] = (554, 8000, 8443, 9010, 9020), timeout: float = 0.22) -> set[int]:
    open_ports: set[int] = set()
    for port in ports:
        if _tcp_open(ip, port, timeout):
            open_ports.add(port)
    return open_ports


def _looks_like_ezviz(open_ports: set[int]) -> bool:
    """Require a strong EZVIZ/Hik signature — lone :8000 is too common (false positives)."""
    if 554 in open_ports:
        return True
    # Local SDK ports used by the official EZVIZ app.
    if 9010 in open_ports or 9020 in open_ports:
        return True
    # Hik/EZVIZ web + SDK pair (not :8000 alone).
    if 8000 in open_ports and 8443 in open_ports:
        return True
    return False


def _onvif_probe(timeout: float = 2.5) -> list[EzvizDeviceInfo]:
    """WS-Discovery multicast — finds ONVIF cameras (many EZVIZ models)."""
    msg_id = f"uuid:{uuid.uuid4()}"
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "<e:Header>"
        f"<w:MessageID>{msg_id}</w:MessageID>"
        "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header>"
        "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
        "</e:Envelope>"
    ).encode("utf-8")
    found: dict[str, EzvizDeviceInfo] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.35)
    try:
        sock.bind(("", 0))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass
        for _ in range(3):
            try:
                sock.sendto(probe, ("239.255.255.250", 3702))
            except OSError:
                break
            deadline = time.time() + (timeout / 3.0)
            while time.time() < deadline:
                try:
                    data, _addr = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                for ip, mac in _parse_onvif_match(data):
                    if ip in found:
                        continue
                    found[ip] = EzvizDeviceInfo(
                        mac=mac,
                        dev_id=synthetic_device_id(ip),
                        ip=ip,
                        source="onvif",
                        rtsp_ready=True,
                    )
    finally:
        sock.close()
    return list(found.values())


def _parse_onvif_match(data: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        text = data.decode("utf-8", errors="ignore")
        root = ET.fromstring(text)
    except Exception:
        return out
    mac = ""
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag == "scopes" and el.text:
            m = re.search(r"MAC/([0-9A-Fa-f:.\-]+)", el.text)
            if m:
                mac = m.group(1)
        if tag == "xaddrs" and el.text:
            for url in el.text.split():
                host = urlparse(url.strip()).hostname
                if host and re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
                    out.append((host, mac))
    if not out:
        for host in re.findall(r"https?://(\d+\.\d+\.\d+\.\d+)", text):
            out.append((host, mac))
    return out


def _scan_ezviz_subnet(timeout: float = 10.0) -> list[EzvizDeviceInfo]:
    """Scan local /24s for EZVIZ LAN ports (554 RTSP and/or 8000/9010 SDK)."""
    hosts: list[str] = []
    seen_net: set[str] = set()
    local_ips = set(_local_ipv4s())
    has_real_lan = any(not ip.startswith("169.") for ip in local_ips)
    for ip in local_ips:
        parts = ip.split(".")
        if len(parts) != 4:
            continue
        if parts[0] == "169" and has_real_lan:
            continue
        net = f"{parts[0]}.{parts[1]}.{parts[2]}"
        if net in seen_net:
            continue
        seen_net.add(net)
        for last in range(1, 255):
            host = f"{net}.{last}"
            if host in local_ips:
                continue
            hosts.append(host)
    if not hosts:
        return []

    # Pass 1: cheap probe on SDK port 8000 + RTSP 554 (most EZVIZ hit one of these).
    candidates: list[str] = []
    workers = min(128, max(24, len(hosts) // 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_tcp_open, h, port, 0.2): (h, port)
            for h in hosts
            for port in (554, 8000, 9010)
        }
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=max(1.0, timeout)):
                host, _port = futs[fut]
                try:
                    if fut.result() and host not in candidates:
                        candidates.append(host)
                except Exception:
                    continue
        except concurrent.futures.TimeoutError:
            pass

    found: list[EzvizDeviceInfo] = []
    for host in candidates:
        ports = _probe_host_ports(host)
        if not _looks_like_ezviz(ports):
            continue
        found.append(
            EzvizDeviceInfo(
                mac="",
                dev_id=synthetic_device_id(host),
                ip=host,
                source="rtsp" if 554 in ports else "sdk",
                rtsp_ready=554 in ports,
            )
        )
    return found


def discover_ezviz_devices(*, onvif: bool = True, rtsp_scan: bool = True) -> list[EzvizDeviceInfo]:
    """Find EZVIZ cameras on the LAN (RTSP and/or Hik/EZVIZ SDK ports)."""
    by_ip: dict[str, EzvizDeviceInfo] = {}
    if onvif:
        try:
            for d in _onvif_probe():
                by_ip[d.ip] = d
        except Exception:
            pass
    if rtsp_scan:
        try:
            for d in _scan_ezviz_subnet():
                prev = by_ip.get(d.ip)
                if prev is None:
                    by_ip[d.ip] = d
                else:
                    if not prev.mac and d.mac:
                        prev.mac = d.mac
                    prev.rtsp_ready = prev.rtsp_ready or d.rtsp_ready
                    if d.source == "rtsp":
                        prev.source = "rtsp"
        except Exception:
            pass
    return list(by_ip.values())


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


def _with_channel(url: str, channel: int) -> str:
    """Hik/EZVIZ: 101 = main, 102 = sub. Swap path so live + record can share the camera."""
    import re

    url = (url or "").strip()
    if not url:
        return ""
    ch = int(channel)
    if re.search(r"/Channels/\d+", url):
        return re.sub(r"/Channels/\d+", f"/Channels/{ch}", url)
    if url.endswith("/"):
        return f"{url}Streaming/Channels/{ch}"
    return f"{url}/Streaming/Channels/{ch}"


def rtsp_open_options(*, timeout_sec: float = 8.0, low_delay: bool = True) -> dict:
    """Reliable RTSP open options for EZVIZ (often HEVC).

    Too-small probesize/analyzeduration yields \"unspecified size\" / no frames
    even when TCP :554 is fine — that shows up as a black Live video panel.
    """
    opts = {
        "rtsp_transport": "tcp",
        "stimeout": str(int(max(3.0, timeout_sec) * 1_000_000)),
        # HEVC needs a larger probe window than H.264.
        "probesize": "5000000",
        "analyzeduration": "5000000",
    }
    if low_delay:
        opts["fflags"] = "nobuffer"
        opts["flags"] = "low_delay"
        opts["max_delay"] = "1000000"
    return opts


def resolve_rtsp_url(cam, *, stream: str = "main") -> str:
    """Resolve RTSP URL. stream='sub' uses channel 102 for live (keeps 101 free for record)."""
    brand = normalize_brand(getattr(cam, "brand", None))
    if brand != "ezviz":
        return ""
    url = str(getattr(cam, "rtsp_url", "") or "").strip()
    if url.lower().startswith("rtsp://"):
        out = url
    else:
        out = build_rtsp_url(
            str(getattr(cam, "ip", "") or ""),
            str(getattr(cam, "password", "") or ""),
            username=str(getattr(cam, "username", "") or "admin"),
            port=int(getattr(cam, "port", 554) or 554),
            existing=url,
        )
    if not out:
        return ""
    if str(stream or "main").lower() in {"sub", "substream", "102"}:
        return _with_channel(out, 102)
    return _with_channel(out, 101) if "/Channels/" not in out else out


def probe_rtsp(url: str, timeout: float = 2.0, *, grab: bool = False) -> dict:
    """Fast reachability check.

    Default is TCP-only on :554 (milliseconds). Opening a full RTSP session for
    status checks fights recording/live and often takes 6–10s (what users see as
    "Online · 9720 ms"). Use grab=True only when verifying credentials on add/edit.
    """
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
        with socket.create_connection((host, port), timeout=min(1.5, timeout)):
            pass
    except OSError as exc:
        # Camera can be online via EZVIZ app (ports 8000/9010) while RTSP :554 is off.
        sdk_ports = _probe_host_ports(host, (8000, 9010, 9020, 80, 443), timeout=0.25)
        if sdk_ports:
            return {
                "ok": False,
                "reachable": True,
                "online": False,
                "host": host,
                "port": port,
                "sdk_ports": sorted(sdk_ports),
                "error": (
                    "Camera is online (EZVIZ app OK) but RTSP port "
                    f"{port} is closed. Enable RTSP: EZVIZ app → Settings → "
                    "LAN Live View → camera → Local Server Settings → RTSP"
                ),
                "ms": int((time.time() - t0) * 1000),
            }
        return {
            "ok": False,
            "reachable": False,
            "online": False,
            "host": host,
            "port": port,
            "error": str(exc),
            "ms": int((time.time() - t0) * 1000),
        }
    if grab:
        jpeg = grab_jpeg(url, timeout=min(4.0, max(2.0, timeout)))
        if jpeg:
            return {
                "ok": True,
                "reachable": True,
                "online": True,
                "host": host,
                "port": port,
                "ms": int((time.time() - t0) * 1000),
            }
        # Port open but grab failed — usually max concurrent RTSP sessions.
        return {
            "ok": True,
            "reachable": True,
            "online": True,
            "host": host,
            "port": port,
            "busy": True,
            "ms": int((time.time() - t0) * 1000),
        }
    # TCP :554 open = online for status / home mosaic (do not steal an RTSP slot).
    return {
        "ok": True,
        "reachable": True,
        "online": True,
        "host": host,
        "port": port,
        "ms": int((time.time() - t0) * 1000),
    }


def grab_jpeg(url: str, timeout: float = 4.0) -> bytes | None:
    """Grab one JPEG from RTSP using PyAV, then FFmpeg fallback. Single attempt (fast fail)."""
    try:
        import av

        container = av.open(
            url,
            options=rtsp_open_options(timeout_sec=timeout, low_delay=True),
            timeout=timeout,
        )
        try:
            for frame in container.decode(video=0):
                img = frame.to_image().convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return buf.getvalue()
        finally:
            try:
                container.close()
            except Exception:
                pass
            time.sleep(0.25)
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
    """Minimal RTSP client producing JPEG frames (+ optional PCM audio) for Studio."""

    def __init__(self, url: str, *, ip: str = "", username: str = "admin", password: str = ""):
        self.url = (url or "").strip()
        self.ip = (ip or "").strip()
        self.username = username or "admin"
        self.password = password or ""
        self.video_codec = "jpeg"
        self.audio_codec = "pcm"
        self._sps = None
        self._pps = None
        self._vps = None
        self._stop = threading.Event()
        self._audio_cb = None  # callable[[bytes], None] raw s16le mono/stereo

    def set_audio_callback(self, cb) -> None:
        self._audio_cb = cb

    def connect(self) -> None:
        """Light check only — do not open a full RTSP session here.

        Opening RTSP in connect() then again in iter_video_frames() burns the
        camera's single/dual session limit and makes the second open fail with
        Invalid data / 'RTSP open failed'.
        """
        if not self.url.lower().startswith("rtsp://"):
            raise RuntimeError("EZVIZ RTSP URL required")
        parsed = urlparse(self.url)
        host = parsed.hostname or self.ip
        port = int(parsed.port or 554)
        if not host:
            raise RuntimeError("Bad RTSP host")
        try:
            with socket.create_connection((host, port), timeout=3.0):
                pass
        except OSError as exc:
            raise RuntimeError(
                f"RTSP port {port} closed — enable LAN Live View / RTSP in EZVIZ app"
            ) from exc
        self._stop.clear()

    def close(self) -> None:
        self._stop.set()

    def h264_for_decode(self, payload: bytes) -> bytes:
        return payload

    def send_control(self, name: str) -> bool:
        from v380.client.ezviz_control import send_ptz

        host = self.ip
        if not host:
            try:
                host = urlparse(self.url).hostname or ""
            except Exception:
                host = ""
        return send_ptz(host, self.username, self.password, name)

    def iter_video_frames(self, stop_event: threading.Event | None = None):
        """Yield ('video', True, jpeg_bytes) continuously; audio goes to callback if set."""
        stop = stop_event or self._stop
        try:
            import av
            import numpy as np
        except Exception as exc:
            raise RuntimeError(f"PyAV required for EZVIZ: {exc}") from exc

        backoff = 2.0
        while not stop.is_set() and not self._stop.is_set():
            container = None
            got_frame = False
            try:
                container = av.open(
                    self.url,
                    options=rtsp_open_options(timeout_sec=12.0, low_delay=True),
                    timeout=12.0,
                )
                vstream = container.streams.video[0]
                vstream.thread_type = "AUTO"
                astream = None
                try:
                    if container.streams.audio:
                        astream = container.streams.audio[0]
                except Exception:
                    astream = None
                first_deadline = time.time() + 12.0
                for packet in container.demux(vstream, *([astream] if astream is not None else [])):
                    if stop.is_set() or self._stop.is_set():
                        break
                    if not got_frame and time.time() > first_deadline:
                        # Camera accepted RTSP but sent no decodable video (common after
                        # connection storms / HEVC “unspecified size”). Back off hard.
                        raise RuntimeError(
                            "Camera opened RTSP but sent no video frames — "
                            "reboot the camera or check LAN Live View in EZVIZ app"
                        )
                    if packet.stream.type == "audio":
                        cb = self._audio_cb
                        if cb is None:
                            continue
                        try:
                            rate = int(getattr(packet.stream, "rate", 0) or 0)
                            for frame in packet.decode():
                                arr = frame.to_ndarray()
                                fr = int(getattr(frame, "sample_rate", 0) or rate or 8000)
                                if getattr(arr, "dtype", None) is not None and arr.dtype.kind == "f":
                                    arr = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
                                else:
                                    arr = np.asarray(arr, dtype=np.int16)
                                if arr.ndim > 1:
                                    axis = 0 if arr.shape[0] <= 8 else -1
                                    arr = arr.mean(axis=axis).astype(np.int16)
                                pcm = arr.tobytes()
                                if pcm:
                                    try:
                                        cb(pcm, fr)
                                    except TypeError:
                                        cb(pcm)
                        except Exception:
                            pass
                        continue
                    try:
                        for frame in packet.decode():
                            if stop.is_set() or self._stop.is_set():
                                break
                            jpeg = _ezviz_frame_to_jpeg(frame)
                            if jpeg:
                                got_frame = True
                                backoff = 2.0
                                yield ("video", True, jpeg)
                    except Exception:
                        continue
                if not got_frame and not stop.is_set() and not self._stop.is_set():
                    raise RuntimeError(
                        "Camera opened RTSP but sent no video frames — "
                        "reboot the camera or check LAN Live View in EZVIZ app"
                    )
            except Exception as exc:
                if stop.is_set() or self._stop.is_set():
                    break
                # Surface the reason once so UI status / logs are not silent black screens.
                print("[ezviz]", exc)
                time.sleep(backoff)
                backoff = min(30.0, backoff * 1.5)
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass
                    # Give the camera time to TEARDOWN before the next open.
                    time.sleep(1.0)


def _ezviz_frame_to_jpeg(frame, max_width: int = 1280, quality: int = 70) -> bytes | None:
    """Fast RGB→JPEG for live. Prefer OpenCV; fall back to PIL."""
    try:
        import cv2
        import numpy as np

        arr = frame.to_ndarray(format="rgb24")
        h, w = arr.shape[:2]
        if w > max_width > 0:
            nh = max(1, int(h * (max_width / w)))
            arr = cv2.resize(arr, (max_width, nh), interpolation=cv2.INTER_AREA)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if ok:
            return enc.tobytes()
    except Exception:
        pass
    try:
        img = frame.to_image().convert("RGB")
        if max_width > 0 and img.width > max_width:
            nh = max(1, int(img.height * (max_width / img.width)))
            img = img.resize((max_width, nh))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        return buf.getvalue()
    except Exception:
        return None


class EzvizRtspRecorder:
    """Record EZVIZ RTSP with FFmpeg via MPEG-TS then finalize to playable MP4."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.active = False
        self.path: Path | None = None
        self._ts_path: Path | None = None
        self._proc: subprocess.Popen | None = None

    def start(self, url: str) -> Path:
        self.stop()
        from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

        exe = _ffmpeg_exe()
        if not exe:
            raise RuntimeError("FFmpeg not found — cannot record EZVIZ RTSP")
        stamp = time.strftime("rec_%Y%m%d_%H%M%S")
        ts_path = self.folder / f"{stamp}.ts"
        mp4_path = self.folder / f"{stamp}.mp4"
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        kwargs.update(_win_hide_kwargs())
        self._proc = subprocess.Popen(
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
                "-f",
                "mpegts",
                "-y",
                str(ts_path),
            ],
            **kwargs,
        )
        self._ts_path = ts_path
        self.path = mp4_path
        self.active = True
        return mp4_path

    def stop(self) -> Path | None:
        from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

        proc = self._proc
        ts_path = self._ts_path
        mp4_path = self.path
        self._proc = None
        self._ts_path = None
        self.active = False
        self.path = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if ts_path is None or mp4_path is None:
            return None
        if not ts_path.is_file() or ts_path.stat().st_size < 2048:
            try:
                ts_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        exe = _ffmpeg_exe()
        if not exe:
            return ts_path
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 180,
            "check": False,
        }
        kwargs.update(_win_hide_kwargs())
        try:
            subprocess.run(
                [
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
                    str(ts_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    str(mp4_path),
                ],
                **kwargs,
            )
        except Exception:
            return ts_path if ts_path.is_file() else None
        if mp4_path.is_file() and mp4_path.stat().st_size > 1024:
            try:
                ts_path.unlink(missing_ok=True)
            except OSError:
                pass
            return mp4_path
        return ts_path if ts_path.is_file() else None

