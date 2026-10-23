"""Persist restaurant tables under data/tables.json."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from v380.paths import DATA_DIR
from v380.tables.types import Table, TableAim

STORE_PATH = DATA_DIR / "tables.json"


class TableStore:
    def __init__(self, path: Path | None = None):
        self.path = path or STORE_PATH
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[Table]:
        with self._lock:
            return list(self._load().values())

    def list_for_camera(self, camera_id: int) -> list[Table]:
        cid = int(camera_id)
        return [t for t in self.list() if t.camera_id == cid]

    def get(self, table_id: str) -> Table | None:
        with self._lock:
            return self._load().get(str(table_id))

    def add(self, name: str, *, camera_id: int | None = None, x: float = 0.5, y: float = 0.5) -> Table:
        table = Table(
            id=uuid.uuid4().hex[:10],
            name=(name or "Table").strip() or "Table",
            camera_id=int(camera_id) if camera_id is not None else None,
            x=max(0.05, min(0.95, float(x))),
            y=max(0.05, min(0.95, float(y))),
        )
        with self._lock:
            data = self._load()
            data[table.id] = table
            self._save(data)
        return table

    def update(self, table: Table) -> Table:
        with self._lock:
            data = self._load()
            data[table.id] = table
            self._save(data)
        return table

    def delete(self, table_id: str) -> bool:
        with self._lock:
            data = self._load()
            if str(table_id) not in data:
                return False
            del data[str(table_id)]
            self._save(data)
        return True

    def set_aim(self, table_id: str, aim: TableAim) -> Table | None:
        with self._lock:
            data = self._load()
            t = data.get(str(table_id))
            if t is None:
                return None
            t.aim = aim
            data[t.id] = t
            self._save(data)
            return t

    def next_preset_slot(self, camera_id: int, *, exclude_table_id: str | None = None) -> int:
        """Allocate next free ONVIF preset slot (1..) for this camera."""
        used: set[int] = set()
        for t in self.list_for_camera(camera_id):
            if exclude_table_id and t.id == exclude_table_id:
                continue
            if t.aim.preset_slot is not None:
                used.add(int(t.aim.preset_slot))
        slot = 1
        while slot in used:
            slot += 1
        return slot

    def _load(self) -> dict[str, Table]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        items = raw.get("tables") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return {}
        out: dict[str, Table] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                t = Table.from_dict(item)
            except Exception:
                continue
            if t.id:
                out[t.id] = t
        return out

    def _save(self, data: dict[str, Table]) -> None:
        payload = {
            "version": 1,
            "tables": [t.to_dict() for t in sorted(data.values(), key=lambda x: (x.name.lower(), x.id))],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)
