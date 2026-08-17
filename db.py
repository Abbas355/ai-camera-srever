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
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "auto_record" not in cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN auto_record INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
