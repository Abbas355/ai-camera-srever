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
PORT = int(os.environ.get("V380_PORT", "8080"))
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
    from v380.client import ezviz_rtsp as ez
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
        "Decoder": pick(vc, "make_live_decoder", "LiveH264Decoder", "LiveH264Decoder"),
        "read_status": pick(ar, "read_worker_status", "read_worker_status"),
        "ensure_worker": pick(ar, "ensure_worker", "ensure_worker"),
        "ezviz_client": ez.EzvizRtspClient,
        "ezviz_probe": ez.probe_rtsp,
        "ezviz_resolve": ez.resolve_rtsp_url,
        "ezviz_brand": ez.normalize_brand,
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
        "brand": str(cam_get(cam, "brand", default="v380") or "v380"),
        "rtsp_url": str(cam_get(cam, "rtsp_url", default="") or ""),
        "brand_name": str(cam_get(cam, "brand_name", default="V380") or "V380"),
        "created_at": cam.created_at,
        "updated_at": cam.updated_at,
        "quality_name": str(cam_get(cam, "quality_name", "quality_name", default="HD")),
        "source_name": str(cam_get(cam, "source_name", "source_name", default="LAN")),
    }


def ping_camera(cam) -> dict:
    """Probe V380 TCP login or EZVIZ RTSP."""
    t0 = time.time()
    brand_fn = MODS.get("ezviz_brand")
    brand = brand_fn(cam_get(cam, "brand", default="v380")) if callable(brand_fn) else "v380"
    if brand == "ezviz":
        resolve = MODS.get("ezviz_resolve")
        probe = MODS.get("ezviz_probe")
        url = resolve(cam) if callable(resolve) else ""
        if not url:
            return {
                "ok": False,
                "reachable": False,
                "online": False,
                "error": "Missing EZVIZ RTSP URL / IP",
                "ms": int((time.time() - t0) * 1000),
            }
        out = probe(url) if callable(probe) else {"ok": False, "error": "EZVIZ probe missing"}
        out.setdefault("ms", int((time.time() - t0) * 1000))
        out["brand"] = "ezviz"
        return out

    Client = MODS["Client"]
    relay = MODS["relay"]
    device_id = int(cam_get(cam, "device_id", "device_id") or 0)
    source = str(cam_get(cam, "source", default="lan") or "lan")
    port = int(cam_get(cam, "port", default=8800) or 8800)
    host = str(cam_get(cam, "ip", default="") or "")
    if source == "cloud" and relay:
        try:
            host = relay(device_id) or ""
        except Exception as exc:
            return {
                "ok": False,
                "reachable": False,
                "online": False,
                "error": f"Relay lookup failed: {exc}",
                "ms": int((time.time() - t0) * 1000),
            }
        if not host:
            return {
                "ok": False,
                "reachable": False,
                "online": False,
                "error": "No cloud relay",
                "ms": int((time.time() - t0) * 1000),
            }
    if not host:
        return {
            "ok": False,
            "reachable": False,
            "online": False,
            "error": "No camera IP",
            "ms": int((time.time() - t0) * 1000),
        }
    try:
        with socket.create_connection((host, port), timeout=3):
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
    client = None
    try:
        client = Client(
            host,
            device_id,
            cam_get(cam, "username", default="") or "",
            cam_get(cam, "password", default="") or "",
            port,
            quality=1,
            source=source,
        )
        call(client, "connect", "connect")
        return {
            "ok": True,
            "reachable": True,
            "online": True,
            "host": host,
            "port": port,
            "brand": "v380",
            "ms": int((time.time() - t0) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reachable": True,
            "online": False,
            "host": host,
            "port": port,
            "error": str(exc),
            "brand": "v380",
            "ms": int((time.time() - t0) * 1000),
        }
    finally:
        if client is not None:
            call(client, "close", "close")


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


def update_profile(
    current_user: str,
    *,
    current_password: str,
    new_username: str | None = None,
    new_password: str | None = None,
) -> dict:
    """Change studio login username and/or password. Requires current password."""
    user = (current_user or "").strip()
    if not user:
        raise RuntimeError("Not logged in")
    if not check_user(user, current_password or ""):
        raise RuntimeError("Current password is wrong")

    want_user = (new_username or "").strip() or user
    want_pass = new_password if new_password is not None else ""
    change_user = want_user != user
    change_pass = bool(want_pass)

    if not change_user and not change_pass:
        raise RuntimeError("Nothing to update")
    if change_user:
        if len(want_user) < 3:
            raise RuntimeError("Username must be at least 3 characters")
        if any(ch.isspace() for ch in want_user):
            raise RuntimeError("Username cannot contain spaces")
        taken = STORE._conn.execute(
            "SELECT id FROM users WHERE username = ? AND username != ?",
            (want_user, user),
        ).fetchone()
        if taken:
            raise RuntimeError("That username is already taken")
    if change_pass and len(want_pass) < 4:
        raise RuntimeError("New password must be at least 4 characters")

    row = STORE._conn.execute("SELECT id FROM users WHERE username = ?", (user,)).fetchone()
    if not row:
        raise RuntimeError("User not found")

    if change_pass:
        salt = os.urandom(16)
        STORE._conn.execute(
            "UPDATE users SET username = ?, pass_salt = ?, pass_hash = ? WHERE id = ?",
            (want_user, salt, hash_pw(want_pass, salt), row["id"]),
        )
    elif change_user:
        STORE._conn.execute(
            "UPDATE users SET username = ? WHERE id = ?",
            (want_user, row["id"]),
        )
    STORE._conn.commit()

    if change_user:
        with _sessions_lock:
            for rec in _sessions.values():
                if rec.get("user") == user:
                    rec["user"] = want_user

    return {"ok": True, "user": want_user, "username_changed": change_user, "password_changed": change_pass}


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
    brand_fn = MODS.get("ezviz_brand")
    raw_brand = body.get("brand") or (cam_get(existing, "brand", default="v380") if existing else "v380")
    brand = brand_fn(raw_brand) if callable(brand_fn) else (
        "ezviz" if str(raw_brand or "").lower() == "ezviz" else "v380"
    )
    device_id = str(
        body.get("device_id")
        or (cam_get(existing, "device_id", "device_id") if existing else "")
        or ""
    )
    password = body.get("password")
    if not password and existing is not None:
        password = existing.password
    rtsp_url = str(body.get("rtsp_url") or (cam_get(existing, "rtsp_url", default="") if existing else "") or "")
    ip = str(body.get("ip") or (existing.ip if existing else "") or "")
    username = str(body.get("username") or (existing.username if existing else "") or ("admin" if brand == "ezviz" else ""))
    port_default = 554 if brand == "ezviz" else 8800
    port = int(body.get("port") or (existing.port if existing else port_default) or port_default)
    if brand == "ezviz":
        if not device_id:
            from v380.client.ezviz_rtsp import synthetic_device_id

            device_id = synthetic_device_id(ip, rtsp_url)
        if not rtsp_url and ip:
            from v380.client.ezviz_rtsp import build_rtsp_url

            rtsp_url = build_rtsp_url(ip, str(password or ""), username=username, port=port)
    return Camera(
        id=int(body.get("id") or (existing.id if existing else 0)),
        name=str(body.get("name") or device_id),
        device_id=device_id,
        mac=str(body.get("mac") or (existing.mac if existing else "") or ""),
        ip=ip,
        port=port,
        username=username,
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
            or (cam_get(existing, "record_chunk", "record_chunk", default="hour") if existing else "hour")
            or "hour"
        ),
        created_at=existing.created_at if existing else "",
        updated_at=existing.updated_at if existing else "",
        brand=brand,
        rtsp_url=rtsp_url,
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
        self._need_keyframe = False
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
        brand_fn = MODS.get("ezviz_brand")
        brand = brand_fn(cam_get(cam, "brand", default="v380")) if callable(brand_fn) else "v380"
        if brand == "ezviz":
            self._run_ezviz()
            return
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
                            decoder = Decoder(frames, fmt=fmt, scale_width=1280, jpeg_q=3, threads=2, output="jpeg")
                        except TypeError:
                            try:
                                decoder = Decoder(frames, fmt=fmt, scale_width=1280, jpeg_q=3, threads=2)
                            except TypeError:
                                decoder = Decoder(frames, fmt=fmt)
                    if is_iframe:
                        got_key = True
                        if self._need_keyframe:
                            if decoder is not None:
                                call(decoder, "close", "close")
                            try:
                                decoder = Decoder(
                                    frames, fmt=self.codec, scale_width=1280, jpeg_q=3, threads=2, output="jpeg"
                                )
                            except TypeError:
                                decoder = Decoder(frames, fmt=self.codec)
                            self._need_keyframe = False
                    if not got_key:
                        continue
                    if self._need_keyframe and not is_iframe:
                        continue
                    rec = payload
                    prep = pick(client, "h264_for_decode", "h264_for_decode")
                    if is_iframe and prep:
                        rec = prep(payload)
                    self._emit_h264(bool(is_iframe), rec)
                    self._jpeg_n += 1
                    with self._sub_lock:
                        live_watchers = bool(self._subs)
                    # Decode every frame while someone is watching — smoother live, fewer gray glitches.
                    if live_watchers or is_iframe or self._jpeg_n % 3 == 0:
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

    def _run_ezviz(self) -> None:
        """EZVIZ path: RTSP → JPEG for snapshot + MJPEG live."""
        EzClient = MODS.get("ezviz_client")
        resolve = MODS.get("ezviz_resolve")
        cam = self.cam
        while self.alive and not self._stop.is_set():
            client = None
            try:
                url = resolve(cam) if callable(resolve) else ""
                if not url:
                    raise RuntimeError("Missing EZVIZ RTSP URL")
                self.state = "Connecting EZVIZ…"
                client = EzClient(url) if callable(EzClient) else None
                if client is None:
                    raise RuntimeError("EZVIZ client missing")
                call(client, "connect", "connect")
                self._client = client
                self.state = "Live (EZVIZ)"
                self.codec = "jpeg"
                for item in client.iter_video_frames(self._stop):
                    if not self.alive:
                        break
                    if not (isinstance(item, tuple) and len(item) == 3):
                        continue
                    kind, _is_iframe, payload = item
                    if kind != "video" or not payload:
                        continue
                    self.jpeg = payload
                    self._jpeg_n += 1
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.state = f"Offline ({exc})"
                print("[studio] ezviz", getattr(cam, "name", "?"), ":", exc)
                self._client = None
                time.sleep(2.5)
            finally:
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

    def _is_ezviz(self) -> bool:
        brand_fn = MODS.get("ezviz_brand")
        brand = brand_fn(cam_get(self.cam, "brand", default="v380")) if callable(brand_fn) else "v380"
        return brand == "ezviz"

    def command(self, name: str, hold: float = 0.0) -> bool:
        if self._is_ezviz():
            return False
        # Wait briefly — live session may still be connecting.
        for _ in range(50):
            if self._client is not None:
                break
            time.sleep(0.05)
        if self._client is None:
            return False
        if str(name) == "ptz_calibrate":
            call(self._client, "send_control", "send_control", args=("ptz_calibrate",), default=False)
            call(self._client, "send_control", "send_control", args=("ptz_calibrate_alt",), default=False)
            self._need_keyframe = True
            return True
        if str(name).startswith("preset_set_"):
            try:
                slot = int(str(name).split("_")[-1])
            except ValueError:
                return False
            fn = pick(self._client, "preset_set", "preset_set")
            return bool(fn(slot)) if callable(fn) else False
        if str(name).startswith("preset_call_"):
            try:
                slot = int(str(name).split("_")[-1])
            except ValueError:
                return False
            fn = pick(self._client, "preset_call", "preset_call")
            return bool(fn(slot)) if callable(fn) else False

        hold = max(0.0, min(30.0, float(hold or 0.0)))
        if hold > 0.05 and str(name).startswith("ptz_") and str(name) != "ptz_stop":
            # Keepalive on the live socket — V380 motors stop if START is not refreshed.
            ok = bool(call(self._client, "send_control", "send_control", args=(name,), default=False))
            if not ok:
                return False
            deadline = time.time() + hold
            while time.time() < deadline:
                time.sleep(min(0.28, max(0.0, deadline - time.time())))
                if time.time() >= deadline:
                    break
                call(self._client, "send_control", "send_control", args=(name,), default=False)
            call(self._client, "send_control", "send_control", args=("ptz_stop",), default=False)
            self._need_keyframe = True
            return True

        if str(name) == "ptz_stop":
            self._need_keyframe = True
        return bool(call(self._client, "send_control", "send_control", args=(name,), default=False))

    def talk_start(self) -> bool:
        if self._is_ezviz():
            return False
        for _ in range(6):
            if self._client is not None:
                break
            time.sleep(0.05)
        if self._client is None:
            return False
        return bool(call(self._client, "start_talk", "start_talk", default=False))

    def talk_audio(self, ima: bytes) -> bool:
        if self._is_ezviz() or self._client is None:
            return False
        return bool(call(self._client, "send_talk_audio", "send_talk_audio", args=(ima,), default=False))

    def talk_stop(self) -> None:
        if self._is_ezviz():
            return
        if self._client is not None:
            call(self._client, "stop_talk", "stop_talk")

    def alert_on(self) -> bool:
        if self._is_ezviz():
            return False
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
            if parts[1] == "ping":
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing"}, 404)
                    return
                self.send_json(ping_camera(cam))
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
                # EZVIZ Hub sessions are JPEG/MJPEG only — fail fast so clients fall back.
                if (ses.codec or "") == "jpeg" or ses._is_ezviz():
                    self.send_json({"ok": False, "error": "mjpeg only"}, 404)
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
        if path == "/api/profile":
            body = self.read_json()
            me = auth_user(self.token()) or ""
            try:
                out = update_profile(
                    me,
                    current_password=str(body.get("current_password") or body.get("password") or ""),
                    new_username=str(body.get("username") or body.get("new_username") or "").strip() or None,
                    new_password=str(body.get("new_password") or "") or None,
                )
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json(out)
            return
        if path == "/api/cameras/probe":
            cam = camera_from_body(self.read_json())
            self.send_json(ping_camera(cam))
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
                body = self.read_json()
                name = str(body.get("name") or "")
                hold = float(body.get("hold") or 0)
                self.send_json({"ok": ses.command(name, hold=hold), "name": name, "hold": hold})
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
