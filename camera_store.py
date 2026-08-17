"""Camera CRUD. Passwords are encrypted at rest."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from db import connect
from secretbox import decrypt, encrypt, load_or_create_key

DATA_DIR = Path(__file__).with_name("data")
DB_PATH = DATA_DIR / "v380.db"
KEY_PATH = DATA_DIR / "master.key"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Camera:
    id: int
    name: str
    device_id: str
    mac: str
    ip: str
    port: int
    username: str
    password: str
    source: str
    quality: int
    auto_record: bool
    created_at: str
    updated_at: str

    @property
    def quality_name(self) -> str:
        return "HD" if self.quality else "SD"

    @property
    def source_name(self) -> str:
        return "Cloud" if self.source == "cloud" else "LAN"


class CameraStore:
    def __init__(self, data_dir: Path | None = None):
        root = data_dir or DATA_DIR
        self._key = load_or_create_key(root / "master.key")
        self._conn = connect(root / "v380.db")
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def list(self) -> list[Camera]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cameras ORDER BY name, id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, camera_id: int) -> Camera | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_device_id(self, device_id: str) -> Camera | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cameras WHERE device_id = ?", (device_id,)).fetchone()
        return self._row(row) if row else None

    def upsert(self, cam: Camera) -> Camera:
        now = _now()
        blob = encrypt(self._key, cam.password)
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM cameras WHERE device_id = ?", (cam.device_id,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    """
                    UPDATE cameras SET
                        name=?, mac=?, ip=?, port=?, username=?, password_enc=?,
                        source=?, quality=?, auto_record=?, updated_at=?
                    WHERE device_id=?
                    """,
                    (
                        cam.name, cam.mac, cam.ip, cam.port, cam.username, blob,
                        cam.source, cam.quality, 1 if cam.auto_record else 0, now, cam.device_id,
                    ),
                )
                camera_id = int(existing["id"])
            else:
                cur = self._conn.execute(
                    """
                    INSERT INTO cameras
                        (name, device_id, mac, ip, port, username, password_enc, source, quality, auto_record, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cam.name, cam.device_id, cam.mac, cam.ip, cam.port, cam.username, blob,
                        cam.source, cam.quality, 1 if cam.auto_record else 0, now, now,
                    ),
                )
                camera_id = int(cur.lastrowid)
            self._conn.commit()
        saved = self.get(camera_id)
        if saved is None:
            raise RuntimeError("Failed to save camera")
        return saved

    def update(self, cam: Camera) -> Camera:
        now = _now()
        blob = encrypt(self._key, cam.password)
        with self._lock:
            self._conn.execute(
                """
                UPDATE cameras SET
                    name=?, device_id=?, mac=?, ip=?, port=?, username=?, password_enc=?,
                    source=?, quality=?, auto_record=?, updated_at=?
                WHERE id=?
                """,
                (
                    cam.name, cam.device_id, cam.mac, cam.ip, cam.port, cam.username, blob,
                    cam.source, cam.quality, 1 if cam.auto_record else 0, now, cam.id,
                ),
            )
            self._conn.commit()
        saved = self.get(cam.id)
        if saved is None:
            raise RuntimeError("Camera not found")
        return saved

    def set_auto_record(self, camera_id: int, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cameras SET auto_record = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _now(), camera_id),
            )
            self._conn.commit()

    def delete(self, camera_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            self._conn.commit()

    def set_status(
        self,
        camera_id: int,
        *,
        codec: str | None = None,
        error: str | None = None,
        width: int | None = None,
        height: int | None = None,
        seen: bool = False,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO camera_status (camera_id) VALUES (?)",
                (camera_id,),
            )
            fields: list[str] = []
            args: list[object] = []
            if codec is not None:
                fields.append("last_codec = ?")
                args.append(codec)
            if error is not None:
                fields.append("last_error = ?")
                args.append(error)
            if width is not None:
                fields.append("last_width = ?")
                args.append(width)
            if height is not None:
                fields.append("last_height = ?")
                args.append(height)
            if seen:
                fields.append("last_seen = ?")
                args.append(_now())
            if not fields:
                return
            args.append(camera_id)
            self._conn.execute(
                f"UPDATE camera_status SET {', '.join(fields)} WHERE camera_id = ?",
                args,
            )
            self._conn.commit()

    def _row(self, row: sqlite3.Row) -> Camera:
        return Camera(
            id=int(row["id"]),
            name=row["name"],
            device_id=str(row["device_id"]),
            mac=row["mac"] or "",
            ip=row["ip"] or "",
            port=int(row["port"]),
            username=row["username"],
            password=decrypt(self._key, row["password_enc"]),
            source=row["source"],
            quality=int(row["quality"]),
            auto_record=bool(row["auto_record"]) if "auto_record" in row.keys() else False,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
