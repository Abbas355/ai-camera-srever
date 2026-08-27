"""Discover, cloud relay, listen audio, and local recording."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import socket
import struct
import wave
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs
from v380.paths import AUDIO_DIR


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


class ImaEncoder:
    """IMA-ADPCM encoder matching V380 speak packets (256-byte blocks)."""

    def __init__(self) -> None:
        self.pred = 0
        self.index = 0

    def reset(self) -> None:
        self.pred = 0
        self.index = 0

    def _nibble(self, sample: int) -> int:
        delta = sample - self.pred
        value = 0
        if delta < 0:
            value = 8
            delta = -delta
        step = _IMA_STEP[self.index]
        diff = step >> 3
        if delta > step:
            value |= 4
            delta -= step
            diff += step
        step >>= 1
        if delta > step:
            value |= 2
            delta -= step
            diff += step
        step >>= 1
        if delta > step:
            value |= 1
            diff += step
        self.pred += -diff if value & 8 else diff
        self.pred = max(-32767, min(32767, self.pred))
        self.index = max(0, min(88, self.index + _IMA_INDEX[value & 7]))
        return value

    def encode_block(self, pcm: bytes) -> bytes:
        if len(pcm) < 2:
            return b"\x00" * 256
        samples = [struct.unpack_from("<h", pcm, i)[0] for i in range(0, len(pcm) - 1, 2)]
        if not samples:
            return b"\x00" * 256
        self._nibble(samples[0])
        out = bytearray(struct.pack("<hBB", samples[0], self.index, 0))
        i = 1
        while len(out) < 256:
            n0 = self._nibble(samples[i]) if i < len(samples) else 0
            n1 = self._nibble(samples[i + 1]) if i + 1 < len(samples) else 0
            out.append((n1 << 4) | n0)
            i += 2
        return bytes(out[:256])


def alaw_to_pcm16(alaw: bytes) -> bytes:
    try:
        import audioop

        return audioop.alaw2lin(alaw, 2)
    except Exception:
        return _alaw_soft(alaw)


def pcm16_to_alaw(pcm: bytes) -> bytes:
    try:
        import audioop

        return audioop.lin2alaw(pcm, 2)
    except Exception:
        return _pcm_to_alaw_soft(pcm)


def _pcm_to_alaw_soft(pcm: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(pcm) - 1, 2):
        sample = struct.unpack_from("<h", pcm, i)[0]
        sign = 0x80 if sample < 0 else 0
        mag = min(abs(sample), 32767)
        if mag >= 256:
            exp = 7
            thresh = 256 << 7
            while exp > 0 and mag < thresh:
                exp -= 1
                thresh >>= 1
            mant = (mag >> (exp + 3)) & 0x0F
        else:
            exp = 0
            mant = mag >> 4
        out.append(((sign | (exp << 4) | mant) ^ 0x55) & 0xFF)
    return bytes(out)


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
        self._sd = None

    def _ensure(self) -> bool:
        if self._stream is not None:
            return True
        try:
            import sounddevice as sd

            self._sd = sd
            self._stream = sd.RawOutputStream(samplerate=8000, channels=1, dtype="int16", blocksize=0)
            self._stream.start()
            return True
        except Exception:
            self._sd = None
            self._stream = None
            return False

    def play(self, data: bytes, codec: str = "alaw") -> None:
        if not self.enabled:
            return
        if not self._ensure():
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


class _SharedMic:
    """One process-wide mic so Windows does not keep asking permission."""

    def __init__(self):
        self.stream = None
        self.rate = 8000
        self._lock = threading.Lock()
        self._listeners: list = []
        self._sd = None

    def add(self, talker) -> None:
        with self._lock:
            if talker not in self._listeners:
                self._listeners.append(talker)
            self._open()

    def remove(self, talker) -> None:
        with self._lock:
            if talker in self._listeners:
                self._listeners.remove(talker)

    def _cb(self, indata, frames, time_info, status) -> None:
        for talker in list(self._listeners):
            try:
                talker._on_mic(indata, frames, time_info, status)
            except Exception:
                pass

    def _open(self) -> None:
        if self.stream is not None:
            return
        import sounddevice as sd

        self._sd = sd
        last_err: Exception | None = None
        for rate in (8000, 16000, 44100, 48000):
            try:
                stream = sd.RawInputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="int16",
                    blocksize=max(160, int(rate * 0.02)),
                    callback=self._cb,
                )
                stream.start()
                self.stream = stream
                self.rate = rate
                return
            except Exception as exc:
                last_err = exc
        raise last_err or RuntimeError("microphone blocked")


_SHARED_MIC = _SharedMic()


class Talker:
    """PC mic → IMA-ADPCM → camera talk channel."""

    def __init__(self):
        self.enabled = False
        self.error = ""
        self._stream = None
        self._client = None
        self._pcm = bytearray()
        self._ima = ImaEncoder()
        self._out: queue.Queue[bytes] = queue.Queue(maxsize=40)
        self._sender: threading.Thread | None = None
        self._send_stop = threading.Event()
        self._rate = 8000
        try:
            import sounddevice as sd

            self._sd = sd
        except Exception:
            self._sd = None

    @staticmethod
    def open_mic_settings() -> None:
        if sys.platform == "win32":
            os.startfile("ms-settings:privacy-microphone")

    def attach(self, client) -> None:
        self._client = client

    def prepare(self) -> None:
        """Open the mic once so Windows only prompts on first use."""
        try:
            self._open_mic()
        except Exception:
            pass

    def _sender_loop(self) -> None:
        while not self._send_stop.is_set():
            try:
                ima = self._out.get(timeout=0.2)
            except queue.Empty:
                continue
            client = self._client
            if client is None or not self.enabled:
                continue
            try:
                client.send_talk_audio(ima)
            except Exception:
                pass

    def _ensure_sender(self) -> None:
        if self._sender is not None and self._sender.is_alive():
            return
        self._send_stop.clear()
        self._sender = threading.Thread(target=self._sender_loop, daemon=True, name="talk-send")
        self._sender.start()

    def _open_mic(self) -> None:
        if self._sd is None:
            raise RuntimeError("sounddevice is not installed")
        _SHARED_MIC.add(self)
        self._rate = _SHARED_MIC.rate
        self._stream = _SHARED_MIC.stream

    def _to_8k(self, pcm: bytes) -> bytes:
        if self._rate == 8000:
            return pcm
        import array

        src = array.array("h")
        src.frombytes(pcm)
        if not src:
            return b""
        step = self._rate / 8000.0
        out = array.array("h")
        i = 0.0
        n = len(src)
        while int(i) < n:
            out.append(src[int(i)])
            i += step
        return out.tobytes()

    def _on_mic(self, indata, frames, time_info, status) -> None:
        if not self.enabled or self._client is None:
            return
        try:
            self._pcm.extend(self._to_8k(bytes(indata)))
            need = 1010
            while len(self._pcm) >= need:
                block = bytes(self._pcm[:need])
                del self._pcm[:need]
                ima = self._ima.encode_block(block)
                try:
                    self._out.put_nowait(ima)
                except queue.Full:
                    try:
                        self._out.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._out.put_nowait(ima)
                    except queue.Full:
                        pass
        except Exception:
            pass

    def start(self) -> bool:
        self.error = ""
        if self._sd is None:
            self.error = "sounddevice is not installed"
            return False
        if self._client is None:
            self.error = "not connected"
            return False
        ok = False
        for _ in range(3):
            try:
                ok = bool(self._client.start_talk())
            except Exception:
                ok = False
            if ok:
                break
            time.sleep(0.05)
        if not ok:
            self.error = "camera rejected talk"
            return False
        try:
            self._open_mic()
            self._ensure_sender()
            self._pcm.clear()
            self._ima.reset()
            self.enabled = True
            return True
        except Exception as exc:
            self.error = str(exc) or "microphone blocked"
            try:
                self._client.stop_talk()
            except Exception:
                pass
            return False

    def stop(self) -> None:
        self.enabled = False
        if self._client is not None:
            try:
                self._client.stop_talk()
            except Exception:
                pass

    def close(self) -> None:
        self.stop()
        self._send_stop.set()
        _SHARED_MIC.remove(self)
        self._stream = None


ALERT_WAV = AUDIO_DIR / "alert.wav"


class AlertSiren:
    """Play alert.wav (or a beep) on the camera speaker via the talk channel."""

    def __init__(self) -> None:
        self.active = False
        self.using_file = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        self._own_talk = False

    def start(self, client) -> bool:
        self.stop()
        self._client = client
        self.using_file = ALERT_WAV.is_file()
        self._own_talk = not getattr(client, "_talk_on", False)
        if self._own_talk and not client.start_talk():
            return False
        for name in ("alert_on", "alert2_on"):
            try:
                client.send_control(name)
            except Exception:
                pass
        self._stop.clear()
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="v380-alert")
        self._thread.start()
        return True

    def stop(self) -> None:
        self.active = False
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        client = self._client
        if client is not None:
            for name in ("alert_off", "alert2_off"):
                try:
                    client.send_control(name)
                except Exception:
                    pass
            if self._own_talk:
                try:
                    client.stop_talk()
                except Exception:
                    pass
        self._own_talk = False

    def _play_pcm(self, pcm: bytes) -> None:
        ima = ImaEncoder()
        need = 1010
        off = 0
        while not self._stop.is_set() and off < len(pcm):
            chunk = pcm[off : off + need]
            if len(chunk) < need:
                chunk = chunk + b"\x00" * (need - len(chunk))
            off += need
            try:
                if self._client is not None:
                    self._client.send_talk_audio(ima.encode_block(chunk))
            except Exception:
                break
            if self._stop.wait(505 / 8000):
                break

    def _run(self) -> None:
        pcm = _load_alert_pcm()
        if pcm:
            self.using_file = True
            self._play_pcm(pcm)
        else:
            self.using_file = False
            ima = ImaEncoder()
            sample = 0
            while not self._stop.is_set():
                block = _siren_pcm(sample, 505)
                sample += 505
                try:
                    if self._client is not None:
                        self._client.send_talk_audio(ima.encode_block(block))
                except Exception:
                    break
                if self._stop.wait(505 / 8000):
                    break
        self.active = False


def _load_alert_pcm() -> bytes | None:
    path = ALERT_WAV
    if not path.is_file():
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return None
    if width != 2 or not raw:
        return None
    if channels == 2:
        out = bytearray()
        for i in range(0, len(raw) - 3, 4):
            left = struct.unpack_from("<h", raw, i)[0]
            right = struct.unpack_from("<h", raw, i + 2)[0]
            out += struct.pack("<h", (left + right) // 2)
        raw = bytes(out)
        channels = 1
    if channels != 1:
        return None
    if rate != 8000:
        try:
            import audioop

            raw, _ = audioop.ratecv(raw, 2, 1, rate, 8000, None)
        except Exception:
            return None
    return raw


def _siren_pcm(start: int, count: int) -> bytes:
    out = bytearray()
    for i in range(count):
        n = start + i
        freq = 880.0 if (n // 800) % 2 == 0 else 1175.0
        val = int(24000 * math.sin(2 * math.pi * freq * n / 8000.0))
        out += struct.pack("<h", max(-32767, min(32767, val)))
    return bytes(out)


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
            kwargs = {
                "timeout": timeout,
                "check": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            kwargs.update(_win_hide_kwargs())
            subprocess.run(
                [exe, "-y", "-hide_banner", "-loglevel", "error", "-f", fmt, "-i", str(path), "-c", "copy", str(mp4)],
                **kwargs,
            )
            if mp4.exists() and mp4.stat().st_size > 0:
                path.unlink(missing_ok=True)
                return mp4
        except Exception:
            pass
    return path
