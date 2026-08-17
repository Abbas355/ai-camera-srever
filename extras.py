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


# IMA ADPCM (WAV type 0x11) — V380 0x16 frames are 256-byte blocks at 8 kHz mono.
# Layout after the 16-byte V380 header: predictor s16le, step index u8, reserved u8, nibbles.
_IMA_INDEX = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
_IMA_STEP = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
)


def ima_adpcm_to_pcm16(data: bytes) -> bytes:
    out = bytearray()
    for off in range(0, len(data), 256):
        block = data[off : off + 256]
        if len(block) < 8:
            break
        pred = struct.unpack_from("<h", block, 0)[0]
        index = block[2]
        if index > 88:
            continue
        step = _IMA_STEP[index]
        out += struct.pack("<h", pred)
        for b in block[4:]:
            for nibble in (b & 0x0F, b >> 4):
                diff = step >> 3
                if nibble & 4:
                    diff += step
                if nibble & 2:
                    diff += step >> 1
                if nibble & 1:
                    diff += step >> 2
                pred = pred - diff if nibble & 8 else pred + diff
                pred = max(-32767, min(32767, pred))
                index = max(0, min(88, index + _IMA_INDEX[nibble]))
                step = _IMA_STEP[index]
                out += struct.pack("<h", pred)
    return bytes(out)


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

    def play(self, data: bytes, codec: str = "alaw") -> None:
        if not self.enabled or self._stream is None:
            return
        pcm = ima_adpcm_to_pcm16(data) if codec == "ima" else alaw_to_pcm16(data)
        if not pcm:
            return
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
        self._fmt = "h264"

    def start(self, fmt: str = "h264") -> Path:
        self.stop()
        self._fmt = "hevc" if fmt == "hevc" else "h264"
        ext = "h265" if self._fmt == "hevc" else "h264"
        name = datetime.now().strftime(f"rec_%Y%m%d_%H%M%S.{ext}")
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
        return remux_annexb(path, self._fmt)


def remux_annexb(path: Path | None, fmt: str, timeout: int = 180) -> Path | None:
    if path is None or not path.exists() or path.stat().st_size <= 0:
        if path is not None:
            path.unlink(missing_ok=True)
        return None
    mp4 = path.with_suffix(".mp4")
    exe = _ffmpeg_exe()
    if exe:
        try:
            subprocess.run(
                [exe, "-y", "-hide_banner", "-loglevel", "error", "-f", fmt, "-i", str(path), "-c", "copy", str(mp4)],
                timeout=timeout,
                check=False,
            )
            if mp4.exists() and mp4.stat().st_size > 0:
                path.unlink(missing_ok=True)
                return mp4
        except Exception:
            pass
    return path
