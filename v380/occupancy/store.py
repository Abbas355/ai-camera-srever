"""SQLite persistence for floor occupancy zones and sessions."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from v380.paths import DATA_DIR

DB_PATH = DATA_DIR / "occupancy.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS occupancy_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS occupancy_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    camera_id INTEGER NOT NULL,
    points TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS occupancy_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    FOREIGN KEY (zone_id) REFERENCES occupancy_zones(id)
);

CREATE INDEX IF NOT EXISTS idx_occ_sessions_zone
    ON occupancy_sessions(zone_id, started_at);
CREATE INDEX IF NOT EXISTS idx_occ_sessions_day
    ON occupancy_sessions(started_at);
"""

DEFAULTS: dict[str, str] = {
    "enabled": "0",
    "camera_id": "",
    "poll_interval_sec": "2.5",
    # How long a zone must stay empty before flipping to Free.
    "empty_confirm_sec": "20",
    "min_face_area": "400",
    # face = InsightFace centers; yolo = person boxes for occupancy
    "detect_mode": "face",
    # Table boxes are NEVER auto-synced — only via Detect tables button
    "sync_tables": "0",
    # YOLO confidence threshold (Detect tables + YOLO occupancy)
    "yolo_conf": "0.35",
}


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
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _clock(value: str | None) -> str | None:
    if not value:
        return None
    ts = _parse_ts(value)
    return ts.strftime("%H:%M:%S") if ts else None


class OccupancyStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or DB_PATH)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            for k, v in DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO occupancy_settings(key, value) VALUES(?, ?)",
                    (k, v),
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
                rows = conn.execute("SELECT key, value FROM occupancy_settings").fetchall()
        raw = {r["key"]: r["value"] for r in rows}
        for k, v in DEFAULTS.items():
            raw.setdefault(k, v)
        cam_raw = (raw.get("camera_id") or "").strip()
        try:
            cam = int(cam_raw) if cam_raw else None
        except ValueError:
            cam = None
        try:
            poll = float(raw.get("poll_interval_sec") or DEFAULTS["poll_interval_sec"])
        except ValueError:
            poll = 2.5
        try:
            empty = int(float(raw.get("empty_confirm_sec") or DEFAULTS["empty_confirm_sec"]))
        except ValueError:
            empty = 20
        try:
            min_area = float(raw.get("min_face_area") or DEFAULTS["min_face_area"])
        except ValueError:
            min_area = 400.0
        mode = (raw.get("detect_mode") or DEFAULTS["detect_mode"]).strip().lower()
        if mode not in {"face", "yolo"}:
            mode = "face"
        try:
            yolo_conf = float(raw.get("yolo_conf") or DEFAULTS["yolo_conf"])
        except ValueError:
            yolo_conf = 0.35
        yolo_conf = max(0.05, min(0.95, yolo_conf))
        return {
            "enabled": (raw.get("enabled") or "0").strip() in {"1", "true", "yes", "on"},
            "camera_id": cam,
            "poll_interval_sec": max(1.0, poll),
            "empty_confirm_sec": max(5, empty),
            "min_face_area": max(200.0, min_area),
            "detect_mode": mode,
            "sync_tables": (raw.get("sync_tables") or "1").strip()
            in {"1", "true", "yes", "on"},
            "yolo_conf": round(yolo_conf, 3),
        }

    def update_settings(self, **kwargs: Any) -> dict[str, Any]:
        mapping: dict[str, str] = {}
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            mapping["enabled"] = "1" if bool(kwargs["enabled"]) else "0"
        if "camera_id" in kwargs:
            cam = kwargs["camera_id"]
            mapping["camera_id"] = "" if cam in (None, "") else str(int(cam))
        if "poll_interval_sec" in kwargs and kwargs["poll_interval_sec"] is not None:
            mapping["poll_interval_sec"] = str(float(kwargs["poll_interval_sec"]))
        if "empty_confirm_sec" in kwargs and kwargs["empty_confirm_sec"] is not None:
            mapping["empty_confirm_sec"] = str(int(kwargs["empty_confirm_sec"]))
        if "min_face_area" in kwargs and kwargs["min_face_area"] is not None:
            mapping["min_face_area"] = str(float(kwargs["min_face_area"]))
        if "detect_mode" in kwargs and kwargs["detect_mode"] is not None:
            mode = str(kwargs["detect_mode"]).strip().lower()
            mapping["detect_mode"] = mode if mode in {"face", "yolo"} else "face"
        if "sync_tables" in kwargs and kwargs["sync_tables"] is not None:
            mapping["sync_tables"] = "1" if bool(kwargs["sync_tables"]) else "0"
        if "yolo_conf" in kwargs and kwargs["yolo_conf"] is not None:
            try:
                conf = float(kwargs["yolo_conf"])
            except (TypeError, ValueError):
                conf = 0.35
            mapping["yolo_conf"] = str(round(max(0.05, min(0.95, conf)), 3))
        if mapping:
            with self._lock:
                with self._connect() as conn:
                    for k, v in mapping.items():
                        conn.execute(
                            "INSERT INTO occupancy_settings(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (k, v),
                        )
                    conn.commit()
        return self.get_settings()

    # --- zones ---

    def list_zones(self, *, camera_id: int | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM occupancy_zones"
        clauses: list[str] = []
        args: list[Any] = []
        if active_only:
            clauses.append("active=1")
        if camera_id is not None:
            clauses.append("camera_id=?")
            args.append(int(camera_id))
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id ASC"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(q, args).fetchall()
        return [self._zone_row(r) for r in rows]

    def get_zone(self, zone_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                r = conn.execute(
                    "SELECT * FROM occupancy_zones WHERE id=?", (int(zone_id),)
                ).fetchone()
        return self._zone_row(r) if r else None

    def add_zone(
        self,
        *,
        name: str,
        camera_id: int,
        points: list[list[float]],
    ) -> dict[str, Any]:
        now = _now()
        clean = (name or "").strip() or self.next_zone_name()
        pts = self._normalize_points(points)
        if len(pts) < 3:
            raise ValueError("Need at least 3 points for a table zone")
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO occupancy_zones(name, camera_id, points, active, created_at, updated_at) "
                    "VALUES(?, ?, ?, 1, ?, ?)",
                    (clean, int(camera_id), json.dumps(pts), now, now),
                )
                zid = int(cur.lastrowid)
                # Start in Free state.
                conn.execute(
                    "INSERT INTO occupancy_sessions(zone_id, status, started_at, ended_at) "
                    "VALUES(?, 'free', ?, NULL)",
                    (zid, now),
                )
                conn.commit()
        out = self.get_zone(zid)
        assert out is not None
        return out

    def update_zone(
        self,
        zone_id: int,
        *,
        name: str | None = None,
        points: list[list[float]] | None = None,
        active: bool | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM occupancy_zones WHERE id=?", (int(zone_id),)
                ).fetchone()
                if row is None:
                    return None
                if name is not None:
                    conn.execute(
                        "UPDATE occupancy_zones SET name=?, updated_at=? WHERE id=?",
                        ((name or "").strip() or "Table", _now(), int(zone_id)),
                    )
                if points is not None:
                    pts = self._normalize_points(points)
                    if len(pts) < 3:
                        raise ValueError("Need at least 3 points for a table zone")
                    conn.execute(
                        "UPDATE occupancy_zones SET points=?, updated_at=? WHERE id=?",
                        (json.dumps(pts), _now(), int(zone_id)),
                    )
                if active is not None:
                    conn.execute(
                        "UPDATE occupancy_zones SET active=?, updated_at=? WHERE id=?",
                        (1 if active else 0, _now(), int(zone_id)),
                    )
                conn.commit()
        return self.get_zone(zone_id)

    def delete_zone(self, zone_id: int) -> bool:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM occupancy_zones WHERE id=?", (int(zone_id),)
                ).fetchone()
                if row is None:
                    return False
                conn.execute(
                    "DELETE FROM occupancy_sessions WHERE zone_id=?", (int(zone_id),)
                )
                conn.execute("DELETE FROM occupancy_zones WHERE id=?", (int(zone_id),))
                conn.commit()
        return True

    def next_zone_name(self) -> str:
        """Next free T## — never reuse a number already assigned to any zone."""
        max_n = 0
        for z in self.list_zones(active_only=False):
            name = str(z.get("name") or "")
            m = re.match(r"^T(\d+)$", name.strip(), re.IGNORECASE)
            if m:
                max_n = max(max_n, int(m.group(1)))
            else:
                try:
                    max_n = max(max_n, int(z.get("id") or 0))
                except (TypeError, ValueError):
                    pass
        return f"T{max_n + 1:02d}"

    @staticmethod
    def _normalize_points(points: list[list[float]]) -> list[list[float]]:
        out: list[list[float]] = []
        for p in points or []:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            x = max(0.0, min(1.0, float(p[0])))
            y = max(0.0, min(1.0, float(p[1])))
            out.append([round(x, 5), round(y, 5)])
        return out

    @staticmethod
    def _zone_row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        try:
            pts = json.loads(r["points"] or "[]")
        except Exception:
            pts = []
        if not isinstance(pts, list):
            pts = []
        return {
            "id": int(r["id"]),
            "name": r["name"],
            "camera_id": int(r["camera_id"]),
            "points": pts,
            "active": bool(r["active"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    # --- sessions ---

    def open_session(self, zone_id: int) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                r = conn.execute(
                    "SELECT * FROM occupancy_sessions WHERE zone_id=? AND ended_at IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (int(zone_id),),
                ).fetchone()
        return self._session_row(r) if r else None

    def set_status(self, zone_id: int, status: str) -> dict[str, Any]:
        """Flip zone to free/occupied; close previous open session and open a new one."""
        status = "occupied" if status == "occupied" else "free"
        now = _now()
        with self._lock:
            with self._connect() as conn:
                open_row = conn.execute(
                    "SELECT * FROM occupancy_sessions WHERE zone_id=? AND ended_at IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (int(zone_id),),
                ).fetchone()
                if open_row is not None and open_row["status"] == status:
                    return self._session_row(open_row)
                if open_row is not None:
                    conn.execute(
                        "UPDATE occupancy_sessions SET ended_at=? WHERE id=?",
                        (now, int(open_row["id"])),
                    )
                cur = conn.execute(
                    "INSERT INTO occupancy_sessions(zone_id, status, started_at, ended_at) "
                    "VALUES(?, ?, ?, NULL)",
                    (int(zone_id), status, now),
                )
                sid = int(cur.lastrowid)
                conn.commit()
                r = conn.execute(
                    "SELECT * FROM occupancy_sessions WHERE id=?", (sid,)
                ).fetchone()
        return self._session_row(r)

    def list_sessions(
        self,
        *,
        day: str | None = None,
        zone_id: int | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        day = (day or _today()).strip()
        limit = max(1, min(1000, int(limit)))
        clauses = ["s.started_at LIKE ?"]
        args: list[Any] = [f"{day}%"]
        if zone_id is not None:
            clauses.append("s.zone_id=?")
            args.append(int(zone_id))
        args.append(limit)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT s.*, z.name AS zone_name FROM occupancy_sessions s "
                    "JOIN occupancy_zones z ON z.id=s.zone_id "
                    f"WHERE {' AND '.join(clauses)} "
                    "ORDER BY s.id DESC LIMIT ?",
                    args,
                ).fetchall()
        return [self._session_row(r, with_name=True) for r in rows]

    def summary_today(self) -> dict[str, Any]:
        day = _today()
        zones = self.list_zones(active_only=True)
        free_n = 0
        occ_n = 0
        items: list[dict[str, Any]] = []
        for z in zones:
            open_s = self.open_session(int(z["id"]))
            status = (open_s or {}).get("status") or "free"
            if status == "occupied":
                occ_n += 1
            else:
                free_n += 1
            items.append(
                {
                    "zone_id": int(z["id"]),
                    "name": z["name"],
                    "status": status,
                    "since": (open_s or {}).get("started_at"),
                    "since_time": (open_s or {}).get("start_time"),
                    "duration_sec": (open_s or {}).get("duration_sec"),
                    "duration": (open_s or {}).get("duration"),
                }
            )
        # Totals from closed+open sessions today.
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT zone_id, status, started_at, ended_at FROM occupancy_sessions "
                    "WHERE started_at LIKE ?",
                    (f"{day}%",),
                ).fetchall()
        occ_sec = 0
        free_sec = 0
        now = datetime.now()
        for r in rows:
            st = _parse_ts(r["started_at"])
            if st is None:
                continue
            en = _parse_ts(r["ended_at"]) or now
            dur = max(0, int((en - st).total_seconds()))
            if r["status"] == "occupied":
                occ_sec += dur
            else:
                free_sec += dur
        return {
            "day": day,
            "free": free_n,
            "occupied": occ_n,
            "zones": items,
            "time_occupied_sec": occ_sec,
            "time_occupied": _fmt_duration(occ_sec),
            "time_free_sec": free_sec,
            "time_free": _fmt_duration(free_sec),
        }

    def _session_row(
        self, r: sqlite3.Row | None, *, with_name: bool = False
    ) -> dict[str, Any]:
        if r is None:
            return {}
        start = r["started_at"]
        end = r["ended_at"]
        st = _parse_ts(start)
        en = _parse_ts(end) if end else datetime.now()
        dur = None
        if st is not None and en is not None:
            dur = max(0, int((en - st).total_seconds()))
        out: dict[str, Any] = {
            "id": int(r["id"]),
            "zone_id": int(r["zone_id"]),
            "status": r["status"],
            "started_at": start,
            "ended_at": end,
            "start_time": _clock(start),
            "end_time": _clock(end),
            "duration_sec": dur,
            "duration": _fmt_duration(dur),
            "open": end is None,
        }
        if with_name and "zone_name" in r.keys():
            out["zone_name"] = r["zone_name"]
        return out
