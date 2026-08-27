"""Linux studio API. The Windows app is only a UI.

Login, cameras, live video, talk, alert, PTZ, and recordings stay on this machine.
Port 8080 only. Does not touch 22, 8000, or 8081.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from v380.paths import DATA_DIR, REC_DIR, ROOT as APP_DIR
PORT = 8080
VIDEO_EXT = {".mp4", ".h264", ".h265"}
TOKEN_TTL = 7 * 24 * 3600
DEFAULT_USER = os.environ.get("V380_ADMIN_USER", "admin")
DEFAULT_PASS = os.environ.get("V380_ADMIN_PASS", "1290")

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def pick(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def call(obj, *names, args=(), kwargs=None, default=None):
    kwargs = kwargs or {}
    fn = pick(obj, *names)
    if callable(fn):
        return fn(*args, **kwargs)
    return default


def load_mods() -> dict:
    from v380.client import extras as ex
    from v380.client import v380_client as vc
    from v380.record import auto_record as ar
    from v380.store.camera_store import Camera, CameraStore

    return {
        "Camera": Camera,
        "CameraStore": CameraStore,
        "discover": pick(ex, "discover_devices", "discover_devices"),
        "relay": pick(ex, "get_relay_ip", "get_relay_ip"),
        "Alert": pick(ex, "AlertSiren", "AlertSiren"),
        "Client": pick(vc, "V380SnapshotClient", "V380SnapshotClient"),
        "Decoder": pick(vc, "LiveH264Decoder", "LiveH264Decoder"),
        "read_status": pick(ar, "read_worker_status", "read_worker_status"),
        "ensure_worker": pick(ar, "ensure_worker", "ensure_worker"),
    }


MODS = load_mods()
STORE = MODS["CameraStore"]()


def cam_get(cam, *names, default=""):
    val = pick(cam, *names, default=default)
    return default if val is None else val


def cam_json(cam) -> dict:
    return {
        "id": cam.id,
        "name": cam.name,
        "device_id": str(cam_get(cam, "device_id", "device_id")),
        "mac": cam.mac or "",
        "ip": cam.ip or "",
        "port": int(cam.port),
        "username": cam.username,
        "password": cam.password,
        "source": cam.source,
        "quality": int(cam.quality),
        "auto_record": bool(cam_get(cam, "auto_record", "auto_record", default=False)),
        "record_chunk": str(cam_get(cam, "record_chunk", "record_chunk", default="hour") or "hour"),
        "created_at": cam.created_at,
        "updated_at": cam.updated_at,
        "quality_name": str(cam_get(cam, "quality_name", "quality_name", default="HD")),
        "source_name": str(cam_get(cam, "source_name", "source_name", default="LAN")),
    }


def hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)


def ensure_admin() -> None:
    conn = STORE._conn
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            pass_salt BLOB NOT NULL,
            pass_hash BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    if row:
        return
    salt = os.urandom(16)
    conn.execute(
        "INSERT INTO users (username, pass_salt, pass_hash) VALUES (?, ?, ?)",
        (DEFAULT_USER, salt, hash_pw(DEFAULT_PASS, salt)),
    )
    conn.commit()
    print("[studio] created login user:", DEFAULT_USER)


def check_user(username: str, password: str) -> bool:
    row = STORE._conn.execute(
        "SELECT pass_salt, pass_hash FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not row:
        return False
    return secrets.compare_digest(hash_pw(password, bytes(row["pass_salt"])), bytes(row["pass_hash"]))


def new_token(username: str) -> str:
    token = secrets.token_hex(24)
    with _sessions_lock:
        _sessions[token] = {"user": username, "exp": time.time() + TOKEN_TTL}
    return token


def auth_user(token: str | None) -> str | None:
    if not token:
        return None
    with _sessions_lock:
        rec = _sessions.get(token)
        if not rec or rec["exp"] < time.time():
            _sessions.pop(token, None)
            return None
        return rec["user"]


def local_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip not in ips:
            ips.insert(0, ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def clip_index() -> dict:
    cameras: dict[str, dict[str, list[dict]]] = {}
    if REC_DIR.is_dir():
        for path in REC_DIR.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
                continue
            try:
                rel = path.relative_to(REC_DIR)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) < 2:
                continue
            camera = parts[0]
            date = parts[1] if len(parts) >= 3 else "other"
            cameras.setdefault(camera, {}).setdefault(date, []).append(
                {"rel": rel.as_posix(), "name": path.name, "size": path.stat().st_size}
            )
    return {"ok": True, "port": PORT, "ips": local_ips(), "cameras": cameras}


def camera_from_body(body: dict, existing=None):
    Camera = MODS["Camera"]
    device_id = str(
        body.get("device_id")
        or body.get("device_id")
        or (cam_get(existing, "device_id", "device_id") if existing else "")
        or ""
    )
    password = body.get("password")
    if not password and existing is not None:
        password = existing.password
    return Camera(
        id=int(body.get("id") or (existing.id if existing else 0)),
        name=str(body.get("name") or device_id),
        device_id=device_id,
        mac=str(body.get("mac") or (existing.mac if existing else "") or ""),
        ip=str(body.get("ip") or (existing.ip if existing else "") or ""),
        port=int(body.get("port") or (existing.port if existing else 8800)),
        username=str(body.get("username") or (existing.username if existing else "")),
        password=str(password or ""),
        source=str(body.get("source") or (existing.source if existing else "lan")),
        quality=int(
            body["quality"]
            if body.get("quality") is not None
            else (existing.quality if existing else 1)
        ),
        auto_record=bool(
            body["auto_record"]
            if body.get("auto_record") is not None
            else (cam_get(existing, "auto_record", "auto_record", default=False) if existing else False)
        ),
        record_chunk=str(
            body.get("record_chunk")
            or body.get("record_chunk")
            or (cam_get(existing, "record_chunk", "record_chunk", default="hour") if existing else "hour")
            or "hour"
        ),
        created_at=existing.created_at if existing else "",
        updated_at=existing.updated_at if existing else "",
    )


class Session:
    def __init__(self, cam):
        self.cam = cam
        self.jpeg: bytes | None = None
        self.codec = "h264"
        self.last_iframe: bytes | None = None
        self.state = "Starting…"
        self.alive = True
        self._stop = threading.Event()
        self._client = None
        self._alert = None
        self._subs: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._jpeg_n = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"hub-{cam.id}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.alive = False
        self._stop.set()
        if self._client is not None:
            call(self._client, "close", "close")

    def _run(self) -> None:
        Client = MODS["Client"]
        Decoder = MODS["Decoder"]
        relay = MODS["relay"]
        cam = self.cam
        device_id = int(cam_get(cam, "device_id", "device_id") or 0)
        while self.alive and not self._stop.is_set():
            client = None
            decoder = None
            frames: queue.Queue = queue.Queue(maxsize=2)
            try:
                host = cam.ip
                source = cam.source
                if source == "cloud" and relay:
                    host = relay(device_id)
                    if not host:
                        raise RuntimeError("No cloud relay")
                self.state = "Connecting…"
                client = Client(
                    host,
                    device_id,
                    cam.username,
                    cam.password,
                    cam.port,
                    quality=1,
                    source=source,
                )
                call(client, "connect", "connect")
                self._client = client
                self.state = "Live"
                got_key = False
                iterator = pick(client, "iter_video_frames", "iter_video_frames")
                if iterator is None:
                    raise RuntimeError("Camera client has no video iterator")
                for item in iterator(self._stop):
                    if not self.alive:
                        break
                    if not (isinstance(item, tuple) and len(item) == 3):
                        continue
                    kind, is_iframe, payload = item
                    if kind == "audio":
                        continue
                    if decoder is None:
                        fmt = pick(client, "video_codec", "video_codec", default="h264") or "h264"
                        self.codec = fmt
                        try:
                            decoder = Decoder(frames, fmt=fmt, scale_width=1280, jpeg_q=5, threads=1)
                        except TypeError:
                            decoder = Decoder(frames, fmt=fmt)
                    if is_iframe:
                        got_key = True
                    if not got_key:
                        continue
                    rec = payload
                    prep = pick(client, "h264_for_decode", "h264_for_decode")
                    if is_iframe and prep:
                        rec = prep(payload)
                    self._emit_h264(bool(is_iframe), rec)
                    self._jpeg_n += 1
                    with self._sub_lock:
                        live_watchers = bool(self._subs)
                    if (not live_watchers) or is_iframe or self._jpeg_n % 8 == 0:
                        write = pick(decoder, "write_frame", "write_frame")
                        if write:
                            write(
                                bool(is_iframe),
                                rec,
                                pick(client, "_sps", "_sps"),
                                pick(client, "_pps", "_pps"),
                                pick(client, "_vps", "_vps"),
                            )
                        try:
                            self.jpeg = frames.get_nowait()
                        except queue.Empty:
                            pass
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.state = f"Offline ({exc})"
                print("[studio] camera", getattr(cam, "name", "?"), ":", exc)
                self._client = None
                time.sleep(2.5)
            finally:
                if decoder is not None:
                    call(decoder, "close", "close")
                if client is not None:
                    call(client, "close", "close")
                if self._client is client:
                    self._client = None
        self.state = "Stopped"

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2)
        with self._sub_lock:
            if self.last_iframe:
                try:
                    q.put_nowait((True, self.last_iframe))
                except queue.Full:
                    pass
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit_h264(self, is_iframe: bool, payload: bytes) -> None:
        if not payload:
            return
        if is_iframe:
            self.last_iframe = payload
        with self._sub_lock:
            subs = list(self._subs)
        item = (is_iframe, payload)
        for q in subs:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def command(self, name: str) -> bool:
        if self._client is None:
            return False
        return bool(call(self._client, "send_control", "send_control", args=(name,), default=False))

    def talk_start(self) -> bool:
        for _ in range(6):
            if self._client is not None:
                break
            time.sleep(0.05)
        if self._client is None:
            return False
        return bool(call(self._client, "start_talk", "start_talk", default=False))

    def talk_audio(self, ima: bytes) -> bool:
        if self._client is None:
            return False
        return bool(call(self._client, "send_talk_audio", "send_talk_audio", args=(ima,), default=False))

    def talk_stop(self) -> None:
        if self._client is not None:
            call(self._client, "stop_talk", "stop_talk")

    def alert_on(self) -> bool:
        Alert = MODS["Alert"]
        if self._client is None or Alert is None:
            return False
        siren = Alert()
        self._alert = siren
        return bool(call(siren, "start", "start", args=(self._client,), default=False))

    def alert_off(self) -> None:
        siren = self._alert
        self._alert = None
        if siren is not None:
            call(siren, "stop", "stop")


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._live: dict[int, Session] = {}

    def session(self, cam_id: int) -> Session | None:
        with self._lock:
            ses = self._live.get(cam_id)
            if ses and ses.alive:
                return ses
            cam = call(STORE, "get", "get", args=(cam_id,))
            if cam is None:
                return None
            ses = Session(cam)
            self._live[cam_id] = ses
            ses.start()
            return ses

    def drop(self, cam_id: int) -> None:
        with self._lock:
            ses = self._live.pop(cam_id, None)
        if ses:
            ses.stop()

    def snapshot(self, cam_id: int) -> bytes | None:
        ses = self.session(cam_id)
        return ses.jpeg if ses else None

    def status(self) -> dict:
        with self._lock:
            items = list(self._live.items())
        return {str(cid): {"live": ses.alive and ses.jpeg is not None, "state": ses.state} for cid, ses in items}


HUB = Hub()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("[studio]", fmt % args)

    def token(self) -> str | None:
        hdr = self.headers.get("Authorization") or ""
        if hdr.lower().startswith("bearer "):
            return hdr[7:].strip()
        return self.headers.get("X-Token")

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def send_json(self, payload: dict, code: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def need_user(self) -> str | None:
        user = auth_user(self.token())
        if not user:
            self.send_json({"ok": False, "error": "login required"}, 401)
            return None
        return user

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def read_raw(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def kick_record(self) -> None:
        ensure = MODS["ensure_worker"]
        if ensure:
            try:
                ensure()
            except Exception as exc:
                print("[studio] record worker:", exc)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/api/ping"):
            self.send_json({"ok": True, "ips": local_ips(), "port": PORT, "app": "v380-studio"})
            return
        if path == "/api/index":
            if not self.need_user():
                return
            self.send_json(clip_index())
            return
        if path.startswith("/file/"):
            if not self.need_user():
                return
            rel = Path(path[len("/file/") :])
            if ".." in rel.parts:
                self.send_json({"ok": False, "error": "bad path"}, 400)
                return
            full = (REC_DIR / rel).resolve()
            try:
                full.relative_to(REC_DIR.resolve())
            except ValueError:
                self.send_json({"ok": False, "error": "bad path"}, 400)
                return
            if not full.is_file():
                self.send_json({"ok": False, "error": "missing"}, 404)
                return
            data = full.read_bytes()
            ctype = "video/mp4" if full.suffix.lower() == ".mp4" else "application/octet-stream"
            self.send_response(200)
            self.cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not self.need_user():
            return
        if path == "/api/me":
            self.send_json({"ok": True, "user": auth_user(self.token())})
            return
        if path == "/api/discover":
            discover = MODS["discover"]
            try:
                devices = list(discover()) if discover else []
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
                return
            out = []
            for device in devices:
                out.append(
                    {
                        "mac": pick(device, "mac", default="") or "",
                        "device_id": str(pick(device, "dev_id", "device_id", "dev_id", default="") or ""),
                        "ip": pick(device, "ip", default="") or "",
                    }
                )
            self.send_json({"ok": True, "devices": out})
            return
        if path == "/api/cameras":
            cams = call(STORE, "list", "list", default=[]) or []
            self.send_json({"ok": True, "cameras": [cam_json(c) for c in cams]})
            return
        if path == "/api/record/status":
            read_status = MODS["read_status"]
            status = read_status() if read_status else {}
            self.send_json({"ok": True, "status": status or {}, "hub": HUB.status()})
            return
        if path.startswith("/api/cameras/"):
            parts = path[len("/api/cameras/") :].split("/")
            try:
                cam_id = int(parts[0])
            except ValueError:
                self.send_json({"ok": False, "error": "bad id"}, 400)
                return
            if len(parts) == 1:
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing"}, 404)
                    return
                self.send_json({"ok": True, "camera": cam_json(cam)})
                return
            if parts[1] == "snapshot":
                jpeg = HUB.snapshot(cam_id)
                for _ in range(40):
                    if jpeg:
                        break
                    time.sleep(0.1)
                    jpeg = HUB.snapshot(cam_id)
                if not jpeg:
                    self.send_json({"ok": False, "error": "no frame yet"}, 404)
                    return
                self.send_response(200)
                self.cors()
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
                return
            if parts[1] == "h264":
                ses = HUB.session(cam_id)
                if ses is None:
                    self.send_json({"ok": False, "error": "missing camera"}, 404)
                    return
                deadline = time.time() + 3
                while ses.alive and ses.last_iframe is None and time.time() < deadline:
                    time.sleep(0.02)
                q = ses.subscribe()
                try:
                    self.send_response(200)
                    self.cors()
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    codec = 1 if (ses.codec or "") == "hevc" else 0
                    self.wfile.write(bytes([codec]))
                    self.wfile.flush()
                    while ses.alive:
                        try:
                            is_iframe, payload = q.get(timeout=1.0)
                        except queue.Empty:
                            continue
                        self.wfile.write(bytes([1 if is_iframe else 0]) + struct.pack(">I", len(payload)) + payload)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
                finally:
                    ses.unsubscribe(q)
                return
            if parts[1] == "mjpeg":
                ses = HUB.session(cam_id)
                if ses is None:
                    self.send_json({"ok": False, "error": "missing camera"}, 404)
                    return
                boundary = "frame"
                self.send_response(200)
                self.cors()
                self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                last = b""
                try:
                    while ses.alive:
                        jpeg = ses.jpeg
                        if jpeg and jpeg is not last:
                            last = jpeg
                            chunk = (
                                f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                                f"Content-Length: {len(jpeg)}\r\n\r\n"
                            ).encode("ascii") + jpeg + b"\r\n"
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            time.sleep(0.01)
                        else:
                            time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
                return
        self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/login":
            body = self.read_json()
            user = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            if not check_user(user, password):
                self.send_json({"ok": False, "error": "Wrong username or password"}, 401)
                return
            self.send_json({"ok": True, "token": new_token(user), "user": user, "ips": local_ips(), "port": PORT})
            return
        if not self.need_user():
            return
        if path == "/api/cameras":
            saved = call(STORE, "upsert", "upsert", args=(camera_from_body(self.read_json()),))
            self.kick_record()
            self.send_json({"ok": True, "camera": cam_json(saved)})
            return
        if path.startswith("/api/cameras/"):
            parts = path[len("/api/cameras/") :].split("/")
            try:
                cam_id = int(parts[0])
            except ValueError:
                self.send_json({"ok": False, "error": "bad id"}, 400)
                return
            if len(parts) == 2 and parts[1] == "auto_record":
                enabled = bool(self.read_json().get("enabled"))
                call(STORE, "set_auto_record", "set_auto_record", args=(cam_id, enabled))
                HUB.drop(cam_id)
                self.kick_record()
                cam = call(STORE, "get", "get", args=(cam_id,))
                self.send_json({"ok": True, "camera": cam_json(cam) if cam else None})
                return
            ses = HUB.session(cam_id)
            if ses is None:
                self.send_json({"ok": False, "error": "missing camera"}, 404)
                return
            if len(parts) == 2 and parts[1] == "command":
                name = str(self.read_json().get("name") or "")
                self.send_json({"ok": ses.command(name), "name": name})
                return
            if parts[-2:] == ["talk", "start"]:
                self.send_json({"ok": ses.talk_start()})
                return
            if parts[-2:] == ["talk", "audio"]:
                self.send_json({"ok": ses.talk_audio(self.read_raw())})
                return
            if parts[-2:] == ["talk", "stop"]:
                ses.talk_stop()
                self.send_json({"ok": True})
                return
            if len(parts) == 2 and parts[1] == "alert":
                if self.read_json().get("on"):
                    self.send_json({"ok": ses.alert_on()})
                else:
                    ses.alert_off()
                    self.send_json({"ok": True})
                return
        self.send_json({"ok": False, "error": "not found"}, 404)

    def do_PUT(self) -> None:
        if not self.need_user():
            return
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/cameras/"):
            self.send_json({"ok": False, "error": "not found"}, 404)
            return
        try:
            cam_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_json({"ok": False, "error": "bad id"}, 400)
            return
        existing = call(STORE, "get", "get", args=(cam_id,))
        if existing is None:
            self.send_json({"ok": False, "error": "missing"}, 404)
            return
        body = self.read_json()
        body["id"] = cam_id
        saved = call(STORE, "update", "update", args=(camera_from_body(body, existing),))
        HUB.drop(cam_id)
        self.kick_record()
        self.send_json({"ok": True, "camera": cam_json(saved)})

    def do_DELETE(self) -> None:
        if not self.need_user():
            return
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/cameras/"):
            self.send_json({"ok": False, "error": "not found"}, 404)
            return
        try:
            cam_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_json({"ok": False, "error": "bad id"}, 400)
            return
        HUB.drop(cam_id)
        call(STORE, "delete", "delete", args=(cam_id,))
        self.kick_record()
        self.send_json({"ok": True})


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REC_DIR.mkdir(parents=True, exist_ok=True)
    ensure_admin()
    ensure = MODS["ensure_worker"]
    if ensure:
        try:
            ensure()
        except Exception as exc:
            print("[studio] record worker:", exc)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("V380 studio server — Windows UI connects here")
    print(f"Port: {PORT}")
    for ip in local_ips():
        print(f"On Windows: Server IP {ip}  then login")
    print("Ctrl+C to stop")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
