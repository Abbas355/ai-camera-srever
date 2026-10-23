"""SQLite persistence for restaurant customers and visits."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from v380.paths import DATA_DIR

DB_PATH = DATA_DIR / "customers.db"
PHOTOS_DIR = DATA_DIR / "customers" / "photos"

SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    embedding BLOB NOT NULL,
    photo_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    last_seen_at TEXT NOT NULL,
    table_id TEXT,
    table_name TEXT,
    camera_id INTEGER,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS customer_sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    visit_id INTEGER,
    camera_id INTEGER,
    table_id TEXT,
    table_name TEXT,
    score REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE INDEX IF NOT EXISTS idx_customer_visits_customer
    ON customer_visits(customer_id, started_at);
CREATE INDEX IF NOT EXISTS idx_customer_sightings_customer
    ON customer_sightings(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_customer_sightings_visit
    ON customer_sightings(visit_id);

CREATE TABLE IF NOT EXISTS customer_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    area REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_faces_customer
    ON customer_faces(customer_id);
"""

DEFAULTS: dict[str, str] = {
    "enabled": "0",
    # Confident same-person match (InsightFace cosine on normed vectors).
    "match_threshold": "0.42",
    # Soft re-id across angles (front vs side often ~0.25–0.35).
    "soft_match_threshold": "0.26",
    # Staff skip: only strong matches (too low = real customers skipped).
    "staff_exclude_threshold": "0.30",
    "away_sec": "2700",
    "poll_interval_sec": "2.5",
    "auto_enroll": "1",
    # Absolute face size floor (pixels²). Table-distance CCTV faces are often ~800–1500.
    "min_face_area": "350",
    # Relative size floor; combined with min_face_area via min() so HD + substream both work.
    "min_face_frac": "0.0005",
    # Drop very weak detections (InsightFace det_score).
    "min_det_score": "0.35",
    # Same unknown face must appear this many times before auto-enroll.
    "enroll_confirm_hits": "3",
    # Cap faces processed per frame (largest first).
    "max_faces_per_frame": "8",
    # Auto-enroll quality — tuned for seated / looking-down guests.
    "enroll_min_area": "900",
    "enroll_min_det": "0.40",
    "enroll_max_yaw": "45",
    # Seated guests often look down at plates (InsightFace pitch −40…−80).
    "enroll_max_pitch": "70",
    "camera_ids": "",
}

# One-shot — v9: stop duplicate Guests for the same person (multi-angle).
_QUALITY_V9 = {
    "match_threshold": "0.42",
    "staff_exclude_threshold": "0.30",
    "soft_match_threshold": "0.26",
    "min_face_area": "350",
    "min_face_frac": "0.0005",
    "min_det_score": "0.35",
    "enroll_confirm_hits": "3",
    "max_faces_per_frame": "8",
    "enroll_min_area": "900",
    "enroll_min_det": "0.40",
    "enroll_max_yaw": "45",
    "enroll_max_pitch": "70",
    "poll_interval_sec": "2.5",
}

# Clear enough to freeze the portrait (pixels² / InsightFace det_score).
PHOTO_CLEAR_AREA = 4500.0
PHOTO_CLEAR_DET = 0.55


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_ts(value: str | None) -> datetime | None:
    raw = (value or "").strip().replace("Z", "").replace(" ", "T")
    if not raw:
        return None
    for n, fmt in ((19, "%Y-%m-%dT%H:%M:%S"), (16, "%Y-%m-%dT%H:%M")):
        try:
            return datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    return None


