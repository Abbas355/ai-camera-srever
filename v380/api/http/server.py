"""See Me HTTP API implementation.

This module holds the running server logic (handler, live hub, auth helpers).
Import through v380.api.* facades for a clear package layout.
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
from urllib.parse import parse_qs, quote, unquote, urlparse

from v380.brand import APP_ID, APP_NAME
from v380.paths import DATA_DIR, REC_DIR, ROOT as APP_DIR
PORT = int(os.environ.get("V380_PORT", "8080"))
VIDEO_EXT = {".mp4", ".h264", ".h265", ".ts"}


def _is_hls_junk(path: Path) -> bool:
    """HLS packaging next to recordings must not appear in the clip index."""
    if any(part.endswith(".hls") for part in path.parts):
        return True
    name = path.name.lower()
    if name.startswith("seg_") and name.endswith((".ts", ".m4s", ".mp4")):
        return True
    if name in {"index.m3u8", "index.m3u"}:
        return True
    return False
TOKEN_TTL = 7 * 24 * 3600
DEFAULT_USER = os.environ.get("V380_ADMIN_USER", "admin")
DEFAULT_PASS = os.environ.get("V380_ADMIN_PASS", "1290")


def debug_login_allowed() -> bool:
    """Flutter debug builds may send debug=true to skip password locally.

    Default on for studio; set V380_DEBUG=0 (or OPTIVIEW_DEBUG=0) to disable.
    """
    raw = (
        os.environ.get("V380_DEBUG")
        or os.environ.get("OPTIVIEW_DEBUG")
        or "1"
    ).strip().lower()
    return raw not in {"0", "false", "no", "off"}

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


def _camera_ezviz_username(cam) -> str:
    """Account linked to this camera. Never the last account used by another camera."""
    fresh = call(STORE, "get", "get", args=(int(getattr(cam, "id", 0) or 0),)) or cam
    aid = int(cam_get(fresh, "ezviz_account_id", default=0) or 0)
    if not aid:
        return ""
    from v380.store.ezviz_accounts import EzvizAccountStore

    rec = EzvizAccountStore().get(aid)
    return str(rec.get("username") or "").strip() if rec else ""


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
        "record_chunk": str(cam_get(cam, "record_chunk", "record_chunk", default="minute") or "minute"),
        "brand": str(cam_get(cam, "brand", default="v380") or "v380"),
        "rtsp_url": str(cam_get(cam, "rtsp_url", default="") or ""),
        "ezviz_account_id": int(cam_get(cam, "ezviz_account_id", default=0) or 0),
        "ezviz_serial": str(cam_get(cam, "ezviz_serial", default="") or ""),
        "ezviz_username": _camera_ezviz_username(cam),
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
        # If hub already has a live session, don't open another RTSP just to probe.
        try:
            cam_id = int(cam_get(cam, "id", default=0) or 0)
        except (TypeError, ValueError):
            cam_id = 0
        if cam_id:
            with HUB._lock:
                ses = HUB._live.get(cam_id)
            if ses is not None and ses.alive and ses.jpeg:
                return {
                    "ok": True,
                    "reachable": True,
                    "online": True,
                    "brand": "ezviz",
                    "via": "hub",
                    "ms": int((time.time() - t0) * 1000),
                }
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
        # Status ping must stay TCP-only — a full RTSP grab fights live/record and
        # shows "Online · 9000 ms" while the mosaic waits for video.
        try:
            out = probe(url, timeout=1.5, grab=False) if callable(probe) else {"ok": False, "error": "EZVIZ probe missing"}
        except TypeError:
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


def list_users() -> list[dict]:
    rows = STORE._conn.execute(
        "SELECT id, username, created_at FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "username": str(r["username"]),
            "created_at": str(r["created_at"] or ""),
        }
        for r in rows
    ]


def create_user(username: str, password: str) -> dict:
    user = (username or "").strip()
    if len(user) < 3:
        raise RuntimeError("Username must be at least 3 characters")
    if any(ch.isspace() for ch in user):
        raise RuntimeError("Username cannot contain spaces")
    if len(password or "") < 4:
        raise RuntimeError("Password must be at least 4 characters")
    taken = STORE._conn.execute("SELECT id FROM users WHERE username = ?", (user,)).fetchone()
    if taken:
        raise RuntimeError("That username is already taken")
    salt = os.urandom(16)
    cur = STORE._conn.execute(
        "INSERT INTO users (username, pass_salt, pass_hash) VALUES (?, ?, ?)",
        (user, salt, hash_pw(password, salt)),
    )
    STORE._conn.commit()
    return {"ok": True, "id": int(cur.lastrowid), "username": user}


def delete_user(username: str, *, actor: str) -> dict:
    target = (username or "").strip()
    me = (actor or "").strip()
    if not target:
        raise RuntimeError("Username required")
    if target == me:
        raise RuntimeError("You cannot delete your own account while signed in")
    count = STORE._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if count and int(count["n"]) <= 1:
        raise RuntimeError("Cannot delete the last user")
    row = STORE._conn.execute("SELECT id FROM users WHERE username = ?", (target,)).fetchone()
    if not row:
        raise RuntimeError("User not found")
    STORE._conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
    STORE._conn.commit()
    with _sessions_lock:
        dead = [tok for tok, rec in _sessions.items() if rec.get("user") == target]
        for tok in dead:
            _sessions.pop(tok, None)
    return {"ok": True, "username": target}


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


def clip_index(
    *,
    camera: str | None = None,
    date: str | None = None,
    summary: bool = False,
) -> dict:
    """Build clip index.

    - summary=True: cameras → date keys only (folder scan, no file lists).
    - camera+date: only that day's clips (lazy load for player).
    - otherwise: full index (desktop / legacy).
    """
    cam_filter = (camera or "").strip() or None
    date_filter = (date or "").strip() or None
    cameras: dict[str, dict[str, list[dict]]] = {}

    if not REC_DIR.is_dir():
        return {"ok": True, "port": PORT, "ips": local_ips(), "cameras": cameras, "summary": summary}

    # Fast path: only list camera/date folders — no rglob of every video.
    if summary:
        for cam_dir in REC_DIR.iterdir():
            if not cam_dir.is_dir():
                continue
            if cam_filter and cam_dir.name != cam_filter:
                continue
            for day_dir in cam_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                if date_filter and day_dir.name != date_filter:
                    continue
                cameras.setdefault(cam_dir.name, {}).setdefault(day_dir.name, [])
        return {"ok": True, "port": PORT, "ips": local_ips(), "cameras": cameras, "summary": True}

    # Narrow filesystem walk when a single day (or camera) is requested.
    roots: list[Path] = []
    if cam_filter and date_filter:
        day_dir = REC_DIR / cam_filter / date_filter
        if day_dir.is_dir():
            roots = [day_dir]
    elif cam_filter:
        cam_dir = REC_DIR / cam_filter
        if cam_dir.is_dir():
            roots = [cam_dir]
    else:
        roots = [REC_DIR]

    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
                continue
            if _is_hls_junk(path):
                continue
            try:
                rel = path.relative_to(REC_DIR)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) < 2:
                continue
            cam_name = parts[0]
            day = parts[1] if len(parts) >= 3 else "other"
            if cam_filter and cam_name != cam_filter:
                continue
            if date_filter and day != date_filter:
                continue
            cameras.setdefault(cam_name, {}).setdefault(day, []).append(
                {"rel": rel.as_posix(), "name": path.name, "size": path.stat().st_size}
            )

    return {"ok": True, "port": PORT, "ips": local_ips(), "cameras": cameras, "summary": False}


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
            else (cam_get(existing, "auto_record", "auto_record", default=True) if existing else True)
        ),
        record_chunk=str(
            body.get("record_chunk")
            or (cam_get(existing, "record_chunk", "record_chunk", default="minute") if existing else "minute")
            or "minute"
        ),
        created_at=existing.created_at if existing else "",
        updated_at=existing.updated_at if existing else "",
        brand=brand,
        rtsp_url=rtsp_url,
        ezviz_account_id=int(
            body["ezviz_account_id"]
            if body.get("ezviz_account_id") is not None
            else (cam_get(existing, "ezviz_account_id", default=0) if existing else 0)
            or 0
        ),
        ezviz_serial=str(
            body.get("ezviz_serial")
            if body.get("ezviz_serial") is not None
            else (cam_get(existing, "ezviz_serial", default="") if existing else "")
            or ""
        ),
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
        self._jpeg_event = threading.Event()
        self._need_keyframe = False
        self._last_preview_save = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"hub-{cam.id}")

    def _set_jpeg(self, jpeg: bytes) -> None:
        if not jpeg:
            return
        self.jpeg = jpeg
        self._jpeg_n += 1
        self._jpeg_event.set()
        # Persist last frame ~every 2s for instant mosaic after restart/reconnect.
        now = time.time()
        if now - self._last_preview_save >= 2.0:
            self._last_preview_save = now
            try:
                from v380.runtime_cache import save_preview

                save_preview(int(getattr(self.cam, "id", 0) or 0), jpeg)
            except Exception:
                pass

    def start(self) -> None:
        self._thread.start()
        if self._is_ezviz():
            self.arm_control()

    def arm_control(self) -> None:
        """Open the control channel once, so later button presses are not a fresh login."""
        thread = getattr(self, "_ctrl_thread", None)
        if thread is not None and thread.is_alive():
            return
        self._ctrl_q = queue.Queue()
        self._ctrl_thread = threading.Thread(
            target=self._control_loop, daemon=True, name=f"ctrl-{getattr(self.cam, 'id', 0)}"
        )
        self._ctrl_thread.start()

    def _control_loop(self) -> None:
        try:
            account = _camera_ezviz_username(self.cam)
            if account:
                from v380.client.ezviz_session import get_client, using_account

                with using_account(account):
                    get_client()
                self._last_cmd_msg = "Ready"
        except Exception as exc:
            self._last_cmd_msg = str(exc)
        try:
            fresh = call(STORE, "get", "get", args=(int(getattr(self.cam, "id", 0) or 0),)) or self.cam
            from v380.client.ezviz_control import send_ptz

            send_ptz(
                str(getattr(fresh, "ip", "") or ""),
                str(getattr(fresh, "username", "") or "admin"),
                str(getattr(fresh, "password", "") or ""),
                "ptz_stop",
                ports=(80,),
            )
        except Exception:
            pass
        while self.alive and not self._stop.is_set():
            try:
                name, hold = self._ctrl_q.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                self._ezviz_command(name, hold)
            except Exception as exc:
                self._last_cmd_msg = str(exc)

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
                    # MJPEG / Floor / Home tiles read ses.jpeg — keep it fresher
                    # (every 2nd frame) so live preview stays smooth.
                    want_jpeg = is_iframe or self._jpeg_n % 2 == 0
                    if want_jpeg:
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
                            self._set_jpeg(frames.get_nowait())
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
        """EZVIZ path: RTSP → JPEG for snapshot + MJPEG live.

        Prefer substream (102) so auto-record can keep main (101) without fighting
        for the camera's limited RTSP sessions.
        """
        EzClient = MODS.get("ezviz_client")
        resolve = MODS.get("ezviz_resolve")
        cam = self.cam
        prefer_sub = True
        while self.alive and not self._stop.is_set():
            client = None
            try:
                stream = "sub" if prefer_sub else "main"
                try:
                    url = resolve(cam, stream=stream) if callable(resolve) else ""
                except TypeError:
                    url = resolve(cam) if callable(resolve) else ""
                    if prefer_sub and url:
                        url = url.replace("/Channels/101", "/Channels/102")
                if not url:
                    raise RuntimeError("Missing EZVIZ RTSP URL")
                self.state = f"Connecting EZVIZ ({stream})…"
                client = EzClient(url) if callable(EzClient) else None
                if client is None:
                    raise RuntimeError("EZVIZ client missing")
                call(client, "connect", "connect")
                self._client = client
                self.state = "Waiting for video…"
                self.codec = "jpeg"
                got = False
                for item in client.iter_video_frames(self._stop):
                    if not self.alive:
                        break
                    if not (isinstance(item, tuple) and len(item) == 3):
                        continue
                    kind, _is_iframe, payload = item
                    if kind != "video" or not payload:
                        continue
                    got = True
                    self.state = "Live (EZVIZ)"
                    self._set_jpeg(payload)
                if prefer_sub and not got and self.alive and not self._stop.is_set():
                    # Substream unavailable — fall back to main once.
                    prefer_sub = False
                    continue
            except Exception as exc:
                if self._stop.is_set():
                    break
                if prefer_sub:
                    prefer_sub = False
                    print("[studio] ezviz substream failed, trying main:", exc)
                    time.sleep(1.5)
                    continue
                self.state = f"No video ({exc})"
                print("[studio] ezviz", getattr(cam, "name", "?"), ":", exc)
                self._client = None
                time.sleep(8.0)
            finally:
                if client is not None:
                    call(client, "close", "close")
                if self._client is client:
                    self._client = None
        self.state = "Stopped"

    def subscribe(self) -> queue.Queue:
        # Larger buffer so a brief UI hiccup doesn't force mid-GOP drops.
        q: queue.Queue = queue.Queue(maxsize=12)
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
        """Fan out NALUs without destroying the GOP.

        Old behavior cleared the queue on every frame (kept only the newest NALU),
        which dropped P-frames mid-GOP and caused gray/stutter on remote live.
        """
        if not payload:
            return
        if is_iframe:
            self.last_iframe = payload
        with self._sub_lock:
            subs = list(self._subs)
        item = (is_iframe, payload)
        for q in subs:
            if is_iframe:
                # Keyframe: flush backlog so the client resyncs cleanly.
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass
            else:
                try:
                    q.put_nowait(item)
                except queue.Full:
                    # Client is behind — drop this P-frame; keep queued GOP intact.
                    pass

    def _is_ezviz(self) -> bool:
        brand_fn = MODS.get("ezviz_brand")
        brand = brand_fn(cam_get(self.cam, "brand", default="v380")) if callable(brand_fn) else "v380"
        return brand == "ezviz"

    def command(self, name: str, hold: float = 0.0) -> bool:
        self._last_cmd_msg = ""
        if self._is_ezviz():
            self.arm_control()
            self._ctrl_q.put((str(name), float(hold or 0)))
            self._last_cmd_msg = "Stopped" if name == "ptz_stop" else str(name).replace("_", " ")
            return True
        # Wait briefly — live session may still be connecting.
        for _ in range(50):
            if self._client is not None:
                break
            time.sleep(0.05)
        if self._client is None:
            self._last_cmd_msg = "Camera is still connecting. Wait for the picture, then try again."
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
        ok = bool(call(self._client, "send_control", "send_control", args=(name,), default=False))
        if not ok:
            self._last_cmd_msg = "Camera did not accept that control"
        return ok

    def _ezviz_command(self, name: str, hold: float = 0.0) -> bool:
        fresh = call(STORE, "get", "get", args=(int(getattr(self.cam, "id", 0) or 0),)) or self.cam
        ip = str(getattr(fresh, "ip", "") or "")
        serial = str(cam_get(fresh, "ezviz_serial", default="") or "")
        mac = str(getattr(fresh, "mac", "") or "")
        hint = str(getattr(fresh, "name", "") or "")
        try:
            from v380.client.ezviz_session import ptz, ptz_direction, run_action
        except Exception as exc:
            self._last_cmd_msg = str(exc)
            return False
        direction = ptz_direction(name)
        account = _camera_ezviz_username(fresh)
        # Move on the camera's own network. That is one short local call.
        # Light, siren, and the rest stay on the EZVIZ account.
        if direction or name == "ptz_stop":
            from v380.client.ezviz_control import send_ptz

            local_name = "ptz_stop" if name == "ptz_stop" else name
            user = str(getattr(fresh, "username", "") or "admin")
            password = str(getattr(fresh, "password", "") or "")
            if send_ptz(ip, user, password, local_name, ports=(80,)):
                self._ezviz_local = True
                self._last_cmd_msg = "Stopped" if name == "ptz_stop" else (direction or name)
                if direction:
                    self._ezviz_ptz = direction
                else:
                    self._ezviz_ptz = None
                return True
        # Local ONVIF/ISAPI tries several ports and can take many seconds.
        # A linked EZVIZ account is the path that actually moves this camera.
        if not account:
            client = self._client
            if client is not None and getattr(self, "_ezviz_local", None) is not False and hasattr(client, "send_control"):
                try:
                    if bool(client.send_control(name)):
                        self._ezviz_local = True
                        self._last_cmd_msg = "Stopped" if name == "ptz_stop" else name.replace("_", " ")
                        if name.startswith("ptz_") and name != "ptz_stop":
                            self._ezviz_ptz = direction or name
                        return True
                    self._ezviz_local = False
                except Exception as exc:
                    self._ezviz_local = False
                    self._last_cmd_msg = str(exc)
            self._last_cmd_msg = "Sign in with the EZVIZ app account for this camera"
            return False
        try:
            from v380.client.ezviz_session import using_account

            with using_account(account):
                if name == "ptz_stop":
                    last = getattr(self, "_ezviz_ptz", None)
                    if last:
                        ptz(ip, last, "STOP", serial=serial, mac=mac, name=hint)
                    self._ezviz_ptz = None
                    self._last_cmd_msg = "Stopped"
                    return True
                if direction:
                    ptz(ip, direction, "START", serial=serial, mac=mac, name=hint)
                    self._ezviz_ptz = direction
                    if hold > 0.05:
                        time.sleep(min(hold, 30.0))
                        ptz(ip, direction, "STOP", serial=serial, mac=mac, name=hint)
                        self._ezviz_ptz = None
                    self._last_cmd_msg = direction
                    return True
                self._last_cmd_msg = str(run_action(ip, name, serial=serial, mac=mac, name_hint=hint))
                return True
        except Exception as exc:
            self._last_cmd_msg = str(exc)
            return False

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
        # HD face grabs (main stream) — throttled so we don't fight recording RTSP.
        self._face_lock = threading.Lock()
        self._face_jpeg: dict[int, tuple[float, bytes]] = {}
        self._face_inflight: set[int] = set()

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

    def warm_all(self) -> None:
        """Keep RTSP/live sessions open for every camera (fast mosaic + View)."""
        cams = call(STORE, "list", "list", default=[]) or []
        wanted = {int(c.id) for c in cams if getattr(c, "id", None)}
        with self._lock:
            for cam_id in list(self._live.keys()):
                if cam_id not in wanted:
                    ses = self._live.pop(cam_id, None)
                    if ses:
                        try:
                            ses.stop()
                        except Exception:
                            pass
        for cam in cams:
            try:
                cid = int(cam.id)
            except (TypeError, ValueError):
                continue
            if not cid:
                continue
            try:
                self.session(cid)
            except Exception as exc:
                print("[studio] warm hub", cid, ":", exc)

    def drop(self, cam_id: int) -> None:
        with self._lock:
            ses = self._live.pop(cam_id, None)
        if ses:
            ses.stop()

    def live_jpeg(self, cam_id: int) -> bytes | None:
        """Current in-memory live frame only — never disk cache / HD grab."""
        ses = self.session(int(cam_id))
        if ses and ses.jpeg:
            return ses.jpeg
        return None

    def snapshot(self, cam_id: int) -> bytes | None:
        live = self.live_jpeg(cam_id)
        if live:
            return live
        # Disk preview only as a tiny bridge while the hub is still warming up.
        # (Old default was 600s — that made Add staff / tiles look 10–30s behind.)
        try:
            from v380.runtime_cache import load_preview

            return load_preview(cam_id, max_age_sec=1.5)
        except Exception:
            return None

    def attendance_jpeg(self, cam_id: int) -> bytes | None:
        """Fresh live frame for enroll / identify / check-in.

        Never serve disk preview_cache or a multi-second HD main-stream cache —
        those are the usual cause of Add staff looking 10–30s behind Live.

        Returns immediately (no sleep loop) so face-AI polls do not stall MJPEG.
        Dual-lens stacked frames are cropped to the lower (face-level) pane.
        """
        jpeg = self.live_jpeg(int(cam_id))
        if not jpeg:
            return None
        try:
            from v380.attendance.frame_util import normalize_attendance_frame

            return normalize_attendance_frame(jpeg)
        except Exception:
            return jpeg

    def face_jpeg(self, cam_id: int, *, max_age_sec: float = 4.0) -> bytes | None:
        """Best frame for face AI: prefer cached HD main-stream grab, else live JPEG.

        Live EZVIZ stays on substream (102). Face scans briefly sample main (101)
        on a throttle so continuous recording is not disrupted.
        """
        cam_id = int(cam_id)
        now = time.time()
        with self._face_lock:
            cached = self._face_jpeg.get(cam_id)
            if cached and (now - cached[0]) <= float(max_age_sec) and cached[1]:
                return cached[1]
            start_grab = cam_id not in self._face_inflight
            if start_grab:
                self._face_inflight.add(cam_id)

        live = self.snapshot(cam_id)
        # Already-large live frames (V380 HD etc.) are good enough — skip main grab.
        if live and len(live) >= 120_000:
            with self._face_lock:
                self._face_jpeg[cam_id] = (now, live)
                self._face_inflight.discard(cam_id)
            return live

        if start_grab:
            threading.Thread(
                target=self._grab_face_hd,
                args=(cam_id,),
                daemon=True,
                name=f"face-hd-{cam_id}",
            ).start()

        # While HD grab runs (or if it fails), use live / last cache.
        # Prefer live over a cache older than ~1s so movers are not missed.
        with self._face_lock:
            cached = self._face_jpeg.get(cam_id)
            if cached and cached[1] and (now - cached[0]) <= min(1.0, float(max_age_sec)):
                return cached[1]
        if live:
            return live
        with self._face_lock:
            cached = self._face_jpeg.get(cam_id)
            if cached and cached[1]:
                return cached[1]
        return None

    def _grab_face_hd(self, cam_id: int) -> None:
        jpeg: bytes | None = None
        try:
            cam = call(STORE, "get", "get", args=(cam_id,))
            if cam is None:
                return
            brand_fn = MODS.get("ezviz_brand")
            brand_raw = getattr(cam, "brand", None) or ""
            try:
                brand = (
                    brand_fn(brand_raw) if callable(brand_fn) else str(brand_raw or "v380")
                )
            except Exception:
                brand = str(brand_raw or "v380")
            is_ez = str(brand).lower() == "ezviz"
            grab = None
            try:
                from v380.client.ezviz_rtsp import grab_jpeg as grab
            except Exception:
                grab = None
            urls: list[str] = []
            if is_ez:
                resolve = MODS.get("ezviz_resolve")
                if callable(resolve):
                    try:
                        u = resolve(cam, stream="main") or ""
                    except TypeError:
                        u = (resolve(cam) or "").replace("/Channels/102", "/Channels/101")
                    if u:
                        urls.append(u)
            # Camera may already store the main RTSP URL (…/101).
            for attr in ("rtsp_url", "url"):
                raw = getattr(cam, attr, None) or ""
                if isinstance(raw, str) and raw.startswith("rtsp://"):
                    urls.append(raw.replace("/Channels/102", "/Channels/101"))
            seen: set[str] = set()
            if callable(grab):
                for url in urls:
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    try:
                        jpeg = grab(url, timeout=3.5)
                    except Exception:
                        jpeg = None
                    if jpeg and len(jpeg) >= 40_000:
                        break
            # Prefer a real HD frame; only fall back to live substream if grab failed.
            if not jpeg or len(jpeg) < 40_000:
                live = self.snapshot(cam_id)
                if live and (not jpeg or len(live) > len(jpeg)):
                    jpeg = live
            if jpeg:
                with self._face_lock:
                    self._face_jpeg[cam_id] = (time.time(), jpeg)
        except Exception as exc:
            print("[studio] face_jpeg", cam_id, ":", exc)
        finally:
            with self._face_lock:
                self._face_inflight.discard(cam_id)

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
        xt = self.headers.get("X-Token")
        if xt:
            return xt
        # HLS segment fetches often drop custom headers — allow ?token= on /file/.
        try:
            qs = parse_qs(urlparse(self.path).query or "")
            tok = (qs.get("token") or [""])[0].strip()
            return tok or None
        except Exception:
            return None

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Token, X-Api-Key, Range",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")

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
        try:
            HUB.warm_all()
        except Exception as exc:
            print("[studio] warm hub:", exc)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/api/ping"):
            self.send_json({"ok": True, "ips": local_ips(), "port": PORT, "app": APP_ID})
            return
        # Public attendance share API — API key auth (no staff login).
        if path.startswith("/api/v1/attendance"):
            from v380.integrations.attendance_public import try_handle_public

            if try_handle_public(self, "GET", path):
                return
        if path == "/api/system/autostart":
            if not self.need_user():
                return
            try:
                from v380.windows_autostart import autostart_status

                self.send_json(autostart_status())
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/api/index":
            if not self.need_user():
                return
            qs = parse_qs(urlparse(self.path).query or "")
            cam_q = (qs.get("camera") or [""])[0].strip()
            date_q = (qs.get("date") or [""])[0].strip()
            summary_raw = (qs.get("summary") or ["0"])[0].strip().lower()
            summary = summary_raw in {"1", "true", "yes"}
            self.send_json(
                clip_index(
                    camera=cam_q or None,
                    date=date_q or None,
                    summary=summary,
                )
            )
            return
        if path == "/api/storage":
            if not self.need_user():
                return
            from v380.record.auto_record import storage_summary

            # Free space / keep policy for the recordings volume (HDD mount on server).
            self.send_json({"ok": True, **storage_summary(REC_DIR)})
            return
        if path == "/api/hls/prepare":
            if not self.need_user():
                return
            from v380.record.hls import ensure_hls, ensure_hls_async, hls_ready, playlist_for, playlist_rel

            qs = parse_qs(urlparse(self.path).query or "")
            rel = (qs.get("rel") or [""])[0].strip().lstrip("/")
            # wait=0 (default for UI): return immediately; build in background if needed.
            wait_raw = (qs.get("wait") or ["0"])[0].strip().lower()
            wait = wait_raw in {"1", "true", "yes"}
            if not rel or ".." in Path(rel).parts:
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
            if hls_ready(full):
                playlist = playlist_for(full)
            elif wait:
                playlist = ensure_hls(full)
            else:
                # Instant play path: client streams the original file while HLS builds.
                ensure_hls_async(full)
                self.send_json({"ok": False, "building": True, "file": rel})
                return
            if playlist is None:
                self.send_json({"ok": False, "error": "hls unavailable", "file": rel})
                return
            try:
                prel = playlist.relative_to(REC_DIR.resolve()).as_posix()
            except ValueError:
                prel = playlist_rel(rel)
            self.send_json({"ok": True, "playlist": prel, "file": rel})
            return
        if path == "/api/thumb":
            if not self.need_user():
                return
            from v380.record.thumbs import ensure_thumb

            qs = parse_qs(urlparse(self.path).query or "")
            rel = (qs.get("rel") or [""])[0].strip().lstrip("/")
            if not rel or ".." in Path(rel).parts:
                self.send_json({"ok": False, "error": "bad path"}, 400)
                return
            full = (REC_DIR / rel).resolve()
            try:
                full.relative_to(REC_DIR.resolve())
            except ValueError:
                self.send_json({"ok": False, "error": "bad path"}, 400)
                return
            thumb = ensure_thumb(full)
            if thumb is None or not thumb.is_file():
                self.send_json({"ok": False, "error": "no preview"}, 404)
                return
            data = thumb.read_bytes()
            self.send_response(200)
            self.cors()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=20")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/ezviz/session":
            if not self.need_user():
                return
            from v380.client.ezviz_session import has_session, load_account

            acc = load_account()
            self.send_json({
                "ok": True,
                "signed_in": bool(has_session()),
                "username": acc[0] if acc else "",
            })
            return
        if path == "/api/ezviz/accounts":
            if not self.need_user():
                return
            from v380.store.ezviz_accounts import EzvizAccountStore

            accs = EzvizAccountStore().list_full()
            self.send_json({"ok": True, "accounts": accs})
            return
        if path.startswith("/file/"):
            if not self.need_user():
                return
            # Drop ?query from URL so token= does not become part of the filename.
            file_path = urlparse(self.path).path
            rel = Path(unquote(file_path[len("/file/") :]))
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
            # Stream from disk (Range-aware) — do not load the whole file into RAM.
            size = int(full.stat().st_size)
            suf = full.suffix.lower()
            if suf == ".mp4":
                ctype = "video/mp4"
            elif suf in {".m3u8", ".m3u"}:
                ctype = "application/vnd.apple.mpegurl"
            elif suf == ".ts":
                ctype = "video/mp2t"
            else:
                ctype = "application/octet-stream"
            # Rewrite HLS playlists so each segment keeps ?token= (ffmpeg often drops headers).
            if suf in {".m3u8", ".m3u"}:
                tok = (self.token() or "").strip()
                try:
                    text = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    self.send_json({"ok": False, "error": "read failed"}, 500)
                    return
                if tok:
                    out_lines = []
                    for line in text.splitlines():
                        s = line.strip()
                        if s and not s.startswith("#"):
                            sep = "&" if "?" in s else "?"
                            line = f"{s}{sep}token={quote(tok, safe='')}"
                        out_lines.append(line)
                    text = "\n".join(out_lines) + "\n"
                raw = text.encode("utf-8")
                self.send_response(200)
                self.cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
                return
            start, end = 0, size - 1
            status = 200
            range_hdr = (self.headers.get("Range") or "").strip()
            if range_hdr.startswith("bytes=") and size > 0:
                spec = range_hdr[6:].split(",", 1)[0].strip()
                left, _, right = spec.partition("-")
                try:
                    if left == "":
                        # suffix bytes: bytes=-N
                        suffix = int(right or "0")
                        start = max(0, size - suffix)
                    else:
                        start = int(left)
                        end = int(right) if right else (size - 1)
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    status = 206
                except ValueError:
                    start, end = 0, size - 1
                    status = 200
            length = end - start + 1 if size > 0 else 0
            self.send_response(status)
            self.cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with full.open("rb") as fh:
                    fh.seek(start)
                    left = length
                    while left > 0:
                        chunk = fh.read(min(1024 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
            return
        if not self.need_user():
            return
        # Attendance is an isolated feature module — only handles /api/attendance/*.
        if path.startswith("/api/attendance"):
            from v380.attendance.routes import try_handle

            if try_handle(self, "GET", path):
                return
        if path.startswith("/api/integrations/attendance"):
            from v380.integrations.attendance_public import try_handle_manage

            if try_handle_manage(self, "GET", path):
                return
        if path.startswith("/api/customers"):
            from v380.customers.routes import try_handle

            if try_handle(self, "GET", path):
                return
        if path.startswith("/api/occupancy"):
            from v380.occupancy.routes import try_handle

            if try_handle(self, "GET", path):
                return
        if path == "/api/me":
            self.send_json({"ok": True, "user": auth_user(self.token())})
            return
        if path == "/api/users":
            self.send_json({"ok": True, "users": list_users(), "me": auth_user(self.token())})
            return
        if path == "/api/discover":
            discover = MODS["discover"]
            out = []
            seen: set[str] = set()
            try:
                devices = list(discover()) if discover else []
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
                return
            for device in devices:
                ip = pick(device, "ip", default="") or ""
                mac = pick(device, "mac", default="") or ""
                did = str(pick(device, "dev_id", "device_id", "dev_id", default="") or "")
                key = ip or mac or did
                if key in seen:
                    continue
                seen.add(key)
                out.append({"mac": mac, "device_id": did, "ip": ip, "brand": "v380"})
            try:
                from v380.client.ezviz_rtsp import discover_ezviz_devices

                for device in discover_ezviz_devices():
                    ip = getattr(device, "ip", "") or ""
                    mac = getattr(device, "mac", "") or ""
                    did = str(getattr(device, "dev_id", "") or "")
                    key = ip or mac or did
                    row = {
                        "mac": mac,
                        "device_id": did,
                        "ip": ip,
                        "brand": "ezviz",
                        "rtsp_ready": bool(getattr(device, "rtsp_ready", False)),
                    }
                    if key in seen:
                        for existing in out:
                            if (existing.get("ip") or existing.get("mac") or existing.get("device_id")) == key:
                                existing["brand"] = "ezviz"
                                existing["rtsp_ready"] = row["rtsp_ready"] or bool(existing.get("rtsp_ready"))
                                if mac and not existing.get("mac"):
                                    existing["mac"] = mac
                                break
                        continue
                    seen.add(key)
                    out.append(row)
            except Exception:
                pass
            self.send_json({"ok": True, "devices": out})
            return
        if path == "/api/tables":
            from v380.tables.store import TableStore

            self.send_json({"ok": True, "tables": [t.to_dict() for t in TableStore().list()]})
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
            if len(parts) == 2 and parts[1] == "ezviz":
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing"}, 404)
                    return
                username = _camera_ezviz_username(cam)
                self.send_json({
                    "ok": True,
                    "signed_in": bool(username),
                    "username": username,
                    "account_id": int(cam_get(cam, "ezviz_account_id", default=0) or 0),
                })
                return
            if parts[1] == "ping":
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing"}, 404)
                    return
                self.send_json(ping_camera(cam))
                return
            if parts[1] == "panorama":
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing"}, 404)
                    return
                from v380.client.ezviz_panorama import load_cached_panorama

                serial = str(cam_get(cam, "ezviz_serial", default="") or getattr(cam, "device_id", "") or "")
                img = load_cached_panorama(serial) if serial else None
                if img is None:
                    from v380.client.ezviz_panorama import PANO_CACHE_DIR

                    if PANO_CACHE_DIR.is_dir():
                        files = sorted(
                            PANO_CACHE_DIR.glob("*.jpg"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        if files:
                            try:
                                from PIL import Image

                                img = Image.open(files[0]).convert("RGB")
                            except Exception:
                                img = None
                if img is None:
                    self.send_json({"ok": False, "error": "no panorama yet — tap Refresh 360"}, 404)
                    return
                import io

                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=88)
                data = buf.getvalue()
                self.send_response(200)
                self.cors()
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=5")
                self.end_headers()
                self.wfile.write(data)
                return
            if parts[1] == "snapshot":
                qs = parse_qs(urlparse(self.path).query or "")
                face_mode = (qs.get("face") or qs.get("attendance") or ["0"])[0].strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                # Prefer live session JPEG; avoid serving a stale disk preview.
                # Face/attendance dialogs: wait a bit longer for first frame + crop dual-pane.
                jpeg = HUB.live_jpeg(cam_id) if hasattr(HUB, "live_jpeg") else HUB.snapshot(cam_id)
                tries = 60 if face_mode else 40
                for _ in range(tries):
                    if jpeg:
                        break
                    time.sleep(0.1)
                    jpeg = (
                        HUB.live_jpeg(cam_id)
                        if hasattr(HUB, "live_jpeg")
                        else HUB.snapshot(cam_id)
                    )
                if not jpeg:
                    jpeg = HUB.snapshot(cam_id)
                if not jpeg:
                    self.send_json({"ok": False, "error": "no frame yet"}, 404)
                    return
                if face_mode:
                    try:
                        from v380.attendance.frame_util import normalize_attendance_frame

                        jpeg = normalize_attendance_frame(jpeg)
                    except Exception:
                        pass
                self.send_response(200)
                self.cors()
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
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
                        else:
                            ses._jpeg_event.wait(timeout=0.05)
                            ses._jpeg_event.clear()
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
            debug_req = bool(body.get("debug"))
            ok = check_user(user, password)
            if not ok and debug_req and debug_login_allowed() and user:
                # App debug mode: username must exist; password not required.
                row = STORE._conn.execute(
                    "SELECT id FROM users WHERE username = ?", (user,)
                ).fetchone()
                ok = row is not None
            if not ok:
                self.send_json({"ok": False, "error": "Wrong username or password"}, 401)
                return
            self.send_json({"ok": True, "token": new_token(user), "user": user, "ips": local_ips(), "port": PORT})
            return
        if not self.need_user():
            return
        if path.startswith("/api/attendance"):
            from v380.attendance.routes import try_handle

            if try_handle(self, "POST", path):
                return
        if path.startswith("/api/integrations/attendance"):
            from v380.integrations.attendance_public import try_handle_manage

            if try_handle_manage(self, "POST", path):
                return
        if path.startswith("/api/customers"):
            from v380.customers.routes import try_handle

            if try_handle(self, "POST", path):
                return
        if path.startswith("/api/occupancy"):
            from v380.occupancy.routes import try_handle

            if try_handle(self, "POST", path):
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
        if path == "/api/users":
            body = self.read_json()
            try:
                out = create_user(
                    str(body.get("username") or ""),
                    str(body.get("password") or ""),
                )
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json(out)
            return
        if path == "/api/cameras/probe":
            # Add/edit verify: may open RTSP once to confirm credentials.
            cam = camera_from_body(self.read_json())
            brand_fn = MODS.get("ezviz_brand")
            brand = brand_fn(cam_get(cam, "brand", default="v380")) if callable(brand_fn) else "v380"
            if brand == "ezviz":
                resolve = MODS.get("ezviz_resolve")
                probe = MODS.get("ezviz_probe")
                url = resolve(cam) if callable(resolve) else ""
                if not url:
                    self.send_json({"ok": False, "reachable": False, "online": False, "error": "Missing EZVIZ RTSP URL / IP"})
                    return
                try:
                    out = probe(url, timeout=4.0, grab=True) if callable(probe) else {"ok": False, "error": "EZVIZ probe missing"}
                except TypeError:
                    out = probe(url) if callable(probe) else {"ok": False, "error": "EZVIZ probe missing"}
                out["brand"] = "ezviz"
                self.send_json(out)
                return
            self.send_json(ping_camera(cam))
            return
        if path == "/api/ezviz/login":
            body = self.read_json()
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            sms = str(body.get("sms_code") or "").strip() or None
            try:
                from v380.client import ezviz_session as ez
                from v380.store.ezviz_accounts import EzvizAccountStore

                token = ez.login(username, password, sms)
                api_url = str(token.get("api_url") or "") if isinstance(token, dict) else ""
                token_json = json.dumps(token) if isinstance(token, dict) else ""
                EzvizAccountStore().upsert(username, password, token_json=token_json, api_url=api_url)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json({"ok": True, "signed_in": True, "username": username})
            return
        if path == "/api/ezviz/accounts":
            from v380.store.ezviz_accounts import EzvizAccountStore

            body = self.read_json()
            rec = EzvizAccountStore().upsert(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                token_json=str(body.get("token_json") or ""),
                api_url=str(body.get("api_url") or ""),
            )
            self.send_json({"ok": True, "account": rec})
            return
        if path == "/api/tables":
            from v380.tables.ptz_presets import save_table_panorama_point
            from v380.tables.store import TableStore

            body = self.read_json()
            store = TableStore()
            x = float(body.get("x") if body.get("x") is not None else 0.5)
            y = float(body.get("y") if body.get("y") is not None else 0.5)
            table = store.add(
                str(body.get("name") or "Table"),
                camera_id=int(body["camera_id"]) if body.get("camera_id") is not None else None,
                x=x,
                y=y,
            )
            msg = f"Added {table.name}"
            if body.get("x") is not None and body.get("y") is not None:
                saved, msg = save_table_panorama_point(store, table, x=x, y=y)
                if saved is not None:
                    table = saved
                cam = call(STORE, "get", "get", args=(int(table.camera_id or 0),)) if table.camera_id else None
                if cam is not None:
                    try:
                        from v380.client.ezviz_panorama import look_at
                        from v380.client.ezviz_session import using_account

                        account = _camera_ezviz_username(cam)
                        if account:
                            with using_account(account):
                                look_at(str(cam.ip or ""), x, y)
                        else:
                            look_at(str(cam.ip or ""), x, y)
                    except Exception:
                        pass
            self.send_json({"ok": True, "table": table.to_dict(), "message": msg})
            return
        if path.startswith("/api/tables/") and path.endswith("/goto"):
            from v380.tables.ptz_presets import goto_table_aim
            from v380.tables.store import TableStore

            table_id = path[len("/api/tables/") : -len("/goto")].strip("/")
            table = TableStore().get(table_id)
            if table is None:
                self.send_json({"ok": False, "error": "missing table"}, 404)
                return
            cam = call(STORE, "get", "get", args=(int(table.camera_id or 0),))
            if cam is None:
                self.send_json({"ok": False, "error": "table has no camera"}, 400)
                return
            account = _camera_ezviz_username(cam)
            try:
                from v380.client.ezviz_session import using_account

                if account:
                    with using_account(account):
                        ok, msg = goto_table_aim(
                            table,
                            ip=str(cam.ip or ""),
                            username=str(cam.username or ""),
                            password=str(cam.password or ""),
                        )
                else:
                    ok, msg = goto_table_aim(
                        table,
                        ip=str(cam.ip or ""),
                        username=str(cam.username or ""),
                        password=str(cam.password or ""),
                    )
            except Exception as exc:
                ok, msg = False, str(exc)
            self.send_json({"ok": bool(ok), "message": msg, "error": "" if ok else msg})
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
            if parts[1:] == ["ezviz", "login"]:
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing camera"}, 404)
                    return
                body = self.read_json()
                username = str(body.get("username") or "").strip()
                password = str(body.get("password") or "")
                sms = str(body.get("sms_code") or "").strip() or None
                try:
                    from v380.client import ezviz_session as ez
                    from v380.store.ezviz_accounts import EzvizAccountStore

                    token = ez.login(username, password, sms)
                    api_url = str(token.get("api_url") or "") if isinstance(token, dict) else ""
                    token_json = json.dumps(token) if isinstance(token, dict) else ""
                    rec = EzvizAccountStore().upsert(
                        username, password, token_json=token_json, api_url=api_url
                    )
                    with ez.using_account(username):
                        serial = ez.match_open_camera(
                            ip=str(getattr(cam, "ip", "") or ""),
                            serial=str(cam_get(cam, "ezviz_serial", default="") or ""),
                            mac=str(getattr(cam, "mac", "") or ""),
                            name=str(getattr(cam, "name", "") or ""),
                        )
                    if not serial:
                        raise RuntimeError(
                            "This EZVIZ account does not include this camera. "
                            "Sign in with the account that owns it."
                        )
                    call(
                        STORE,
                        "set_ezviz_link",
                        "set_ezviz_link",
                        args=(cam_id, int(rec["id"]), serial),
                    )
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 400)
                    return
                self.send_json({"ok": True, "signed_in": True, "username": username, "camera_id": cam_id})
                return
            if len(parts) == 2 and parts[1] == "ezviz_link":
                body = self.read_json()
                saved = call(
                    STORE,
                    "set_ezviz_link",
                    "set_ezviz_link",
                    args=(cam_id, int(body.get("account_id") or 0), str(body.get("serial") or "")),
                )
                self.send_json({"ok": True, "camera": cam_json(saved) if saved else None})
                return
            if len(parts) == 2 and parts[1] == "panorama":
                cam = call(STORE, "get", "get", args=(cam_id,))
                if cam is None:
                    self.send_json({"ok": False, "error": "missing camera"}, 404)
                    return
                from v380.client.ezviz_panorama import create_and_fetch_panorama
                from v380.client.ezviz_session import using_account

                account = _camera_ezviz_username(cam)
                try:
                    if account:
                        with using_account(account):
                            result = create_and_fetch_panorama(
                                str(getattr(cam, "ip", "") or ""),
                                password=str(getattr(cam, "password", "") or ""),
                                wait_sec=150.0,
                                force_new=True,
                            )
                    else:
                        result = create_and_fetch_panorama(
                            str(getattr(cam, "ip", "") or ""),
                            password=str(getattr(cam, "password", "") or ""),
                            wait_sec=150.0,
                            force_new=True,
                        )
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 400)
                    return
                if result.image is None:
                    self.send_json(
                        {"ok": False, "error": result.message or "Could not build 360° map"},
                        400,
                    )
                    return
                self.send_json({"ok": True, "message": result.message or "360° map ready"})
                return
            ses = HUB.session(cam_id)
            if ses is None:
                self.send_json({"ok": False, "error": "missing camera"}, 404)
                return
            if len(parts) == 2 and parts[1] == "control":
                ses.arm_control()
                self.send_json({"ok": True, "ready": True})
                return
            if len(parts) == 2 and parts[1] == "command":
                body = self.read_json()
                name = str(body.get("name") or "")
                hold = float(body.get("hold") or 0)
                ok = ses.command(name, hold=hold)
                self.send_json({
                    "ok": ok,
                    "name": name,
                    "hold": hold,
                    "message": getattr(ses, "_last_cmd_msg", "") or "",
                    "error": "" if ok else (getattr(ses, "_last_cmd_msg", "") or "Control failed"),
                })
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
        if path == "/api/system/autostart":
            body = self.read_json()
            enabled = bool(body.get("enabled"))
            try:
                from v380.windows_autostart import autostart_status, ensure_autostart

                ensure_autostart(enabled)
                self.send_json(autostart_status())
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if path.startswith("/api/attendance"):
            from v380.attendance.routes import try_handle

            if try_handle(self, "PUT", path):
                return
        if path.startswith("/api/customers"):
            from v380.customers.routes import try_handle

            if try_handle(self, "PUT", path):
                return
        if path.startswith("/api/occupancy"):
            from v380.occupancy.routes import try_handle

            if try_handle(self, "PUT", path):
                return
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
        if path.startswith("/api/attendance"):
            from v380.attendance.routes import try_handle

            if try_handle(self, "DELETE", path):
                return
        if path.startswith("/api/integrations/attendance"):
            from v380.integrations.attendance_public import try_handle_manage

            if try_handle_manage(self, "DELETE", path):
                return
        if path.startswith("/api/customers"):
            from v380.customers.routes import try_handle

            if try_handle(self, "DELETE", path):
                return
        if path.startswith("/api/occupancy"):
            from v380.occupancy.routes import try_handle

            if try_handle(self, "DELETE", path):
                return
        if path.startswith("/api/users/"):
            username = path[len("/api/users/") :].strip("/")
            me = auth_user(self.token()) or ""
            try:
                out = delete_user(username, actor=me)
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json(out)
            return
        if path.startswith("/api/tables/"):
            from v380.tables.store import TableStore

            table_id = path[len("/api/tables/") :].strip("/")
            ok = TableStore().delete(table_id)
            self.send_json({"ok": ok, "error": "" if ok else "missing table"}, 200 if ok else 404)
            return
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
    # Do not register Windows logon tasks here — that hid the API in the
    # background. Use Attendance → “Start backend with Windows” (or Share API
    # setup) only when the user explicitly wants logon autostart.
    # Warm live sessions so Home/View do not wait on first RTSP open.
    try:
        threading.Thread(target=HUB.warm_all, daemon=True, name="warm-hub").start()
    except Exception as exc:
        print("[studio] warm hub:", exc)
    # Attendance watcher (optional feature) — uses hub snapshots only; no live/tables changes.
    try:
        from v380.attendance.face_engine import FaceEngine, _ensure_stdio
        from v380.attendance.watcher import start_attendance_watcher

        _ensure_stdio()  # pythonw has no stdout; InsightFace download/progress needs a stream
        # Live frames only — stale HD cache misses people walking through fast.
        start_attendance_watcher(lambda cam_id: HUB.attendance_jpeg(int(cam_id)))

        def _warm_face() -> None:
            ok, msg = FaceEngine.warmup()
            print(f"[studio] attendance face engine: {'ok' if ok else msg}")

        threading.Thread(target=_warm_face, daemon=True, name="attendance-face-warm").start()
    except Exception as exc:
        print("[studio] attendance:", exc)
    # Customer visit tracking — separate feature; same hub face frames.
    try:
        from v380.customers.watcher import start_customer_watcher

        def _customer_cams() -> list[int]:
            cams = call(STORE, "list", "list", default=[]) or []
            out: list[int] = []
            for c in cams:
                try:
                    out.append(int(getattr(c, "id", 0) or 0))
                except Exception:
                    continue
            return [i for i in out if i > 0]

        start_customer_watcher(
            lambda cam_id: HUB.face_jpeg(int(cam_id)),
            _customer_cams,
        )
    except Exception as exc:
        print("[studio] customers:", exc)
    # Floor occupancy — table zones on a fixed overview camera (isolated).
    try:
        from v380.occupancy.watcher import start_occupancy_watcher

        start_occupancy_watcher(lambda cam_id: HUB.face_jpeg(int(cam_id)))
    except Exception as exc:
        print("[studio] occupancy:", exc)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"{APP_NAME} server — Flutter client connects here")
    print(f"Port: {PORT}")
    for ip in local_ips():
        print(f"On Windows: Remote server -> {ip}  then login")
    print("Ctrl+C to stop")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
