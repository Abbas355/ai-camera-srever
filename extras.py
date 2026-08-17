"""Discover, cloud relay, listen audio, and local recording."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from v380_client import _ffmpeg_exe


@dataclass
class DeviceInfo:
    mac: str
    dev_id: str
    ip: str
    subnet: str = ""
    gateway: str = ""


def discover_devices(retries: int = 5) -> list[DeviceInfo]:
    devices: list[DeviceInfo] = []
    seen: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    try:
        sock.bind(("0.0.0.0", 10009))
        payload = b"NVDEVSEARCH^100"
        for _ in range(retries):
            sock.sendto(payload, ("255.255.255.255", 10008))
            deadline = time.time() + 0.25
            while time.time() < deadline:
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    break
                text = data.decode("ascii", errors="ignore")
                parts = text.split("^")
                if len(parts) < 13 or parts[0] != "NVDEVRESULT":
                    continue
                mac = parts[2]
                if mac in seen:
                    continue
                seen.add(mac)
                devices.append(
                    DeviceInfo(mac=mac, dev_id=parts[12], ip=parts[3], subnet=parts[4], gateway=parts[5])
                )
    finally:
        sock.close()
    return devices


def get_relay_ip(device_id: int) -> str:
    timestamp = int(time.time())
    platform = 10001
    base = f"dev_id={device_id}&platform={platform}&timestamp={timestamp}hsdata2022"
    sign = hashlib.sha1(base.encode("utf-8")).hexdigest()
    body = json.dumps(
        {"dev_id": device_id, "platform": platform, "timestamp": timestamp, "sign": sign}
    ).encode("utf-8")
    req = urllib.request.Request(
        "http://dispa1.av380.net:8001/api/v1/get_stream_server",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 2000:
        return ""
    for item in result.get("data") or []:
        ip = item.get("ip") or ""
        if ip and _tcp_ok(ip, 8800):
            return ip
    return ""


def _tcp_ok(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def alaw_to_pcm16(alaw: bytes) -> bytes:
    try:
        import audioop

        return audioop.alaw2lin(alaw, 2)
    except Exception:
        return _alaw_soft(alaw)


def _alaw_soft(alaw: bytes) -> bytes:
    out = bytearray()
    for b in alaw:
        b ^= 0x55
        sign = b & 0x80
        exp = (b >> 4) & 0x07
        mant = b & 0x0F
        dec = ((mant << 4) + 8) << exp if exp else (mant << 4) + 8
        if sign:
            dec = -dec
        out += struct.pack("<h", max(-32768, min(32767, dec)))
    return bytes(out)


class AlawPlayer:
    def __init__(self):
        self.enabled = False
        self._stream = None
        try:
            import sounddevice as sd

            self._sd = sd
            self._stream = sd.RawOutputStream(samplerate=8000, channels=1, dtype="int16", blocksize=0)
            self._stream.start()
        except Exception:
            self._sd = None

    def play(self, alaw: bytes) -> None:
        if not self.enabled or self._stream is None:
            return
        pcm = alaw_to_pcm16(alaw)
        try:
            self._stream.write(pcm)
        except Exception:
            pass

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class H264Recorder:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._path: Path | None = None
        self.active = False

    def start(self) -> Path:
        self.stop()
        name = datetime.now().strftime("rec_%Y%m%d_%H%M%S.h264")
        self._path = self.folder / name
        self._fh = self._path.open("wb")
        self.active = True
        return self._path

    def write(self, annexb: bytes) -> None:
        if self._fh is not None:
            self._fh.write(annexb)

    def stop(self) -> Path | None:
        path = self._path
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.active = False
        self._path = None
        if path and path.exists() and path.stat().st_size > 0:
            mp4 = path.with_suffix(".mp4")
            exe = _ffmpeg_exe()
            if exe:
                try:
                    subprocess.run(
                        [exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "h264", "-i", str(path), "-c", "copy", str(mp4)],
                        timeout=20,
                        check=False,
                    )
                    if mp4.exists() and mp4.stat().st_size > 0:
                        path.unlink(missing_ok=True)
                        return mp4
                except Exception:
                    pass
            return path
        return None