def _fmt_duration(seconds: int | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m"
    return f"{sec}s"


def _clock(value: str | None) -> str | None:
    if not value:
        return None
    ts = _parse_ts(value)
    return ts.strftime("%H:%M:%S") if ts else None


class CustomerStore:
    def __init__(self, db_path: Path | None = None, photos_dir: Path | None = None):
        self.db_path = Path(db_path or DB_PATH)
        self.photos_dir = Path(photos_dir or PHOTOS_DIR)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Photo quality lock — keep the clearest face, stop swapping once good.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(customers)").fetchall()}
            if "photo_area" not in cols:
                conn.execute(
                    "ALTER TABLE customers ADD COLUMN photo_area REAL NOT NULL DEFAULT 0"
                )
            if "photo_locked" not in cols:
                conn.execute(
                    "ALTER TABLE customers ADD COLUMN photo_locked INTEGER NOT NULL DEFAULT 0"
                )
            # Face box for visit playback tracking overlay.
            scols = {
                r[1] for r in conn.execute("PRAGMA table_info(customer_sightings)").fetchall()
            }
            for col, decl in (
                ("bbox_x1", "REAL"),
                ("bbox_y1", "REAL"),
                ("bbox_x2", "REAL"),
                ("bbox_y2", "REAL"),
                ("frame_w", "REAL"),
                ("frame_h", "REAL"),
            ):
                if col not in scols:
                    conn.execute(
                        f"ALTER TABLE customer_sightings ADD COLUMN {col} {decl}"
                    )
            for k, v in DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO customer_settings(key, value) VALUES(?, ?)",
                    (k, v),
                )
            # v9: softer re-id + multi-angle faces to stop duplicate Guests.
            done = conn.execute(
                "SELECT 1 FROM customer_settings WHERE key='quality_v9'"
            ).fetchone()
            if not done:
                for k, v in _QUALITY_V9.items():
                    conn.execute(
                        "INSERT INTO customer_settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, v),
                    )
                conn.execute(
                    "INSERT INTO customer_settings(key, value) VALUES('quality_v9', '1')"
                )
            # Backfill primary embedding into faces table.
            missing = conn.execute(
                "SELECT c.id, c.embedding FROM customers c "
                "WHERE c.active=1 AND NOT EXISTS ("
                "  SELECT 1 FROM customer_faces f WHERE f.customer_id=c.id"
                ")"
            ).fetchall()
            now = _now()
            for r in missing:
                conn.execute(
                    "INSERT INTO customer_faces(customer_id, embedding, area, created_at) "
                    "VALUES(?, ?, 0, ?)",
                    (int(r["id"]), bytes(r["embedding"]), now),
                )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    # --- settings ---

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT key, value FROM customer_settings").fetchall()
        raw = {r["key"]: r["value"] for r in rows}
        for k, v in DEFAULTS.items():
            raw.setdefault(k, v)
        cam_ids: list[int] = []
        blob = (raw.get("camera_ids") or "").strip()
        if blob:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, list):
                    cam_ids = [int(x) for x in parsed]
            except Exception:
                cam_ids = []
        def _f(key: str) -> float:
            try:
                return float(raw.get(key) or DEFAULTS[key])
            except ValueError:
                return float(DEFAULTS[key])

        try:
            away = int(float(raw.get("away_sec") or DEFAULTS["away_sec"]))
        except ValueError:
            away = int(DEFAULTS["away_sec"])
        try:
            min_area = float(raw.get("min_face_area") or DEFAULTS["min_face_area"])
        except ValueError:
            min_area = float(DEFAULTS["min_face_area"])
        try:
            confirm = int(float(raw.get("enroll_confirm_hits") or DEFAULTS["enroll_confirm_hits"]))
        except ValueError:
            confirm = int(DEFAULTS["enroll_confirm_hits"])
        try:
            max_faces = int(float(raw.get("max_faces_per_frame") or DEFAULTS["max_faces_per_frame"]))
        except ValueError:
            max_faces = int(DEFAULTS["max_faces_per_frame"])
        return {
            "enabled": (raw.get("enabled") or "0").strip() in {"1", "true", "yes", "on"},
            "match_threshold": _f("match_threshold"),
            "soft_match_threshold": _f("soft_match_threshold"),
            "staff_exclude_threshold": _f("staff_exclude_threshold"),
            "away_sec": max(120, away),
            "poll_interval_sec": max(1.0, _f("poll_interval_sec")),
            "auto_enroll": (raw.get("auto_enroll") or "1").strip() in {"1", "true", "yes", "on"},
            "min_face_area": max(200.0, min_area),
            "min_face_frac": max(0.0, min(0.2, _f("min_face_frac"))),
            "min_det_score": max(0.0, min(0.95, _f("min_det_score"))),
            "enroll_confirm_hits": max(1, min(10, confirm)),
            "max_faces_per_frame": max(1, min(12, max_faces)),
            "enroll_min_area": max(500.0, _f("enroll_min_area")),
            "enroll_min_det": max(0.3, min(0.95, _f("enroll_min_det"))),
            "enroll_max_yaw": max(5.0, min(60.0, _f("enroll_max_yaw"))),
            "enroll_max_pitch": max(5.0, min(70.0, _f("enroll_max_pitch"))),
            "camera_ids": cam_ids,
        }

    def update_settings(self, **kwargs: Any) -> dict[str, Any]:
        mapping: dict[str, str] = {}
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            mapping["enabled"] = "1" if bool(kwargs["enabled"]) else "0"
        if "match_threshold" in kwargs and kwargs["match_threshold"] is not None:
            mapping["match_threshold"] = str(float(kwargs["match_threshold"]))
        if "soft_match_threshold" in kwargs and kwargs["soft_match_threshold"] is not None:
            mapping["soft_match_threshold"] = str(float(kwargs["soft_match_threshold"]))
        if "staff_exclude_threshold" in kwargs and kwargs["staff_exclude_threshold"] is not None:
            mapping["staff_exclude_threshold"] = str(float(kwargs["staff_exclude_threshold"]))
        if "away_sec" in kwargs and kwargs["away_sec"] is not None:
            mapping["away_sec"] = str(int(kwargs["away_sec"]))
        if "poll_interval_sec" in kwargs and kwargs["poll_interval_sec"] is not None:
            mapping["poll_interval_sec"] = str(float(kwargs["poll_interval_sec"]))
        if "auto_enroll" in kwargs and kwargs["auto_enroll"] is not None:
            mapping["auto_enroll"] = "1" if bool(kwargs["auto_enroll"]) else "0"
        if "min_face_area" in kwargs and kwargs["min_face_area"] is not None:
            mapping["min_face_area"] = str(float(kwargs["min_face_area"]))
        if "min_face_frac" in kwargs and kwargs["min_face_frac"] is not None:
            mapping["min_face_frac"] = str(float(kwargs["min_face_frac"]))
        if "min_det_score" in kwargs and kwargs["min_det_score"] is not None:
            mapping["min_det_score"] = str(float(kwargs["min_det_score"]))
        if "enroll_confirm_hits" in kwargs and kwargs["enroll_confirm_hits"] is not None:
            mapping["enroll_confirm_hits"] = str(int(kwargs["enroll_confirm_hits"]))
        if "max_faces_per_frame" in kwargs and kwargs["max_faces_per_frame"] is not None:
            mapping["max_faces_per_frame"] = str(int(kwargs["max_faces_per_frame"]))
        if "enroll_min_area" in kwargs and kwargs["enroll_min_area"] is not None:
            mapping["enroll_min_area"] = str(float(kwargs["enroll_min_area"]))
        if "enroll_min_det" in kwargs and kwargs["enroll_min_det"] is not None:
            mapping["enroll_min_det"] = str(float(kwargs["enroll_min_det"]))
        if "enroll_max_yaw" in kwargs and kwargs["enroll_max_yaw"] is not None:
            mapping["enroll_max_yaw"] = str(float(kwargs["enroll_max_yaw"]))
        if "enroll_max_pitch" in kwargs and kwargs["enroll_max_pitch"] is not None:
            mapping["enroll_max_pitch"] = str(float(kwargs["enroll_max_pitch"]))
        if "camera_ids" in kwargs:
            ids = kwargs["camera_ids"]
            if ids is None:
                mapping["camera_ids"] = ""
            elif isinstance(ids, list):
                mapping["camera_ids"] = json.dumps([int(x) for x in ids])
            else:
                mapping["camera_ids"] = str(ids)
        if mapping:
            with self._lock:
                with self._connect() as conn:
                    for k, v in mapping.items():
                        conn.execute(
                            "INSERT INTO customer_settings(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (k, v),
                        )
                    conn.commit()
        return self.get_settings()

    # --- customers ---

    def list_customers(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        q = (
            "SELECT id, name, notes, active, photo_path, photo_area, photo_locked, "
            "created_at, updated_at FROM customers"
        )
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY updated_at DESC, id DESC"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(q).fetchall()
        return [self._customer_row(r, enrich=True) for r in rows]

    def get_customer(self, customer_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                r = conn.execute(
                    "SELECT id, name, notes, active, photo_path, photo_area, photo_locked, "
                    "created_at, updated_at FROM customers WHERE id=?",
                    (int(customer_id),),
                ).fetchone()
        return self._customer_row(r, enrich=True) if r else None

    def list_embeddings(self) -> list[tuple[int, bytes]]:
        """All active guest embeddings (primary + extra angles)."""
        with self._lock:
            with self._connect() as conn:
                faces = conn.execute(
                    "SELECT f.customer_id, f.embedding FROM customer_faces f "
                    "JOIN customers c ON c.id=f.customer_id WHERE c.active=1"
                ).fetchall()
                if faces:
                    return [
                        (int(r["customer_id"]), bytes(r["embedding"])) for r in faces
                    ]
                rows = conn.execute(
                    "SELECT id, embedding FROM customers WHERE active=1"
                ).fetchall()
        return [(int(r["id"]), bytes(r["embedding"])) for r in rows]

    def add_customer_face(
        self,
        customer_id: int,
        embedding: bytes,
        *,
        area: float = 0.0,
        max_faces: int = 4,
    ) -> bool:
        """Store an extra angle so side/front views rematch the same guest."""
        if self.get_customer(int(customer_id)) is None:
            return False
        now = _now()
        with self._lock:
            with self._connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM customer_faces WHERE customer_id=?",
                    (int(customer_id),),
                ).fetchone()["n"]
                if int(n) >= int(max_faces):
                    oldest = conn.execute(
                        "SELECT id FROM customer_faces WHERE customer_id=? "
                        "ORDER BY id ASC LIMIT 1",
                        (int(customer_id),),
                    ).fetchone()
                    if oldest:
                        conn.execute(
                            "DELETE FROM customer_faces WHERE id=?",
                            (int(oldest["id"]),),
                        )
                conn.execute(
                    "INSERT INTO customer_faces(customer_id, embedding, area, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (int(customer_id), embedding, float(area or 0), now),
                )
                conn.commit()
        return True

    def get_embedding(self, customer_id: int) -> bytes | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT embedding FROM customers WHERE id=?",
                    (int(customer_id),),
                ).fetchone()
        return bytes(row["embedding"]) if row else None

    def add_customer(
        self,
        *,
        name: str,
        embedding: bytes,
        photo_bytes: bytes | None = None,
        notes: str = "",
        photo_area: float = 0.0,
        photo_det: float = 0.0,
    ) -> dict[str, Any]:
        now = _now()
        clean = (name or "").strip() or f"Guest {_today()}"
        area = max(0.0, float(photo_area or 0.0))
        det = max(0.0, float(photo_det or 0.0))
        locked = 1 if (
            photo_bytes
            and area >= PHOTO_CLEAR_AREA
            and (det <= 0.0 or det >= PHOTO_CLEAR_DET)
        ) else 0
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO customers("
                    "name, notes, active, embedding, photo_path, photo_area, photo_locked, "
                    "created_at, updated_at"
                    ") VALUES(?, ?, 1, ?, NULL, ?, ?, ?, ?)",
                    (clean, (notes or "").strip(), embedding, area, locked, now, now),
                )
                cid = int(cur.lastrowid)
                if photo_bytes:
                    path = self.photos_dir / f"{cid}.jpg"
                    path.write_bytes(photo_bytes)
                    conn.execute(
                        "UPDATE customers SET photo_path=? WHERE id=?",
                        (str(path), cid),
                    )
                conn.execute(
                    "INSERT INTO customer_faces(customer_id, embedding, area, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (cid, embedding, area, now),
                )
                conn.commit()
        out = self.get_customer(cid)
        assert out is not None
        return out

    def update_customer(
        self,
        customer_id: int,
        *,
        name: str | None = None,
        notes: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM customers WHERE id=?", (int(customer_id),)
                ).fetchone()
                if row is None:
                    return None
                if name is not None:
                    conn.execute(
                        "UPDATE customers SET name=?, updated_at=? WHERE id=?",
                        ((name or "").strip() or "Guest", _now(), int(customer_id)),
                    )
                if notes is not None:
                    conn.execute(
                        "UPDATE customers SET notes=?, updated_at=? WHERE id=?",
                        ((notes or "").strip(), _now(), int(customer_id)),
                    )
                if active is not None:
                    conn.execute(
                        "UPDATE customers SET active=?, updated_at=? WHERE id=?",
                        (1 if active else 0, _now(), int(customer_id)),
                    )
                conn.commit()
        return self.get_customer(customer_id)

    def update_embedding(
        self,
        customer_id: int,
        embedding: bytes,
        *,
        photo_bytes: bytes | None = None,
        photo_area: float = 0.0,
        photo_det: float = 0.0,
    ) -> None:
        """Refresh gallery embedding; photo only upgrades until locked clear."""
        now = _now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, photo_path, photo_area, photo_locked FROM customers WHERE id=?",
                    (int(customer_id),),
                ).fetchone()
                if row is None:
                    return
                conn.execute(
                    "UPDATE customers SET embedding=?, updated_at=? WHERE id=?",
                    (embedding, now, int(customer_id)),
                )
                conn.commit()
        if photo_bytes:
            self.maybe_upgrade_photo(
                int(customer_id),
                photo_bytes,
                area=photo_area,
                det_score=photo_det,
            )

    def maybe_upgrade_photo(
        self,
        customer_id: int,
        photo_bytes: bytes,
        *,
        area: float = 0.0,
        det_score: float = 0.0,
    ) -> bool:
        """Replace portrait only if clearer — once locked, never change again."""
        if not photo_bytes:
            return False
        area = max(0.0, float(area or 0.0))
        det = max(0.0, float(det_score or 0.0))
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT photo_path, photo_area, photo_locked FROM customers WHERE id=?",
                    (int(customer_id),),
                ).fetchone()
                if row is None:
                    return False
                if int(row["photo_locked"] or 0) == 1:
                    return False
                prev_area = float(row["photo_area"] or 0.0)
                has_photo = bool(row["photo_path"])
                # First photo, or clearly sharper/larger face.
                better = (not has_photo) or (
                    area >= prev_area * 1.25 and area >= max(prev_area + 400.0, 900.0)
                )
                if not better:
                    return False
                path = self.photos_dir / f"{int(customer_id)}.jpg"
                path.write_bytes(photo_bytes)
                locked = 1 if (
                    area >= PHOTO_CLEAR_AREA and (det <= 0.0 or det >= PHOTO_CLEAR_DET)
                ) else 0
                conn.execute(
                    "UPDATE customers SET photo_path=?, photo_area=?, photo_locked=?, "
                    "updated_at=? WHERE id=?",
                    (str(path), area, locked, _now(), int(customer_id)),
                )
                conn.commit()
        return True

    def delete_customer(self, customer_id: int) -> bool:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT photo_path FROM customers WHERE id=?",
                    (int(customer_id),),
                ).fetchone()
                if row is None:
                    return False
                conn.execute(
                    "DELETE FROM customer_faces WHERE customer_id=?", (int(customer_id),)
                )
                conn.execute("DELETE FROM customer_sightings WHERE customer_id=?", (int(customer_id),))
                conn.execute("DELETE FROM customer_visits WHERE customer_id=?", (int(customer_id),))
                conn.execute("DELETE FROM customers WHERE id=?", (int(customer_id),))
                conn.commit()
        if row["photo_path"]:
            try:
                Path(str(row["photo_path"])).unlink(missing_ok=True)
            except Exception:
                pass
        return True

    def merge_into(self, keep_id: int, drop_id: int) -> bool:
        """Move visits/sightings/faces from drop → keep, then deactivate drop."""
        keep_id, drop_id = int(keep_id), int(drop_id)
        if keep_id == drop_id:
            return False
        if self.get_customer(keep_id) is None or self.get_customer(drop_id) is None:
            return False
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE customer_visits SET customer_id=? WHERE customer_id=?",
                    (keep_id, drop_id),
                )
                conn.execute(
                    "UPDATE customer_sightings SET customer_id=? WHERE customer_id=?",
                    (keep_id, drop_id),
                )
                # Move unique-ish face angles onto the kept guest.
                faces = conn.execute(
                    "SELECT embedding, area FROM customer_faces WHERE customer_id=?",
                    (drop_id,),
                ).fetchall()
                for fr in faces:
                    conn.execute(
                        "INSERT INTO customer_faces(customer_id, embedding, area, created_at) "
                        "VALUES(?, ?, ?, ?)",
                        (keep_id, bytes(fr["embedding"]), float(fr["area"] or 0), _now()),
                    )
                conn.execute("DELETE FROM customer_faces WHERE customer_id=?", (drop_id,))
                conn.execute(
                    "UPDATE customers SET active=0, updated_at=? WHERE id=?",
                    (_now(), drop_id),
                )
                conn.commit()
        return True

    def photo_path_for(self, customer_id: int) -> Path | None:
        c = self.get_customer(customer_id)
        if not c or not c.get("photo_path"):
            return None
        p = Path(str(c["photo_path"]))
        return p if p.is_file() else None

    def next_guest_name(self) -> str:
        with self._lock:
            with self._connect() as conn:
                n = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
        return f"Guest {int(n) + 1}"

    def _customer_row(self, r: sqlite3.Row | None, *, enrich: bool = False) -> dict[str, Any] | None:
        if r is None:
            return None
        keys = r.keys()
        out: dict[str, Any] = {
            "id": int(r["id"]),
            "name": r["name"],
            "notes": r["notes"] or "",
            "active": bool(r["active"]),
            "photo_path": r["photo_path"],
            "has_photo": bool(r["photo_path"]),
            "photo_locked": bool(r["photo_locked"]) if "photo_locked" in keys else False,
            "photo_area": float(r["photo_area"] or 0) if "photo_area" in keys else 0.0,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        if enrich:
            stats = self.customer_stats(int(r["id"]))
            out.update(stats)
        return out

    def customer_stats(self, customer_id: int) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                days = conn.execute(
                    "SELECT COUNT(DISTINCT substr(started_at, 1, 10)) AS n "
                    "FROM customer_visits WHERE customer_id=?",
                    (int(customer_id),),
                ).fetchone()["n"]
                visits = conn.execute(
                    "SELECT COUNT(*) AS n FROM customer_visits WHERE customer_id=?",
                    (int(customer_id),),
                ).fetchone()["n"]
                last = conn.execute(
                    "SELECT started_at, ended_at, table_name, last_seen_at "
                    "FROM customer_visits WHERE customer_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (int(customer_id),),
                ).fetchone()
        last_visit = None
        if last:
            last_visit = {
                "started_at": last["started_at"],
                "ended_at": last["ended_at"],
                "table_name": last["table_name"],
                "last_seen_at": last["last_seen_at"],
                "open": last["ended_at"] is None,
            }
        return {
            "visit_days": int(days),
            "visit_count": int(visits),
            "last_visit": last_visit,
        }

    # --- visits / sightings ---

    def open_visit(self, customer_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM customer_visits WHERE customer_id=? AND ended_at IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (int(customer_id),),
                ).fetchone()
        return self._visit_row(row) if row else None

    def start_visit(
        self,
        customer_id: int,
        *,
        camera_id: int | None,
        table_id: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO customer_visits("
                    "customer_id, started_at, ended_at, last_seen_at, table_id, table_name, camera_id"
                    ") VALUES(?, ?, NULL, ?, ?, ?, ?)",
                    (int(customer_id), now, now, table_id, table_name, camera_id),
                )
                vid = int(cur.lastrowid)
                conn.execute(
                    "UPDATE customers SET updated_at=? WHERE id=?",
                    (now, int(customer_id)),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM customer_visits WHERE id=?", (vid,)).fetchone()
        return self._visit_row(row)

    def touch_visit(
        self,
        visit_id: int,
        *,
        camera_id: int | None = None,
        table_id: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM customer_visits WHERE id=?", (int(visit_id),)
                ).fetchone()
                if row is None or row["ended_at"] is not None:
                    return self._visit_row(row) if row else None
                sets = ["last_seen_at=?"]
                args: list[Any] = [now]
                if camera_id is not None:
                    sets.append("camera_id=?")
                    args.append(int(camera_id))
                if table_id and not row["table_id"]:
                    sets.append("table_id=?")
                    sets.append("table_name=?")
                    args.extend([table_id, table_name or table_id])
                args.append(int(visit_id))
                conn.execute(
                    f"UPDATE customer_visits SET {', '.join(sets)} WHERE id=?",
                    args,
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM customer_visits WHERE id=?", (int(visit_id),)
                ).fetchone()
        return self._visit_row(row)

    def set_visit_table(self, visit_id: int, table_id: str | None, table_name: str | None) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE customer_visits SET table_id=?, table_name=? WHERE id=?",
                    (table_id, table_name, int(visit_id)),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM customer_visits WHERE id=?", (int(visit_id),)
                ).fetchone()
        return self._visit_row(row) if row else None

    def close_visit(self, visit_id: int, *, ended_at: str | None = None) -> dict[str, Any] | None:
        end = ended_at or _now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE customer_visits SET ended_at=? WHERE id=? AND ended_at IS NULL",
                    (end, int(visit_id)),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM customer_visits WHERE id=?", (int(visit_id),)
                ).fetchone()
        return self._visit_row(row) if row else None

    def close_stale_visits(self, away_sec: int) -> int:
        now = datetime.now()
        closed = 0
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, last_seen_at FROM customer_visits WHERE ended_at IS NULL"
                ).fetchall()
                for r in rows:
                    last = _parse_ts(r["last_seen_at"])
                    if last is None:
                        continue
                    age = (now - last).total_seconds()
                    if age >= float(away_sec):
                        conn.execute(
                            "UPDATE customer_visits SET ended_at=? WHERE id=?",
                            (r["last_seen_at"], int(r["id"])),
                        )
                        closed += 1
                conn.commit()
        return closed

    def add_sighting(
        self,
        *,
        customer_id: int,
        visit_id: int | None,
        camera_id: int | None,
        score: float | None,
        table_id: str | None = None,
        table_name: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        frame_size: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        bx1 = by1 = bx2 = by2 = fw = fh = None
        if bbox is not None and len(bbox) >= 4:
            bx1, by1, bx2, by2 = (
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
            )
        if frame_size is not None and len(frame_size) >= 2:
            fw, fh = float(frame_size[0]), float(frame_size[1])
        with self._lock:
            with self._connect() as conn:
                # Throttle identical camera sightings within 8s — but refresh bbox
                # so the tracking overlay stays current while they sit still.
                if visit_id is not None and camera_id is not None:
                    prev = conn.execute(
                        "SELECT id, created_at FROM customer_sightings "
                        "WHERE visit_id=? AND camera_id=? ORDER BY id DESC LIMIT 1",
                        (int(visit_id), int(camera_id)),
                    ).fetchone()
                    if prev:
                        pts = _parse_ts(prev["created_at"])
                        if pts and (datetime.now() - pts).total_seconds() < 8:
                            if bx1 is not None and fw and fh:
                                conn.execute(
                                    "UPDATE customer_sightings SET score=?, "
                                    "bbox_x1=?, bbox_y1=?, bbox_x2=?, bbox_y2=?, "
                                    "frame_w=?, frame_h=?, created_at=? WHERE id=?",
                                    (
                                        score,
                                        bx1,
                                        by1,
                                        bx2,
                                        by2,
                                        fw,
                                        fh,
                                        now,
                                        int(prev["id"]),
                                    ),
                                )
                                conn.commit()
                            row = conn.execute(
                                "SELECT * FROM customer_sightings WHERE id=?",
                                (int(prev["id"]),),
                            ).fetchone()
                            return self._sighting_row(row)
                cur = conn.execute(
                    "INSERT INTO customer_sightings("
                    "customer_id, visit_id, camera_id, table_id, table_name, score, "
                    "created_at, bbox_x1, bbox_y1, bbox_x2, bbox_y2, frame_w, frame_h"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(customer_id),
                        visit_id,
                        camera_id,
                        table_id,
                        table_name,
                        score,
                        now,
                        bx1,
                        by1,
                        bx2,
                        by2,
                        fw,
                        fh,
                    ),
                )
                sid = int(cur.lastrowid)
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM customer_sightings WHERE id=?", (sid,)
                ).fetchone()
        return self._sighting_row(row)

    def list_visits(self, customer_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM customer_visits WHERE customer_id=? "
                    "ORDER BY id DESC LIMIT ?",
                    (int(customer_id), limit),
                ).fetchall()
        return [self._visit_row(r) for r in rows]

    def list_sightings(
        self,
        *,
        customer_id: int | None = None,
        visit_id: int | None = None,
        day: str | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        day = (day or _today()).strip()
        clauses = ["created_at LIKE ?"]
        args: list[Any] = [f"{day}%"]
        if customer_id is not None:
            clauses.append("customer_id=?")
            args.append(int(customer_id))
        if visit_id is not None:
            clauses.append("visit_id=?")
            args.append(int(visit_id))
        args.append(limit)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM customer_sightings WHERE {' AND '.join(clauses)} "
                    f"ORDER BY id DESC LIMIT ?",
                    args,
                ).fetchall()
        return [self._sighting_row(r) for r in rows]

    def list_open_visits_today(self) -> list[dict[str, Any]]:
        day = _today()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT v.*, c.name AS customer_name FROM customer_visits v "
                    "JOIN customers c ON c.id=v.customer_id "
                    "WHERE v.started_at LIKE ? AND v.ended_at IS NULL "
                    "ORDER BY v.id DESC",
                    (f"{day}%",),
                ).fetchall()
        out = []
        for r in rows:
            vis = self._visit_row(r)
            vis["customer_name"] = r["customer_name"]
            out.append(vis)
        return out

    def summary_today(self) -> dict[str, Any]:
        day = _today()
        with self._lock:
            with self._connect() as conn:
                guests = conn.execute(
                    "SELECT COUNT(DISTINCT customer_id) AS n FROM customer_visits "
                    "WHERE started_at LIKE ?",
                    (f"{day}%",),
                ).fetchone()["n"]
                open_n = conn.execute(
                    "SELECT COUNT(*) AS n FROM customer_visits "
                    "WHERE started_at LIKE ? AND ended_at IS NULL",
                    (f"{day}%",),
                ).fetchone()["n"]
                visits = conn.execute(
                    "SELECT COUNT(*) AS n FROM customer_visits WHERE started_at LIKE ?",
                    (f"{day}%",),
                ).fetchone()["n"]
                total = conn.execute(
                    "SELECT COUNT(*) AS n FROM customers WHERE active=1"
                ).fetchone()["n"]
        return {
            "day": day,
            "guests_today": int(guests),
            "open_visits": int(open_n),
            "visits_today": int(visits),
            "known_customers": int(total),
        }

    def _visit_row(self, r: sqlite3.Row | None) -> dict[str, Any]:
        if r is None:
            return {}
        start = r["started_at"]
        end = r["ended_at"]
        last = r["last_seen_at"]
        start_ts = _parse_ts(start)
        end_ts = _parse_ts(end) if end else datetime.now()
        dur = None
        if start_ts and end_ts:
            dur = int((end_ts - start_ts).total_seconds())
        return {
            "id": int(r["id"]),
            "customer_id": int(r["customer_id"]),
            "started_at": start,
            "ended_at": end,
            "last_seen_at": last,
            "start_time": _clock(start),
            "end_time": _clock(end),
            "last_seen_time": _clock(last),
            "table_id": r["table_id"],
            "table_name": r["table_name"],
            "camera_id": r["camera_id"],
            "duration_sec": dur,
            "duration": _fmt_duration(dur),
            "status": "open" if end is None else "closed",
        }

    @staticmethod
    def _sighting_row(r: sqlite3.Row | None) -> dict[str, Any]:
        if r is None:
            return {}
        keys = r.keys()
        out: dict[str, Any] = {
            "id": int(r["id"]),
            "customer_id": int(r["customer_id"]),
            "visit_id": r["visit_id"],
            "camera_id": r["camera_id"],
            "table_id": r["table_id"],
            "table_name": r["table_name"],
            "score": r["score"],
            "created_at": r["created_at"],
            "time": _clock(r["created_at"]),
        }
        if "bbox_x1" in keys and r["bbox_x1"] is not None and r["frame_w"]:
            fw = float(r["frame_w"] or 0)
            fh = float(r["frame_h"] or 0)
            if fw > 1 and fh > 1:
                out["bbox"] = {
                    "x1": float(r["bbox_x1"]),
                    "y1": float(r["bbox_y1"]),
                    "x2": float(r["bbox_x2"]),
                    "y2": float(r["bbox_y2"]),
                    "frame_w": fw,
                    "frame_h": fh,
                    # Normalized 0–1 for resolution-independent overlay.
                    "nx1": float(r["bbox_x1"]) / fw,
                    "ny1": float(r["bbox_y1"]) / fh,
                    "nx2": float(r["bbox_x2"]) / fw,
                    "ny2": float(r["bbox_y2"]) / fh,
                }
        return out
