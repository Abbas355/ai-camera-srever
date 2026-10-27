"""API keys for external software (ServeX-style). Full key shown once; only hash stored."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime
from typing import Any

from v380.paths import DATA_DIR

DB_PATH = DATA_DIR / "integrations.db"
KEY_PREFIX = "opa_"  # Optiview attendance / public API
SCOPE_ATTENDANCE_READ = "attendance:read"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL DEFAULT 'attendance:read',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_raw() -> str:
    # opa_ + 32 url-safe chars — enough entropy, easy to paste into env files.
    return f"{KEY_PREFIX}{secrets.token_urlsafe(24)}"


class ApiKeyStore:
    _instance: ApiKeyStore | None = None
    _instance_lock = threading.Lock()

    def __init__(self, path: Any = None) -> None:
        self._path = path or DB_PATH
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @classmethod
    def instance(cls) -> ApiKeyStore:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def list_keys(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                if include_revoked:
                    rows = conn.execute(
                        "SELECT id, name, prefix, scope, created_at, last_used_at, revoked_at "
                        "FROM api_keys ORDER BY id DESC"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, name, prefix, scope, created_at, last_used_at, revoked_at "
                        "FROM api_keys WHERE revoked_at IS NULL ORDER BY id DESC"
                    ).fetchall()
        return [self._row(r) for r in rows]

    def create(self, name: str, *, scope: str = SCOPE_ATTENDANCE_READ) -> dict[str, Any]:
        name = (name or "").strip() or "Integration"
        scope = (scope or SCOPE_ATTENDANCE_READ).strip()
        raw = _generate_raw()
        prefix = raw[:12] + "…"
        key_hash = _hash_key(raw)
        created = _now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO api_keys(name, prefix, key_hash, scope, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (name, prefix, key_hash, scope, created),
                )
                kid = int(cur.lastrowid)
                conn.commit()
        return {
            "ok": True,
            "id": kid,
            "name": name,
            "prefix": prefix,
            "scope": scope,
            "created_at": created,
            "last_used_at": None,
            "revoked_at": None,
            # Full key only on create/rotate — caller must show once.
            "key": raw,
        }

    def rotate(self, key_id: int) -> dict[str, Any] | None:
        raw = _generate_raw()
        prefix = raw[:12] + "…"
        key_hash = _hash_key(raw)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, name, scope, created_at FROM api_keys "
                    "WHERE id=? AND revoked_at IS NULL",
                    (int(key_id),),
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE api_keys SET prefix=?, key_hash=?, last_used_at=NULL WHERE id=?",
                    (prefix, key_hash, int(key_id)),
                )
                conn.commit()
        return {
            "ok": True,
            "id": int(row["id"]),
            "name": row["name"],
            "prefix": prefix,
            "scope": row["scope"],
            "created_at": row["created_at"],
            "last_used_at": None,
            "revoked_at": None,
            "key": raw,
        }

    def revoke(self, key_id: int) -> bool:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_now(), int(key_id)),
                )
                conn.commit()
                return cur.rowcount > 0

    def authenticate(self, raw_key: str | None, *, scope: str | None = None) -> dict[str, Any] | None:
        """Validate Bearer / X-Api-Key. Returns key metadata or None."""
        raw = (raw_key or "").strip()
        if not raw or not raw.startswith(KEY_PREFIX):
            return None
        key_hash = _hash_key(raw)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, name, prefix, scope, created_at, last_used_at "
                    "FROM api_keys WHERE key_hash=? AND revoked_at IS NULL",
                    (key_hash,),
                ).fetchone()
                if row is None:
                    return None
                if scope and row["scope"] != scope and row["scope"] != "attendance:read":
                    # Future: multi-scope keys. For now attendance:read is the only scope.
                    if scope not in str(row["scope"]):
                        return None
                conn.execute(
                    "UPDATE api_keys SET last_used_at=? WHERE id=?",
                    (_now(), int(row["id"])),
                )
                conn.commit()
        return self._row(row)

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(r["id"]),
            "name": r["name"],
            "prefix": r["prefix"],
            "scope": r["scope"] if "scope" in r.keys() else SCOPE_ATTENDANCE_READ,
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"] if "last_used_at" in r.keys() else None,
            "revoked_at": r["revoked_at"] if "revoked_at" in r.keys() else None,
        }
