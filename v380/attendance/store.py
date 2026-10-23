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
    # Cosine on InsightFace — 0.35 catches angle/lighting changes at the door.
    "match_threshold": "0.35",
    "cooldown_sec": "900",
    # Sub-second poll so fast walk-throughs are not missed.
    "poll_interval_sec": "0.5",
    # Minimum time after check-in before a new face match can count as check-out.
    "min_shift_sec": "1800",
}


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
        try:
            threshold = float(raw.get("match_threshold") or DEFAULTS["match_threshold"])
        except ValueError:
            threshold = float(DEFAULTS["match_threshold"])
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
        return {
            "enabled": (raw.get("enabled") or "0").strip() in {"1", "true", "yes", "on"},
            "entrance_camera_id": entrance,
            "match_threshold": threshold,
            "cooldown_sec": cooldown,
            "poll_interval_sec": max(0.5, poll),
            "min_shift_sec": max(60, min_shift),
        }

    def update_settings(self, **kwargs: Any) -> dict[str, Any]:
        mapping: dict[str, str] = {}
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            mapping["enabled"] = "1" if bool(kwargs["enabled"]) else "0"
        if "entrance_camera_id" in kwargs:
            cid = kwargs["entrance_camera_id"]
            mapping["entrance_camera_id"] = "" if cid is None or cid == "" else str(int(cid))
        if "match_threshold" in kwargs and kwargs["match_threshold"] is not None:
            mapping["match_threshold"] = str(float(kwargs["match_threshold"]))
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

    def open_session(self, staff_id: int, *, day: str | None = None) -> dict[str, Any] | None:
        """Latest unpaired check-in today (no check-out after it)."""
        sessions = self.list_sessions(day=day, staff_id=int(staff_id))
        for s in sessions:
            if s.get("status") == "working":
                return s
        return None

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
        """Pair check-in / check-out into day sessions with duration."""
        day = (day or _today_prefix()).strip()
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
        return self._sessions_from_rows(rows)

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
        """Staff profile + visit history (entry/exit/duration) + totals."""
        staff = self.get_staff(int(staff_id))
        if staff is None:
            return None
        sessions = self.list_sessions_for_staff(int(staff_id), days=days)
        today = _today_prefix()
        today_sessions = [s for s in sessions if str(s.get("check_in") or "").startswith(today)]
        working_now = any(s.get("status") == "working" for s in today_sessions)
        today_sec = sum(int(s.get("duration_sec") or 0) for s in today_sessions)
        all_sec = sum(int(s.get("duration_sec") or 0) for s in sessions)
        days_present = {
            str(s.get("check_in") or "")[:10]
            for s in sessions
            if s.get("check_in")
        }
        face_count = self.staff_face_count(int(staff_id))
        staff = {**staff, "face_count": face_count}
        return {
            "ok": True,
            "staff": staff,
            "sessions": sessions,
            "sessions_today": today_sessions,
            "summary": {
                "days": int(days),
                "visit_count": len(sessions),
                "visits_today": len(today_sessions),
                "days_present": len(days_present),
                "time_today_sec": today_sec,
                "time_today": _fmt_duration(today_sec),
                "time_total_sec": all_sec,
                "time_total": _fmt_duration(all_sec),
                "working_now": working_now,
                "face_count": face_count,
                "open_session": next(
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
