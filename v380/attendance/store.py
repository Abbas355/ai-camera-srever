"""SQLite persistence for attendance — separate DB file from cameras."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from v380.paths import DATA_DIR

DB_PATH = DATA_DIR / "attendance.db"
PHOTOS_DIR = DATA_DIR / "attendance" / "photos"

SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'staff',
    active INTEGER NOT NULL DEFAULT 1,
    embedding BLOB NOT NULL,
    photo_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    camera_id INTEGER,
    score REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (staff_id) REFERENCES attendance_staff(id)
);

CREATE INDEX IF NOT EXISTS idx_attendance_events_created
    ON attendance_events(created_at);
CREATE INDEX IF NOT EXISTS idx_attendance_events_staff_day
    ON attendance_events(staff_id, created_at);

CREATE TABLE IF NOT EXISTS attendance_staff_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    photo_path TEXT,
    area REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (staff_id) REFERENCES attendance_staff(id)
);
CREATE INDEX IF NOT EXISTS idx_attendance_staff_faces_staff
    ON attendance_staff_faces(staff_id);
"""

DEFAULTS: dict[str, str] = {
    "enabled": "0",
    "entrance_camera_id": "",
    # Optional dedicated exit camera (off = entrance does in + out).
    "exit_camera_enabled": "0",
    "exit_camera_id": "",
    # Restaurant closing (HH:MM local). Empty = no hours gate on exit cam.
    "closing_time": "",
    # Exit camera is active from (closing - N minutes) until (closing + 60).
    "exit_window_minutes": "90",
    # At closing time, auto check-out anyone still Working (no face needed).
    "auto_checkout_at_close": "1",
    # Cosine on InsightFace — 0.48 reduces strangers matching as staff.
    "match_threshold": "0.48",
    "cooldown_sec": "900",
    # Sub-second poll so fast walk-throughs are not missed.
    "poll_interval_sec": "0.5",
    # Minimum time after check-in before a new face match can count as check-out.
    "min_shift_sec": "1800",
}


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    raw = (value or "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        hh_s, mm_s = raw.split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except ValueError:
        pass
    return None


def exit_window_active(
    settings: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """True when exit camera should poll.

    No closing_time → always active (if exit cam enabled by caller).
    With closing_time → active in [closing - window, closing + 60 min].
    """
    parsed = _parse_hhmm(str(settings.get("closing_time") or ""))
    if parsed is None:
        return True
    now = now or datetime.now()
    try:
        window = int(float(settings.get("exit_window_minutes") or 90))
    except (TypeError, ValueError):
        window = 90
    window = max(15, min(360, window))
    hh, mm = parsed
    close_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    start = close_dt - timedelta(minutes=window)
    end = close_dt + timedelta(minutes=60)
    # Overnight close (e.g. 01:00): window may start previous calendar day.
    if start <= now <= end:
        return True
    # If close is early morning and now is late evening before midnight.
    if close_dt < now and (close_dt + timedelta(days=1) - timedelta(minutes=window)) <= now:
        return True
    if close_dt > now and (close_dt - timedelta(days=1)) <= now <= (
        close_dt - timedelta(days=1) + timedelta(minutes=60)
    ):
        return True
    return False


def past_closing_time(
    settings: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """True when local time is at/after closing today (for auto check-out)."""
    parsed = _parse_hhmm(str(settings.get("closing_time") or ""))
    if parsed is None:
        return False
    now = now or datetime.now()
    hh, mm = parsed
    close_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= close_dt


def _now() -> str:
    """Local wall clock — attendance days follow the restaurant site."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _parse_ts(value: str) -> datetime | None:
    raw = (value or "").strip().replace("Z", "").replace(" ", "T")
    if not raw:
        return None
    for n, fmt in ((19, "%Y-%m-%dT%H:%M:%S"), (16, "%Y-%m-%dT%H:%M")):
        try:
            return datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    return None


def _row_cam(row: sqlite3.Row | None) -> int | None:
    if row is None:
        return None
    try:
        raw = row["camera_id"]
    except (KeyError, IndexError):
        return None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
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
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _clock(value: str | None) -> str | None:
    if not value:
        return None
    ts = _parse_ts(value)
    return ts.strftime("%H:%M:%S") if ts else value[-8:] if len(value) >= 8 else value


class AttendanceStore:
    def __init__(self, db_path: Path | None = None, photos_dir: Path | None = None):
        self.db_path = Path(db_path or DB_PATH)
        self.photos_dir = Path(photos_dir or PHOTOS_DIR)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            for k, v in DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO attendance_settings(key, value) VALUES(?, ?)",
                    (k, v),
                )
            # One-shot: door recognition tuned for fast walk-throughs.
            if not conn.execute(
                "SELECT 1 FROM attendance_settings WHERE key='attn_v3'"
            ).fetchone():
                conn.execute(
                    "INSERT INTO attendance_settings(key, value) VALUES('match_threshold', '0.35') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                conn.execute(
                    "INSERT INTO attendance_settings(key, value) VALUES('poll_interval_sec', '0.5') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                conn.execute(
                    "INSERT INTO attendance_settings(key, value) VALUES('attn_v3', '1')"
                )
            # Stricter matching: raise floor so wrong faces are not tagged as staff.
            if not conn.execute(
                "SELECT 1 FROM attendance_settings WHERE key='attn_v4'"
            ).fetchone():
                conn.execute(
                    "INSERT INTO attendance_settings(key, value) VALUES('match_threshold', '0.48') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                conn.execute(
                    "INSERT INTO attendance_settings(key, value) VALUES('attn_v4', '1')"
                )
            # Backfill primary embedding into faces table for existing staff.
            missing = conn.execute(
                "SELECT s.id, s.embedding, s.photo_path FROM attendance_staff s "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM attendance_staff_faces f WHERE f.staff_id=s.id"
                ")"
            ).fetchall()
            now = _now()
            for r in missing:
                conn.execute(
                    "INSERT INTO attendance_staff_faces"
                    "(staff_id, embedding, photo_path, area, created_at) "
                    "VALUES(?, ?, ?, 0, ?)",
                    (int(r["id"]), bytes(r["embedding"]), r["photo_path"], now),
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
                rows = conn.execute("SELECT key, value FROM attendance_settings").fetchall()
        raw = {r["key"]: r["value"] for r in rows}
        for k, v in DEFAULTS.items():
            raw.setdefault(k, v)
        cam_raw = (raw.get("entrance_camera_id") or "").strip()
        try:
            entrance = int(cam_raw) if cam_raw else None
        except ValueError:
            entrance = None
        exit_raw = (raw.get("exit_camera_id") or "").strip()
        try:
            exit_cam = int(exit_raw) if exit_raw else None
        except ValueError:
            exit_cam = None
        try:
            threshold = float(raw.get("match_threshold") or DEFAULTS["match_threshold"])
        except ValueError:
            threshold = float(DEFAULTS["match_threshold"])
        threshold = max(0.35, min(0.70, threshold))
        try:
            cooldown = int(float(raw.get("cooldown_sec") or DEFAULTS["cooldown_sec"]))
        except ValueError:
            cooldown = int(DEFAULTS["cooldown_sec"])
        try:
            poll = float(raw.get("poll_interval_sec") or DEFAULTS["poll_interval_sec"])
        except ValueError:
            poll = float(DEFAULTS["poll_interval_sec"])
        try:
            min_shift = int(float(raw.get("min_shift_sec") or DEFAULTS["min_shift_sec"]))
        except ValueError:
            min_shift = int(DEFAULTS["min_shift_sec"])
        closing_raw = (raw.get("closing_time") or "").strip()
        closing = closing_raw if _parse_hhmm(closing_raw) else ""
        try:
            exit_win = int(float(raw.get("exit_window_minutes") or DEFAULTS["exit_window_minutes"]))
        except ValueError:
            exit_win = int(DEFAULTS["exit_window_minutes"])
        exit_win = max(15, min(360, exit_win))
        # Heal: Auto check-in with no entrance but an exit cam set → use exit as entrance
        # so the watcher actually runs (common one-camera setups).
        if entrance is None and exit_cam is not None:
            entrance = int(exit_cam)
            try:
                with self._lock:
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT INTO attendance_settings(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            ("entrance_camera_id", str(entrance)),
                        )
                        conn.commit()
            except Exception:
                pass
        settings = {
            "enabled": (raw.get("enabled") or "0").strip() in {"1", "true", "yes", "on"},
            "entrance_camera_id": entrance,
            "exit_camera_enabled": (raw.get("exit_camera_enabled") or "0").strip()
            in {"1", "true", "yes", "on"},
            "exit_camera_id": exit_cam,
            "closing_time": closing,
            "exit_window_minutes": exit_win,
            "auto_checkout_at_close": (raw.get("auto_checkout_at_close") or "1").strip()
            in {"1", "true", "yes", "on"},
            "match_threshold": threshold,
            "cooldown_sec": cooldown,
            "poll_interval_sec": max(0.5, poll),
            "min_shift_sec": max(60, min_shift),
        }
        settings["exit_window_active"] = exit_window_active(settings)
        settings["past_closing"] = past_closing_time(settings)
        return settings

    def update_settings(self, **kwargs: Any) -> dict[str, Any]:
        mapping: dict[str, str] = {}
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            mapping["enabled"] = "1" if bool(kwargs["enabled"]) else "0"
        if "entrance_camera_id" in kwargs:
            cid = kwargs["entrance_camera_id"]
            mapping["entrance_camera_id"] = "" if cid is None or cid == "" else str(int(cid))
        if "exit_camera_enabled" in kwargs and kwargs["exit_camera_enabled"] is not None:
            mapping["exit_camera_enabled"] = (
                "1" if bool(kwargs["exit_camera_enabled"]) else "0"
            )
        if "exit_camera_id" in kwargs:
            cid = kwargs["exit_camera_id"]
            mapping["exit_camera_id"] = "" if cid is None or cid == "" else str(int(cid))
        if "closing_time" in kwargs and kwargs["closing_time"] is not None:
            raw_ct = str(kwargs["closing_time"]).strip()
            mapping["closing_time"] = raw_ct if _parse_hhmm(raw_ct) else ""
        if "exit_window_minutes" in kwargs and kwargs["exit_window_minutes"] is not None:
            try:
                w = int(float(kwargs["exit_window_minutes"]))
            except (TypeError, ValueError):
                w = 90
            mapping["exit_window_minutes"] = str(max(15, min(360, w)))
        if "auto_checkout_at_close" in kwargs and kwargs["auto_checkout_at_close"] is not None:
            mapping["auto_checkout_at_close"] = (
                "1" if bool(kwargs["auto_checkout_at_close"]) else "0"
            )
        if "match_threshold" in kwargs and kwargs["match_threshold"] is not None:
            # Clamp: too low → false staff IDs; too high → misses real staff.
            th = float(kwargs["match_threshold"])
            th = max(0.35, min(0.70, th))
            mapping["match_threshold"] = str(th)
        if "cooldown_sec" in kwargs and kwargs["cooldown_sec"] is not None:
            mapping["cooldown_sec"] = str(int(kwargs["cooldown_sec"]))
        if "poll_interval_sec" in kwargs and kwargs["poll_interval_sec"] is not None:
            mapping["poll_interval_sec"] = str(float(kwargs["poll_interval_sec"]))
        if "min_shift_sec" in kwargs and kwargs["min_shift_sec"] is not None:
            mapping["min_shift_sec"] = str(int(kwargs["min_shift_sec"]))
        if mapping:
            with self._lock:
                with self._connect() as conn:
                    for k, v in mapping.items():
                        conn.execute(
                            "INSERT INTO attendance_settings(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (k, v),
                        )
                    conn.commit()
        return self.get_settings()

    # --- staff ---

    def list_staff(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT id, name, role, active, photo_path, created_at, updated_at FROM attendance_staff"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY name COLLATE NOCASE"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(q).fetchall()
        return [self._staff_row(r) for r in rows]

    def get_staff(self, staff_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                r = conn.execute(
                    "SELECT id, name, role, active, photo_path, created_at, updated_at "
                    "FROM attendance_staff WHERE id=?",
                    (int(staff_id),),
                ).fetchone()
        return self._staff_row(r) if r else None

    def list_embeddings(self) -> list[tuple[int, bytes]]:
        """All active staff face embeddings (multi-angle via faces table)."""
        with self._lock:
            with self._connect() as conn:
                faces = conn.execute(
                    "SELECT f.staff_id, f.embedding FROM attendance_staff_faces f "
                    "JOIN attendance_staff s ON s.id=f.staff_id WHERE s.active=1"
                ).fetchall()
                if faces:
                    return [
                        (int(r["staff_id"]), bytes(r["embedding"])) for r in faces
                    ]
                # Fallback if faces table empty (should not happen after backfill).
                primary = conn.execute(
                    "SELECT id, embedding FROM attendance_staff WHERE active=1"
                ).fetchall()
        return [(int(r["id"]), bytes(r["embedding"])) for r in primary]

    def add_staff(
        self,
        *,
        name: str,
        role: str,
        embedding: bytes,
        photo_bytes: bytes | None = None,
        photo_area: float = 0.0,
    ) -> dict[str, Any]:
        now = _now()
        clean_name = (name or "").strip() or "Staff"
        clean_role = (role or "").strip() or "staff"
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO attendance_staff(name, role, active, embedding, photo_path, created_at, updated_at) "
                    "VALUES(?, ?, 1, ?, NULL, ?, ?)",
                    (clean_name, clean_role, embedding, now, now),
                )
                staff_id = int(cur.lastrowid)
                photo_path = None
                if photo_bytes:
                    photo_path = str(self._write_photo(staff_id, photo_bytes))
                    conn.execute(
                        "UPDATE attendance_staff SET photo_path=? WHERE id=?",
                        (photo_path, staff_id),
                    )
                # Mirror primary into faces table for multi-angle matching.
                conn.execute(
                    "INSERT INTO attendance_staff_faces(staff_id, embedding, photo_path, area, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (staff_id, embedding, photo_path, float(photo_area or 0), now),
                )
                conn.commit()
        out = self.get_staff(staff_id)
        assert out is not None
        return out

    def add_staff_face(
        self,
        staff_id: int,
        *,
        embedding: bytes,
        photo_bytes: bytes | None = None,
        photo_area: float = 0.0,
        max_faces: int = 4,
    ) -> dict[str, Any] | None:
        """Add an extra angle for a staff member (up to max_faces total extras+primary)."""
        staff = self.get_staff(int(staff_id))
        if staff is None:
            return None
        now = _now()
        with self._lock:
            with self._connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM attendance_staff_faces WHERE staff_id=?",
                    (int(staff_id),),
                ).fetchone()["n"]
                if int(n) >= int(max_faces):
                    # Drop oldest extra (keep newest slots).
                    oldest = conn.execute(
                        "SELECT id, photo_path FROM attendance_staff_faces "
                        "WHERE staff_id=? ORDER BY id ASC LIMIT 1",
                        (int(staff_id),),
                    ).fetchone()
                    if oldest:
                        conn.execute(
                            "DELETE FROM attendance_staff_faces WHERE id=?",
                            (int(oldest["id"]),),
                        )
                        if oldest["photo_path"]:
                            try:
                                Path(str(oldest["photo_path"])).unlink(missing_ok=True)
                            except Exception:
                                pass
                photo_path = None
                if photo_bytes:
                    path = self.photos_dir / f"{int(staff_id)}_{int(time.time())}.jpg"
                    path.write_bytes(photo_bytes)
                    photo_path = str(path)
                conn.execute(
                    "INSERT INTO attendance_staff_faces(staff_id, embedding, photo_path, area, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        int(staff_id),
                        embedding,
                        photo_path,
                        float(photo_area or 0),
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE attendance_staff SET updated_at=? WHERE id=?",
                    (now, int(staff_id)),
                )
                conn.commit()
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM attendance_staff_faces WHERE staff_id=?",
                    (int(staff_id),),
                ).fetchone()["n"]
        return {"ok": True, "staff_id": int(staff_id), "face_count": int(count)}

    def staff_face_count(self, staff_id: int) -> int:
        with self._lock:
            with self._connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM attendance_staff_faces WHERE staff_id=?",
                    (int(staff_id),),
                ).fetchone()["n"]
        return int(n)

    def update_primary_embedding(
        self,
        staff_id: int,
        *,
        embedding: bytes,
        photo_bytes: bytes | None = None,
        photo_area: float = 0.0,
    ) -> bool:
        """Refresh the main staff embedding/photo (used after Add face)."""
        staff = self.get_staff(int(staff_id))
        if staff is None:
            return False
        now = _now()
        with self._lock:
            with self._connect() as conn:
                photo_path = staff.get("photo_path")
                if photo_bytes:
                    path = self._write_photo(int(staff_id), photo_bytes)
                    photo_path = str(path)
                conn.execute(
                    "UPDATE attendance_staff SET embedding=?, photo_path=?, updated_at=? "
                    "WHERE id=?",
                    (embedding, photo_path, now, int(staff_id)),
                )
                conn.commit()
        return True

    def set_staff_active(self, staff_id: int, active: bool) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE attendance_staff SET active=?, updated_at=? WHERE id=?",
                    (1 if active else 0, _now(), int(staff_id)),
                )
                conn.commit()
        return self.get_staff(staff_id)

    def delete_staff(self, staff_id: int) -> bool:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT photo_path FROM attendance_staff WHERE id=?",
                    (int(staff_id),),
                ).fetchone()
                if row is None:
                    return False
                faces = conn.execute(
                    "SELECT photo_path FROM attendance_staff_faces WHERE staff_id=?",
                    (int(staff_id),),
                ).fetchall()
                conn.execute(
                    "DELETE FROM attendance_staff_faces WHERE staff_id=?", (int(staff_id),)
                )
                conn.execute("DELETE FROM attendance_events WHERE staff_id=?", (int(staff_id),))
                conn.execute("DELETE FROM attendance_staff WHERE id=?", (int(staff_id),))
                conn.commit()
        photo = row["photo_path"]
        if photo:
            try:
                Path(photo).unlink(missing_ok=True)
            except Exception:
                pass
        for fr in faces:
            if fr["photo_path"]:
                try:
                    Path(str(fr["photo_path"])).unlink(missing_ok=True)
                except Exception:
                    pass
        return True

    def photo_path_for(self, staff_id: int) -> Path | None:
        staff = self.get_staff(staff_id)
        if not staff or not staff.get("photo_path"):
            return None
        p = Path(str(staff["photo_path"]))
        return p if p.is_file() else None

    def _write_photo(self, staff_id: int, data: bytes) -> Path:
        path = self.photos_dir / f"{int(staff_id)}.jpg"
        path.write_bytes(data)
        return path

    @staticmethod
    def _staff_row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        return {
            "id": int(r["id"]),
            "name": r["name"],
            "role": r["role"],
            "active": bool(r["active"]),
            "photo_path": r["photo_path"],
            "has_photo": bool(r["photo_path"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    # --- events ---

    def add_event(
        self,
        *,
        staff_id: int,
        event_type: str,
        camera_id: int | None,
        score: float | None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO attendance_events(staff_id, event_type, camera_id, score, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (int(staff_id), event_type, camera_id, score, now),
                )
                eid = int(cur.lastrowid)
                conn.commit()
                row = conn.execute(
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.id=?",
                    (eid,),
                ).fetchone()
        return self._event_row(row)

    def today_check_in(self, staff_id: int) -> dict[str, Any] | None:
        day = _today_prefix()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.staff_id=? AND e.event_type='check_in' AND e.created_at LIKE ? "
                    "ORDER BY e.id DESC LIMIT 1",
                    (int(staff_id), f"{day}%"),
                ).fetchone()
        return self._event_row(row) if row else None

    def list_open_sessions(self, *, staff_id: int | None = None) -> list[dict[str, Any]]:
        """Sessions still Working (unpaired check-in), including overnight shifts."""
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        with self._lock:
            with self._connect() as conn:
                q = (
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.created_at >= ?"
                )
                args: list[Any] = [f"{since}T00:00:00"]
                if staff_id is not None:
                    q += " AND e.staff_id=?"
                    args.append(int(staff_id))
                q += " ORDER BY e.staff_id ASC, e.id ASC"
                rows = conn.execute(q, args).fetchall()
        return [s for s in self._sessions_from_rows(rows) if s.get("status") == "working"]

    def open_session(self, staff_id: int, *, day: str | None = None) -> dict[str, Any] | None:
        """Latest unpaired check-in (no check-out after it).

        When [day] is omitted, searches recent days so overnight shifts stay Working
        instead of looking Absent the next calendar day.
        """
        if day is not None:
            sessions = self.list_sessions(day=day, staff_id=int(staff_id))
            for s in sessions:
                if s.get("status") == "working":
                    return s
            return None
        opens = self.list_open_sessions(staff_id=int(staff_id))
        return opens[0] if opens else None

    def force_checkout_open_sessions(
        self,
        *,
        camera_id: int | None = None,
        reason: str = "closing_time",
    ) -> list[dict[str, Any]]:
        """Check out everyone still Working (including overnight open shifts)."""
        open_ones = self.list_open_sessions()
        out: list[dict[str, Any]] = []
        for s in open_ones:
            sid = int(s["staff_id"])
            ev = self.add_event(
                staff_id=sid,
                event_type="check_out",
                camera_id=camera_id,
                score=None,
            )
            out.append(
                {
                    "staff_id": sid,
                    "staff_name": s.get("staff_name"),
                    "event": ev,
                    "reason": reason,
                }
            )
        return out

    def last_event_for(self, staff_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.staff_id=? ORDER BY e.id DESC LIMIT 1",
                    (int(staff_id),),
                ).fetchone()
        return self._event_row(row) if row else None

    def list_events(
        self,
        *,
        day: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        day = (day or _today_prefix()).strip()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.created_at LIKE ? "
                    "ORDER BY e.id DESC LIMIT ?",
                    (f"{day}%", limit),
                ).fetchall()
        return [self._event_row(r) for r in rows]

    def list_sessions(
        self,
        *,
        day: str | None = None,
        staff_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Pair check-in / check-out into day sessions with duration.

        For the current calendar day, also attaches overnight Working sessions that
        started on an earlier day so the board shows Present/Working correctly.
        """
        day = (day or _today_prefix()).strip()
        today = _today_prefix()
        with self._lock:
            with self._connect() as conn:
                q = (
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.created_at LIKE ?"
                )
                args: list[Any] = [f"{day}%"]
                if staff_id is not None:
                    q += " AND e.staff_id=?"
                    args.append(int(staff_id))
                q += " ORDER BY e.staff_id ASC, e.id ASC"
                rows = conn.execute(q, args).fetchall()
        sessions = self._sessions_from_rows(rows)
        if day == today:
            for s in self.list_open_sessions(staff_id=staff_id):
                cin_day = str(s.get("check_in") or "")[:10]
                if not cin_day or cin_day >= day:
                    continue
                already = any(
                    int(x.get("staff_id") or 0) == int(s.get("staff_id") or 0)
                    and str(x.get("check_in") or "") == str(s.get("check_in") or "")
                    for x in sessions
                )
                if not already:
                    sessions.append(s)
            sessions.sort(key=lambda s: s.get("check_in") or "", reverse=True)
        return sessions

    def list_sessions_for_staff(
        self,
        staff_id: int,
        *,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """All check-in/out sessions for one person over the last N days."""
        days = max(1, min(120, int(days)))
        since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT e.id, e.staff_id, e.event_type, e.camera_id, e.score, e.created_at, "
                    "s.name AS staff_name, s.role AS staff_role "
                    "FROM attendance_events e JOIN attendance_staff s ON s.id=e.staff_id "
                    "WHERE e.staff_id=? AND e.created_at >= ? "
                    "ORDER BY e.id ASC",
                    (int(staff_id), f"{since}T00:00:00"),
                ).fetchall()
        return self._sessions_from_rows(rows)

    def staff_detail(self, staff_id: int, *, days: int = 30) -> dict[str, Any] | None:
        """Staff profile + visit history (entry/exit/duration) + day calendar."""
        staff = self.get_staff(int(staff_id))
        if staff is None:
            return None
        days = max(1, min(120, int(days)))
        sessions = self.list_sessions_for_staff(int(staff_id), days=days)
        today = _today_prefix()
        today_sessions = [
            s for s in sessions if str(s.get("check_in") or "").startswith(today)
        ]
        open_any = next((s for s in sessions if s.get("status") == "working"), None)
        # Overnight: still Working from yesterday → Present/Working today.
        overnight = bool(
            open_any is not None
            and not str(open_any.get("check_in") or "").startswith(today)
        )
        if overnight and open_any is not None:
            today_sessions = [open_any, *today_sessions]
        working_now = any(s.get("status") == "working" for s in today_sessions)
        today_sec = sum(
            int(s.get("duration_sec") or 0)
            for s in today_sessions
            if str(s.get("check_in") or "").startswith(today)
        )
        if overnight and open_any is not None:
            # Count only time since local midnight for the overnight open shift.
            start = _parse_ts(str(open_any.get("check_in") or ""))
            midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if start is not None and start < midnight:
                today_sec += max(0, int((datetime.now() - midnight).total_seconds()))
            elif start is not None:
                today_sec += max(0, int(open_any.get("duration_sec") or 0))
        all_sec = sum(int(s.get("duration_sec") or 0) for s in sessions)
        days_present = {
            str(s.get("check_in") or "")[:10]
            for s in sessions
            if s.get("check_in")
        }
        if overnight:
            days_present.add(today)
        # Day-by-day present / absent for the UI calendar (oldest → newest).
        by_day: dict[str, list[dict[str, Any]]] = {}
        for s in sessions:
            d = str(s.get("day") or "")[:10]
            if d:
                by_day.setdefault(d, []).append(s)
        calendar: list[dict[str, Any]] = []
        today_d = datetime.now().date()
        for i in range(days - 1, -1, -1):
            d = (today_d - timedelta(days=i)).strftime("%Y-%m-%d")
            day_sess = list(by_day.get(d, []))
            if d == today and overnight and open_any is not None:
                if not any(
                    str(x.get("check_in") or "") == str(open_any.get("check_in") or "")
                    for x in day_sess
                ):
                    day_sess = [open_any, *day_sess]
            present = bool(day_sess)
            working = any(x.get("status") == "working" for x in day_sess)
            if d == today and overnight:
                present = True
                working = True
            sec = sum(int(x.get("duration_sec") or 0) for x in day_sess)
            if d == today and overnight and open_any is not None:
                start = _parse_ts(str(open_any.get("check_in") or ""))
                midnight = datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if start is not None and start < midnight:
                    # Prefer "time since midnight" over full multi-day duration.
                    sec = sum(
                        int(x.get("duration_sec") or 0)
                        for x in day_sess
                        if str(x.get("check_in") or "").startswith(today)
                    ) + max(0, int((datetime.now() - midnight).total_seconds()))
            cams = sorted(
                {
                    int(c)
                    for x in day_sess
                    for c in (x.get("camera_id_in"), x.get("camera_id_out"), x.get("camera_id"))
                    if c is not None
                }
            )
            if working:
                status = "working"
            elif present:
                status = "present"
            else:
                status = "absent"
            calendar.append(
                {
                    "day": d,
                    "present": present,
                    "status": status,
                    "session_count": len(day_sess),
                    "time_sec": sec,
                    "time": _fmt_duration(sec) if present else None,
                    "camera_ids": cams,
                }
            )
        face_count = self.staff_face_count(int(staff_id))
        staff = {**staff, "face_count": face_count}
        absent_count = sum(1 for c in calendar if not c["present"])
        present_today = bool(today_sessions) or overnight
        return {
            "ok": True,
            "staff": staff,
            "sessions": sessions,
            "sessions_today": today_sessions,
            "calendar": calendar,
            "summary": {
                "days": int(days),
                "visit_count": len(sessions),
                "visits_today": len(today_sessions),
                "days_present": len(days_present),
                "days_absent": absent_count,
                "time_today_sec": today_sec,
                "time_today": _fmt_duration(today_sec),
                "time_total_sec": all_sec,
                "time_total": _fmt_duration(all_sec),
                "working_now": working_now,
                "today_status": (
                    "working"
                    if working_now
                    else ("present" if present_today else "absent")
                ),
                "face_count": face_count,
                "open_session": open_any
                if working_now
                else next(
                    (s for s in today_sessions if s.get("status") == "working"),
                    None,
                ),
            },
        }

    def _sessions_from_rows(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        by_staff: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            by_staff.setdefault(int(r["staff_id"]), []).append(r)

        now = datetime.now()
        sessions: list[dict[str, Any]] = []
        for sid, events in by_staff.items():
            open_in: sqlite3.Row | None = None
            for ev in events:
                et = ev["event_type"]
                if et == "check_in":
                    if open_in is not None:
                        # Unclosed previous — close visually at same stamp before opening new.
                        sessions.append(
                            self._session_dict(
                                staff_id=sid,
                                staff_name=open_in["staff_name"],
                                staff_role=open_in["staff_role"],
                                check_in=open_in["created_at"],
                                check_out=None,
                                camera_id_in=_row_cam(open_in),
                                camera_id_out=None,
                                now=now,
                            )
                        )
                    open_in = ev
                elif et == "check_out" and open_in is not None:
                    sessions.append(
                        self._session_dict(
                            staff_id=sid,
                            staff_name=open_in["staff_name"],
                            staff_role=open_in["staff_role"],
                            check_in=open_in["created_at"],
                            check_out=ev["created_at"],
                            camera_id_in=_row_cam(open_in),
                            camera_id_out=_row_cam(ev),
                            now=now,
                        )
                    )
                    open_in = None
            if open_in is not None:
                sessions.append(
                    self._session_dict(
                        staff_id=sid,
                        staff_name=open_in["staff_name"],
                        staff_role=open_in["staff_role"],
                        check_in=open_in["created_at"],
                        check_out=None,
                        camera_id_in=_row_cam(open_in),
                        camera_id_out=None,
                        now=now,
                    )
                )

        # Newest activity first for the board.
        sessions.sort(key=lambda s: s.get("check_in") or "", reverse=True)
        return sessions

    @staticmethod
    def _session_dict(
        *,
        staff_id: int,
        staff_name: str,
        staff_role: str,
        check_in: str,
        check_out: str | None,
        now: datetime,
        camera_id_in: int | None = None,
        camera_id_out: int | None = None,
    ) -> dict[str, Any]:
        start = _parse_ts(check_in)
        end = _parse_ts(check_out) if check_out else None
        if start and end:
            dur = int((end - start).total_seconds())
            status = "completed"
        elif start:
            dur = int((now - start).total_seconds())
            status = "working"
        else:
            dur = None
            status = "unknown"
        day = (check_in or "")[:10]
        return {
            "staff_id": int(staff_id),
            "staff_name": staff_name,
            "staff_role": staff_role,
            "day": day,
            "check_in": check_in,
            "check_out": check_out,
            "check_in_time": _clock(check_in),
            "check_out_time": _clock(check_out),
            "duration_sec": dur,
            "duration": _fmt_duration(dur),
            "status": status,
            # Primary camera = where they checked in (entrance).
            "camera_id": camera_id_in,
            "camera_id_in": camera_id_in,
            "camera_id_out": camera_id_out,
        }

    def summary_today(self) -> dict[str, Any]:
        day = _today_prefix()
        sessions = self.list_sessions(day=day)
        working = sum(1 for s in sessions if s.get("status") == "working")
        completed = sum(1 for s in sessions if s.get("status") == "completed")
        staff_with_any = {int(s["staff_id"]) for s in sessions}
        with self._lock:
            with self._connect() as conn:
                total_staff = conn.execute(
                    "SELECT COUNT(*) AS n FROM attendance_staff WHERE active=1"
                ).fetchone()["n"]
        return {
            "day": day,
            "active_staff": int(total_staff),
            "checked_in": working,
            "checked_out": completed,
            "present_today": len(staff_with_any),
            "pending": max(0, int(total_staff) - len(staff_with_any)),
        }

    @staticmethod
    def _event_row(r: sqlite3.Row | None) -> dict[str, Any]:
        if r is None:
            return {}
        return {
            "id": int(r["id"]),
            "staff_id": int(r["staff_id"]),
            "staff_name": r["staff_name"],
            "staff_role": r["staff_role"],
            "event_type": r["event_type"],
            "camera_id": r["camera_id"],
            "score": r["score"],
            "created_at": r["created_at"],
        }

    def export_debug(self) -> str:
        return json.dumps(self.get_settings(), indent=2)
