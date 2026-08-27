"""Windows HTTP client for the Linux studio server."""

from __future__ import annotations

import json
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import fields
from pathlib import Path

from v380.paths import DATA_DIR
from v380.store.camera_store import Camera

SERVER_FILE = DATA_DIR / "server.txt"


def camera_from_dict(data: dict) -> Camera:
    names = {f.name for f in fields(Camera)}
    device_id = str(data.get("device_id") or data.get("device_id") or "")
    mapped = {
        "id": int(data.get("id") or 0),
        "name": str(data.get("name") or device_id),
        "device_id": device_id,
        "mac": str(data.get("mac") or ""),
        "ip": str(data.get("ip") or ""),
        "port": int(data.get("port") or 8800),
        "username": str(data.get("username") or ""),
        "password": str(data.get("password") or ""),
        "source": str(data.get("source") or "lan"),
        "quality": int(data.get("quality") if data.get("quality") is not None else 1),
        "auto_record": bool(data.get("auto_record") if data.get("auto_record") is not None else data.get("auto_record") or False),
        "record_chunk": str(data.get("record_chunk") or data.get("record_chunk") or "hour"),
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }
    return Camera(**{k: v for k, v in mapped.items() if k in names})


class StudioAPI:
    def __init__(self, host: str, token: str = "", port: int = 8080):
        host = host.strip()
        if "://" in host:
            self.base = host.rstrip("/")
        else:
            if ":" not in host:
                host = f"{host}:{port}"
            self.base = f"http://{host}"
        self.token = token
        self.user = ""

    def save_host(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        host = self.base.replace("http://", "").replace("https://", "")
        SERVER_FILE.write_text(host.split(":")[0], encoding="utf-8")

    def headers(self, extra: dict | None = None) -> dict:
        hdrs = {"Content-Type": "application/json"}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
            hdrs["X-Token"] = self.token
        if extra:
            hdrs.update(extra)
        return hdrs

    def request(self, method: str, path: str, body=None, raw: bytes | None = None, timeout: float = 12):
        data = None
        headers = self.headers()
        if raw is not None:
            data = raw
            headers = self.headers({"Content-Type": "application/octet-stream"})
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type") or ""
                if "json" in ctype or payload[:1] in (b"{", b"["):
                    return json.loads(payload.decode("utf-8") or "{}")
                return payload
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                raise RuntimeError(parsed.get("error") or detail or str(exc)) from exc
            except json.JSONDecodeError:
                raise RuntimeError(detail or str(exc)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {self.base} ({exc.reason})") from exc

    def ping(self) -> dict:
        return self.request("GET", "/api/ping")

    def login(self, username: str, password: str) -> dict:
        out = self.request("POST", "/api/login", {"username": username, "password": password})
        self.token = str(out.get("token") or "")
        self.user = str(out.get("user") or username)
        self.save_host()
        return out

    def cameras(self) -> list[Camera]:
        out = self.request("GET", "/api/cameras")
        return [camera_from_dict(row) for row in (out.get("cameras") or [])]

    def upsert(self, cam: Camera) -> Camera:
        body = {
            "id": cam.id,
            "name": cam.name,
            "device_id": getattr(cam, "device_id", None) or getattr(cam, "device_id", ""),
            "mac": cam.mac,
            "ip": cam.ip,
            "port": cam.port,
            "username": cam.username,
            "password": cam.password,
            "source": cam.source,
            "quality": cam.quality,
            "auto_record": bool(getattr(cam, "auto_record", None) or getattr(cam, "auto_record", False)),
            "record_chunk": getattr(cam, "record_chunk", None) or getattr(cam, "record_chunk", "hour"),
        }
        if cam.id:
            out = self.request("PUT", f"/api/cameras/{cam.id}", body)
        else:
            out = self.request("POST", "/api/cameras", body)
        return camera_from_dict(out.get("camera") or body)

    def update(self, cam: Camera) -> Camera:
        return self.upsert(cam)

    def delete(self, camera_id: int) -> None:
        self.request("DELETE", f"/api/cameras/{camera_id}")

    def set_auto_record(self, camera_id: int, enabled: bool) -> None:
        self.request("POST", f"/api/cameras/{camera_id}/auto_record", {"enabled": enabled})

    def discover(self) -> list[dict]:
        out = self.request("GET", "/api/discover", timeout=20)
        return list(out.get("devices") or [])

    def snapshot(self, camera_id: int) -> bytes | None:
        try:
            out = self.request("GET", f"/api/cameras/{camera_id}/snapshot", timeout=8)
        except Exception:
            return None
        return out if isinstance(out, (bytes, bytearray)) else None

    def command(self, camera_id: int, name: str) -> bool:
        out = self.request("POST", f"/api/cameras/{camera_id}/command", {"name": name}, timeout=3)
        return bool(out.get("ok"))

    def talk_start(self, camera_id: int) -> bool:
        return bool(self.request("POST", f"/api/cameras/{camera_id}/talk/start", timeout=4).get("ok"))

    def talk_audio(self, camera_id: int, ima: bytes) -> bool:
        return bool(self.request("POST", f"/api/cameras/{camera_id}/talk/audio", raw=ima, timeout=5).get("ok"))

    def talk_stop(self, camera_id: int) -> None:
        self.request("POST", f"/api/cameras/{camera_id}/talk/stop", timeout=3)

    def alert(self, camera_id: int, on: bool) -> bool:
        return bool(self.request("POST", f"/api/cameras/{camera_id}/alert", {"on": on}, timeout=4).get("ok"))

    def record_status(self) -> dict:
        out = self.request("GET", "/api/record/status")
        return out.get("status") or {}

    def index(self) -> dict:
        return self.request("GET", "/api/index")

    def file_url(self, rel: str) -> str:
        return f"{self.base}/file/{rel}"

    def open_h264(self, camera_id: int):
        req = urllib.request.Request(
            f"{self.base}/api/cameras/{camera_id}/h264",
            headers=self.headers(),
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=20)
        try:
            resp.fp.raw._sock.settimeout(None)
        except Exception:
            pass
        return resp

    @staticmethod
    def _read_exact(resp, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = resp.read(n - len(buf))
            if not chunk:
                return buf
            buf += chunk
        return buf

    @classmethod
    def iter_h264(cls, resp):
        codec_b = cls._read_exact(resp, 1)
        if not codec_b:
            return
        yield ("codec", "hevc" if codec_b[0] == 1 else "h264", b"")
        while True:
            hdr = cls._read_exact(resp, 5)
            if len(hdr) < 5:
                break
            length = struct.unpack(">I", hdr[1:5])[0]
            if length == 0 or length > 4_000_000:
                break
            payload = cls._read_exact(resp, length)
            if len(payload) < length:
                break
            yield ("frame", bool(hdr[0] & 1), payload)

    def open_mjpeg(self, camera_id: int):
        req = urllib.request.Request(
            f"{self.base}/api/cameras/{camera_id}/mjpeg",
            headers=self.headers(),
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=20)
        try:
            resp.fp.raw._sock.settimeout(None)
        except Exception:
            pass
        return resp

    def open_file(self, rel: str):
        req = urllib.request.Request(self.file_url(rel), headers=self.headers(), method="GET")
        return urllib.request.urlopen(req, timeout=120)

    @staticmethod
    def iter_mjpeg(resp):
        buf = b""
        while True:
            chunk = resp.read(16384)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9")
                if start < 0 or end < 0 or end < start:
                    break
                yield buf[start : end + 2]
                buf = buf[end + 2 :]


class RemoteCameraStore:
    def __init__(self, api: StudioAPI):
        self.api = api
        self.remote = True

    def close(self) -> None:
        return None

    def list(self) -> list[Camera]:
        return self.api.cameras()

    def get(self, camera_id: int) -> Camera | None:
        for cam in self.list():
            if cam.id == camera_id:
                return cam
        return None

    def get_by_device_id(self, device_id: str) -> Camera | None:
        for cam in self.list():
            if str(getattr(cam, "device_id", None) or getattr(cam, "device_id", "")) == str(device_id):
                return cam
        return None

    def upsert(self, cam: Camera) -> Camera:
        return self.api.upsert(cam)

    def update(self, cam: Camera) -> Camera:
        return self.api.update(cam)

    def set_auto_record(self, camera_id: int, enabled: bool) -> None:
        self.api.set_auto_record(camera_id, enabled)

    def delete(self, camera_id: int) -> None:
        self.api.delete(camera_id)

    def set_status(self, *args, **kwargs) -> None:
        return None


class RemotePreview:
    def __init__(self, api: StudioAPI, on_state=None):
        self.api = api
        self._on_state = on_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: dict[int, bytes] = {}
        self._lock = threading.Lock()
        self._ids: list[int] = []

    def start(self, cameras: list[Camera]) -> None:
        self.stop()
        self._stop = threading.Event()
        self._ids = [c.id for c in cameras]
        self._thread = threading.Thread(target=self._run, daemon=True, name="remote-preview")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def take_latest(self, camera_id: int) -> bytes | None:
        with self._lock:
            return self._latest.pop(camera_id, None)

    def _run(self) -> None:
        while not self._stop.is_set():
            for cam_id in list(self._ids):
                if self._stop.is_set():
                    return
                jpeg = self.api.snapshot(cam_id)
                if jpeg:
                    with self._lock:
                        self._latest[cam_id] = jpeg
                    if self._on_state:
                        try:
                            self._on_state(cam_id, "Live")
                        except Exception:
                            pass
            time.sleep(0.35)


class RemoteRecordSupervisor:
    def __init__(self, api: StudioAPI):
        self.api = api
        self._status: dict = {}
        self._at = 0.0

    def _refresh(self) -> dict:
        now = time.time()
        if now - self._at < 0.8:
            return self._status
        try:
            self._status = self.api.record_status()
        except Exception:
            self._status = {}
        self._at = now
        return self._status

    def sync(self, cameras: list[Camera]) -> None:
        self._at = 0.0

    def watch(self) -> None:
        self._refresh()

    def is_recording(self, camera_id: int) -> bool:
        rec = self._refresh().get("recording") or {}
        if isinstance(rec, dict):
            return bool(rec.get(str(camera_id)) or rec.get(camera_id))
        return False

    def camera_status(self, camera_id: int, enabled: bool) -> tuple[str, str]:
        if not enabled:
            return "Service: off", "off"
        if self.is_recording(camera_id):
            return "Service: recording", "ok"
        beat = self._refresh().get("heartbeat") or self._refresh().get("heartbeat")
        try:
            fresh = bool(beat) and (time.time() - float(beat) <= 20)
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            return "Service: starting…", "wait"
        return "Service: not running", "down"

    def service_summary(self, cameras: list[Camera]) -> str:
        wanted = [c for c in cameras if getattr(c, "auto_record", False) or getattr(c, "auto_record", False)]
        if not wanted:
            return "Record service: off"
        writing = sum(1 for c in wanted if self.is_recording(c.id))
        return f"Record service: server  ·  {writing}/{len(wanted)} camera(s) saving"

    def release_to_worker(self) -> None:
        return None

    def stop_all(self) -> None:
        return None


class ApiTalkClient:
    def __init__(self, api: StudioAPI, camera_id: int):
        self.api = api
        self.camera_id = camera_id

    def start_talk(self) -> bool:
        return self.api.talk_start(self.camera_id)

    def send_talk_audio(self, ima) -> bool:
        return self.api.talk_audio(self.camera_id, ima)

    def stop_talk(self) -> None:
        self.api.talk_stop(self.camera_id)
