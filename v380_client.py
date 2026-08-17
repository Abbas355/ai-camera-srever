"""LAN V380 client: TCP :8800 auth, stream, decrypt, I-frame snapshots."""

from __future__ import annotations

import queue
import random
import socket
import string
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from Crypto.Cipher import AES

STATIC_PW_KEY = b"macrovideo+*#!^@"
MEDIA_KEY_MID = 0x618123462C14795C
MEDIA_KEY_TAIL = 0x82800DF0


def _u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def _u16(v: int) -> bytes:
    return struct.pack("<H", v & 0xFFFF)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


def _ru32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _ru16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def encrypt_password(password: str) -> bytes:
    rng = random.Random()
    chars = string.ascii_letters + string.digits
    random_key = bytes(ord(chars[rng.randrange(len(chars))]) for _ in range(16))
    pad = password.encode("ascii", errors="ignore")[:48].ljust(48, b"\x00")

    def aes_ecb(key: bytes, data: bytearray) -> None:
        cipher = AES.new(key, AES.MODE_ECB)
        for i in range(0, 48, 16):
            data[i : i + 16] = cipher.encrypt(bytes(data[i : i + 16]))

    buf = bytearray(pad)
    aes_ecb(STATIC_PW_KEY, buf)
    aes_ecb(random_key, buf)
    return random_key + bytes(buf)


def media_aes_key(ticket: int) -> bytes:
    return _u32(ticket) + _u64(MEDIA_KEY_MID) + _u32(MEDIA_KEY_TAIL)


def decrypt_video(data: bytearray, key: bytes) -> None:
    cipher = AES.new(key, AES.MODE_ECB)
    offset = 0
    length = len(data)
    while offset + 64 <= length:
        for i in range(4):
            o = offset + i * 16
            data[o : o + 16] = cipher.decrypt(bytes(data[o : o + 16]))
        offset += 80


