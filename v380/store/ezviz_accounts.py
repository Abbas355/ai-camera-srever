"""Multiple EZVIZ cloud accounts in SQLite (v380.db)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from v380.paths import DATA_DIR
from v380.store.db import connect
from v380.store.secretbox import decrypt, encrypt, load_or_create_key


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class EzvizAccountStore:
    def __init__(self, data_dir: Path | None = None):
        root = data_dir or DATA_DIR
        self._key = load_or_create_key(root / "master.key")
        self._conn = connect(root / "v380.db")
        self._lock = threading.Lock()
        self._import_legacy()

    def close(self) -> None:
        self._conn.close()

    def _import_legacy(self) -> None:
        """One-time: copy old single-file login into the accounts table."""
        from v380.client.ezviz_session import CREDS_FILE, TOKEN_FILE, load_account

        acc = load_account()
        if not acc:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM ezviz_accounts WHERE lower(username)=lower(?)",
                (acc[0],),
            ).fetchone()
        if row:
            return
        token = ""
        api = "apiisgp.ezvizlife.com"
        try:
            if TOKEN_FILE.is_file():
                data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                token = json.dumps(data)
                api = str(data.get("api_url") or api)
        except Exception:
            pass
        self.upsert(acc[0], acc[1], token_json=token, api_url=api)

    def list_public(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, api_url FROM ezviz_accounts ORDER BY id"
            ).fetchall()
        return [{"id": int(r["id"]), "username": r["username"], "api_url": r["api_url"]} for r in rows]

    def list_full(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM ezviz_accounts ORDER BY id").fetchall()
        return [self._full(r) for r in rows]

    def get(self, account_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM ezviz_accounts WHERE id=?", (account_id,)).fetchone()
        return self._full(row) if row else None

    def upsert(self, username: str, password: str, token_json: str = "", api_url: str = "") -> dict:
        username = (username or "").strip()
        if not username or not password:
            raise RuntimeError("EZVIZ account and password required")
        api = (api_url or "apiisgp.ezvizlife.com").strip()
        pw_blob = encrypt(self._key, password)
        tok_blob = encrypt(self._key, token_json or "") if token_json else None
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM ezviz_accounts WHERE lower(username)=lower(?)",
                (username,),
            ).fetchone()
            if existing:
                self._conn.execute(
                    """
                    UPDATE ezviz_accounts
                    SET username=?, password_enc=?, token_enc=COALESCE(?, token_enc),
                        api_url=?, updated_at=?
                    WHERE id=?
                    """,
                    (username, pw_blob, tok_blob, api, now, int(existing["id"])),
                )
                aid = int(existing["id"])
            else:
                cur = self._conn.execute(
                    """
                    INSERT INTO ezviz_accounts (username, password_enc, token_enc, api_url, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (username, pw_blob, tok_blob, api, now, now),
                )
                aid = int(cur.lastrowid)
            self._conn.commit()
        rec = self.get(aid)
        if rec is None:
            raise RuntimeError("Failed to save EZVIZ account")
        return rec

    def _full(self, row: sqlite3.Row) -> dict:
        password = decrypt(self._key, row["password_enc"])
        token = ""
        if row["token_enc"]:
            try:
                token = decrypt(self._key, row["token_enc"])
            except Exception:
                token = ""
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "password": password,
            "token_json": token,
            "api_url": row["api_url"] or "apiisgp.ezvizlife.com",
        }
