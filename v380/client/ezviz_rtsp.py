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
        # Camera can be online via EZVIZ app (ports 8000/9010) while RTSP :554 is off.
        sdk_ports = _probe_host_ports(host, (8000, 9010, 9020, 80, 443), timeout=0.35)
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
                vstream = container.streams.video[0]
                vstream.thread_type = "AUTO"
                astream = None
                try:
                    if container.streams.audio:
                        astream = container.streams.audio[0]
                except Exception:
                    astream = None
                for packet in container.demux(vstream, *([astream] if astream is not None else [])):
                    if stop.is_set() or self._stop.is_set():
                        break
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
                            img = frame.to_image().convert("RGB")
                            buf = io.BytesIO()
                            img.save(buf, format="JPEG", quality=78)
                            yield ("video", True, buf.getvalue())
                    except Exception:
                        continue
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


class EzvizRtspRecorder:
    """Record EZVIZ RTSP directly with FFmpeg (JPEG live path cannot use H264Recorder)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.active = False
        self.path: Path | None = None
        self._proc: subprocess.Popen | None = None

    def start(self, url: str) -> Path:
        self.stop()
        from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

        exe = _ffmpeg_exe()
        if not exe:
            raise RuntimeError("FFmpeg not found — cannot record EZVIZ RTSP")
        name = time.strftime("rec_%Y%m%d_%H%M%S.mp4")
        path = self.folder / name
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
                "-movflags",
                "+faststart",
                "-y",
                str(path),
            ],
            **kwargs,
        )
        self.path = path
        self.active = True
        return path

    def stop(self) -> Path | None:
        proc = self._proc
        path = self.path
        self._proc = None
        self.active = False
        self.path = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if path is not None and path.is_file() and path.stat().st_size > 1024:
            return path
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return None

