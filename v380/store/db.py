"""SQLite connection and schema. Swap the path later for PostgreSQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    device_id TEXT NOT NULL UNIQUE,
    mac TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 8800,
    username TEXT NOT NULL,
    password_enc BLOB NOT NULL,
    source TEXT NOT NULL DEFAULT 'lan' CHECK (source IN ('lan', 'cloud')),
    quality INTEGER NOT NULL DEFAULT 1 CHECK (quality IN (0, 1)),
    auto_record INTEGER NOT NULL DEFAULT 0 CHECK (auto_record IN (0, 1)),
    record_chunk TEXT NOT NULL DEFAULT 'minute' CHECK (record_chunk IN ('hour', 'minute')),
    brand TEXT NOT NULL DEFAULT 'v380',
    rtsp_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS camera_status (
    camera_id INTEGER PRIMARY KEY,
    last_codec TEXT,
    last_error TEXT,
    last_seen TEXT,
    last_width INTEGER,
    last_height INTEGER,
    FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cameras_device_id ON cameras(device_id);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pass_salt BLOB NOT NULL,
    pass_hash BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ezviz_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_enc BLOB NOT NULL,
    token_enc BLOB,
    api_url TEXT NOT NULL DEFAULT 'apiisgp.ezvizlife.com',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "auto_record" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN auto_record INTEGER NOT NULL DEFAULT 0")
    if "record_chunk" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN record_chunk TEXT NOT NULL DEFAULT 'minute'")
    if "brand" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN brand TEXT NOT NULL DEFAULT 'v380'")
    if "rtsp_url" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN rtsp_url TEXT NOT NULL DEFAULT ''")
    if "ezviz_account_id" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN ezviz_account_id INTEGER NOT NULL DEFAULT 0")
    if "ezviz_serial" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN ezviz_serial TEXT NOT NULL DEFAULT ''")
    # One-shot: prefer 1-minute clips (SD-card style) for cameras still on hourly default.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _optiview_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    done = conn.execute(
        "SELECT 1 FROM _optiview_meta WHERE key='chunk_minute_default_v1'"
    ).fetchone()
    if not done:
        conn.execute("UPDATE cameras SET record_chunk='minute' WHERE record_chunk='hour'")
        conn.execute(
            "INSERT INTO _optiview_meta(key, value) VALUES('chunk_minute_default_v1', '1')"
        )
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