def decrypt_pre2k(data: bytearray, key: bytes) -> None:
    n = (len(data) // 16) * 16
    if n <= 0:
        return
    cipher = AES.new(key, AES.MODE_ECB)
    data[:n] = cipher.decrypt(bytes(data[:n]))


def find_nals(data: bytes) -> list[bytes]:
    nals: list[bytes] = []
    i = 0
    while i < len(data) - 4:
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            start = i + 4
            end = start
            while end < len(data) - 4:
                if data[end : end + 4] == b"\x00\x00\x00\x01":
                    break
                end += 1
            if end >= len(data) - 4:
                end = len(data)
            nals.append(data[start:end])
            i = end
        else:
            i += 1
    return nals


def extract_sps_pps(h264: bytes) -> tuple[bytes | None, bytes | None]:
    sps = pps = None
    for nal in find_nals(h264):
        if not nal:
            continue
        t = nal[0] & 0x1F
        if t == 7:
            sps = nal
        elif t == 8:
            pps = nal
    return sps, pps


def prepend_sps_pps(idr: bytes, sps: bytes, pps: bytes) -> bytes:
    sc = b"\x00\x00\x00\x01"
    return sc + sps + sc + pps + idr


def recv_exact(sock: socket.socket, n: int, timeout: float = 8.0) -> bytes:
    sock.settimeout(timeout)
    chunks = bytearray()
    while len(chunks) < n:
        part = sock.recv(n - len(chunks))
        if not part:
            break
        chunks.extend(part)
    return bytes(chunks)


class RecvBuf:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

    def pull(self, n: int, timeout: float) -> bytes:
        deadline = time.time() + timeout
        while len(self.buf) < n and not self.closed:
            remain = deadline - time.time()
            if remain <= 0:
                break
            self.sock.settimeout(min(1.0, remain))
            try:
                part = self.sock.recv(min(65536, max(n - len(self.buf), 8192)))
            except (TimeoutError, socket.timeout):
                continue
            if not part:
                self.closed = True
                break
            self.buf.extend(part)
        if len(self.buf) < n:
            return b""
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def find_sync(self, timeout: float, stop_event=None) -> bool:
        deadline = time.time() + timeout
        while not self.closed:
            if stop_event is not None and stop_event.is_set():
                return False
            idx = self.buf.find(b"\x7f")
            if idx >= 0:
                if idx > 0:
                    del self.buf[:idx]
                return True
            remain = deadline - time.time()
            if remain <= 0:
                return False
            self.sock.settimeout(min(1.0, remain))
            try:
                part = self.sock.recv(16384)
            except (TimeoutError, socket.timeout):
                continue
            if not part:
                self.closed = True
                return False
            self.buf.extend(part)
        return False


class V380SnapshotClient:
    def __init__(
        self,
        ip: str,
        device_id: int,
        username: str,
        password: str,
        port: int = 8800,
        quality: int = 1,
    ):
        self.ip = ip
        self.port = port
        self.device_id = device_id & 0xFFFFFFFF
        self.username = username
        self.password = password
        self.quality = 1 if quality else 0
        self.auth_ticket = 0
        self.session_id = 0
        self.device_version = 0
        self.comm_version = 0
        self.frame_width = 1280
        self.frame_height = 720
        self.aes_key = b"\x00" * 16
        self._sock: socket.socket | None = None
        self._rx: RecvBuf | None = None
        self._sps: bytes | None = None
        self._pps: bytes | None = None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._rx = None

    def connect(self) -> None:
        self._auth()
        self._stream_login()
        self._start_stream()

    def _tcp(self) -> socket.socket:
        s = socket.create_connection((self.ip, self.port), timeout=5)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return s

    def _auth(self) -> None:
        sock = self._tcp()
        try:
            pkt = bytearray(520)
            pkt[0:4] = _u32(1167)
            pkt[4:8] = _u32(120)
            pkt[8] = 31
            pkt[9:13] = _u32(1)
            pkt[13:17] = _u32(self.device_id)
            user = self.username.encode("ascii", errors="ignore")[:32]
            pkt[49 : 49 + len(user)] = user
            pw = encrypt_password(self.password)
            pkt[81 : 81 + len(pw)] = pw
            sock.sendall(pkt)
            resp = recv_exact(sock, 256, timeout=8)
            if len(resp) < 16:
                raise RuntimeError("Auth response too short")
            if _ru32(resp, 0) != 1168:
                raise RuntimeError(f"Unexpected auth cmd {_ru32(resp, 0)}")
            result = _ru32(resp, 4)
            if result != 1001:
                reasons = {1011: "invalid username", 1012: "invalid password", 1018: "invalid device id"}
                raise RuntimeError(reasons.get(result, f"login failed ({result})"))
            self.device_version = resp[12]
            self.auth_ticket = _ru32(resp, 13)
            self.session_id = _ru32(resp, 17)
        finally:
            sock.close()

    def _stream_login(self) -> None:
        sock = self._tcp()
        pkt = bytearray(256)
        pkt[0:4] = _u32(301)
        pkt[4:8] = _u32(self.device_id)
        pkt[8:12] = _u32(0)
        pkt[12:14] = _u16(20)
        pkt[14:18] = _u32(self.auth_ticket)
        pkt[22:26] = _u32(4097)
        pkt[26:30] = _u32(self.quality)
        sock.sendall(pkt)

        rx = RecvBuf(sock)
        # Camera sends a short 401 (32 bytes on this firmware), not 412.
        head = rx.pull(8, timeout=8)
        if len(head) < 8:
            sock.close()
            raise RuntimeError("Stream login response too short")
        if _ru32(head, 0) != 401:
            sock.close()
            raise RuntimeError(f"Unexpected stream cmd {_ru32(head, 0)}")
        result = struct.unpack_from("<i", head, 4)[0]
        if result in (-11, -12):
            sock.close()
            raise RuntimeError(f"Stream login rejected ({result})")

        rest = rx.pull(24, timeout=1.0)
        info = head + rest
        if len(info) >= 18:
            self.comm_version = _ru16(info, 8)
            self.frame_width = _ru32(info, 10) or 1280
            self.frame_height = _ru32(info, 14) or 720
        if self.device_version > 30:
            self.aes_key = media_aes_key(self.auth_ticket)
        self._sock = sock
        self._rx = rx

    def _start_stream(self) -> None:
        if self._sock is None:
            raise RuntimeError("Not connected")
        pkt = bytearray(256)
        pkt[0:4] = _u32(303)
        pkt[4:6] = _u16(0x3001)
        self._sock.sendall(pkt)

    def read_iframe(self, timeout: float = 15.0) -> bytes:
        if self._sock is None or self._rx is None:
            raise RuntimeError("Not connected")
        need_decrypt = self.device_version > 30
        video = bytearray()
        video_total = 0
        deadline = time.time() + timeout

        while time.time() < deadline:
            remain = max(1.0, deadline - time.time())
            if not self._rx.find_sync(remain):
                raise RuntimeError("No video header (0x7F) from camera")
            header = self._rx.pull(12, timeout=3)
            if len(header) < 12:
                raise RuntimeError("Stream ended")
            if header[0] != 0x7F:
                continue
            ftype = header[1]
            total = _ru16(header, 3)
            cur = _ru16(header, 5)
            pay_len = _ru16(header, 7)
            if ftype not in (0x00, 0x01, 0x1A, 0x5B) or pay_len == 0 or pay_len > 20000 or total == 0 or cur >= total:
                # false 0x7F in payload — drop one byte and resync
                self._rx.feed(header[1:])
                continue
            payload = self._rx.pull(pay_len, timeout=8)
            if len(payload) < pay_len:
                self._rx.feed(header[1:] + payload)
                continue

            if ftype not in (0x00, 0x01):
                continue
            if cur == 0 or total != video_total:
                video = bytearray()
                video_total = total
            video.extend(payload)
            if cur != total - 1:
                continue
            if len(video) < 16:
                video = bytearray()
                continue
            body = bytearray(video[16:])
            video = bytearray()
            if need_decrypt:
                if self.comm_version == 21:
                    decrypt_pre2k(body, self.aes_key)
                else:
                    decrypt_video(body, self.aes_key)
            if len(body) < 4 or body[:4] != b"\x00\x00\x00\x01":
                continue
            if ftype != 0x00:
                continue
            sps, pps = extract_sps_pps(bytes(body))
            if sps:
                self._sps = sps
            if pps:
                self._pps = pps
            return bytes(body)

        raise RuntimeError("Timed out waiting for I-frame")

    def iter_video_frames(self, stop_event=None):
        """Yield decrypted Annex-B video frames (I and P) in order."""
        if self._sock is None or self._rx is None:
            raise RuntimeError("Not connected")
        need_decrypt = self.device_version > 30
        video = bytearray()
        video_total = 0
        last_type = 0

        while stop_event is None or not stop_event.is_set():
            if not self._rx.find_sync(20.0, stop_event):
                if stop_event is not None and stop_event.is_set():
                    return
                if self._rx.closed:
                    raise RuntimeError("Stream ended")
                continue
            header = self._rx.pull(12, timeout=3)
            if len(header) < 12:
                if self._rx.closed:
                    raise RuntimeError("Stream ended")
                continue
            if header[0] != 0x7F:
                continue
            ftype = header[1]
            total = _ru16(header, 3)
            cur = _ru16(header, 5)
            pay_len = _ru16(header, 7)
            if ftype not in (0x00, 0x01, 0x1A, 0x5B) or pay_len == 0 or pay_len > 20000 or total == 0 or cur >= total:
                self._rx.feed(header[1:])
                continue
            payload = self._rx.pull(pay_len, timeout=8)
            if len(payload) < pay_len:
                self._rx.feed(header[1:] + payload)
                continue
            if ftype not in (0x00, 0x01):
                continue
            if cur == 0 or total != video_total:
                video = bytearray()
                video_total = total
                last_type = ftype
            video.extend(payload)
            if cur != total - 1:
                continue
            if len(video) < 16:
                video = bytearray()
                continue
            body = bytearray(video[16:])
            video = bytearray()
            if need_decrypt:
                if self.comm_version == 21:
                    decrypt_pre2k(body, self.aes_key)
                else:
                    decrypt_video(body, self.aes_key)
            if len(body) < 4 or body[:4] != b"\x00\x00\x00\x01":
                continue
            if last_type == 0x00:
                sps, pps = extract_sps_pps(bytes(body))
                if sps:
                    self._sps = sps
                if pps:
                    self._pps = pps
            yield last_type == 0x00, bytes(body)

    def h264_for_decode(self, iframe: bytes) -> bytes:
        if self._sps and self._pps:
            return prepend_sps_pps(iframe, self._sps, self._pps)
        return iframe


def _ffmpeg_exe() -> str | None:
    from shutil import which

    found = which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class LiveH264Decoder:
    """Persistent FFmpeg pipe: Annex-B in, JPEG out, every frame."""

    def __init__(self, out_queue):
        import threading

        exe = _ffmpeg_exe()
        if not exe:
            raise RuntimeError("FFmpeg not available")
        self.proc = subprocess.Popen(
            [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "6",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._queue = out_queue
        self._stop = threading.Event()
        self._in_q: queue.Queue = queue.Queue(maxsize=8)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._reader.start()
        self._writer.start()

    def write_frame(self, is_iframe: bool, payload: bytes, sps: bytes | None, pps: bytes | None) -> None:
        item = (is_iframe, payload, sps, pps)
        if self._in_q.full():
            try:
                self._in_q.get_nowait()
            except queue.Empty:
                pass
        try:
            self._in_q.put_nowait(item)
        except queue.Full:
            pass

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            try:
                is_iframe, payload, sps, pps = self._in_q.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.proc.stdin is None:
                break
            chunk = payload
            if is_iframe and sps and pps:
                chunk = prepend_sps_pps(payload, sps, pps)
            try:
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break

    def _read_loop(self) -> None:
        stdout = self.proc.stdout
        if stdout is None:
            return
        buf = bytearray()
        while not self._stop.is_set():
            chunk = stdout.read(8192)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                jpeg = _pop_jpeg(buf)
                if not jpeg:
                    break
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except Exception:
                        pass
                try:
                    self._queue.put_nowait(jpeg)
                except Exception:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.kill()
        except OSError:
            pass


def _pop_jpeg(buf: bytearray) -> bytes | None:
    start = buf.find(b"\xff\xd8")
    if start < 0:
        if len(buf) > 1_000_000:
            buf.clear()
        return None
    if start > 0:
        del buf[:start]
    end = buf.find(b"\xff\xd9", 2)
    if end < 0:
        return None
    jpeg = bytes(buf[: end + 2])
    del buf[: end + 2]
    return jpeg if len(jpeg) > 500 else None


def h264_to_jpeg(h264: bytes) -> bytes:
    jpeg = _decode_opencv(h264)
    if jpeg:
        return jpeg
    jpeg = _decode_ffmpeg(h264)
    if jpeg:
        return jpeg
    raise RuntimeError("Could not decode H.264 (install opencv-python or ffmpeg)")


def _decode_opencv(h264: bytes) -> bytes | None:
    try:
        import cv2
    except ImportError:
        return None
    tmp = Path(tempfile.gettempdir()) / "v380_snap.h264"
    tmp.write_bytes(h264)
    cap = cv2.VideoCapture(str(tmp))
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None
    finally:
        cap.release()


def _decode_ffmpeg(h264: bytes) -> bytes | None:
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-f",
                "image2",
                "pipe:1",
            ],
            input=h264,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.stdout else None
