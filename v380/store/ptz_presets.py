"""Named PTZ presets per camera (firmware slot + relative hold-time fallback)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from v380.paths import DATA_DIR

PRESETS_FILE = DATA_DIR / "ptz_presets.json"
_LOCK = threading.Lock()
MAX_SLOTS = 6


def _load() -> dict:
    if not PRESETS_FILE.is_file():
        return {}
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PRESETS_FILE)


def _normalize_items(items: list) -> list[dict]:
    """Migrate legacy id-based rows and keep at most one row per slot 1–6."""
    by_slot: dict[int, dict] = {}
    legacy_i = 1
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        try:
            slot = int(raw.get("slot") or 0)
        except (TypeError, ValueError):
            slot = 0
        if slot < 1 or slot > MAX_SLOTS:
            while legacy_i in by_slot and legacy_i <= MAX_SLOTS:
                legacy_i += 1
            if legacy_i > MAX_SLOTS:
                continue
            slot = legacy_i
            legacy_i += 1
        row = {
            "slot": slot,
            "id": f"slot{slot}",
            "name": str(raw.get("name") or f"Preset {slot}")[:40],
            "pan": round(float(raw.get("pan") or 0), 3),
            "tilt": round(float(raw.get("tilt") or 0), 3),
            "zoom": round(float(raw.get("zoom") or 0), 3),
            "saved_at": str(raw.get("saved_at") or ""),
            "firmware": bool(raw.get("firmware")),
        }
        by_slot[slot] = row
    return [by_slot[s] for s in sorted(by_slot)]


def list_presets(device_id: str) -> list[dict]:
    with _LOCK:
        rows = _normalize_items(list(_load().get(str(device_id), []) or []))
    return rows


def get_slot(device_id: str, slot: int) -> dict | None:
    slot = int(slot)
    for row in list_presets(device_id):
        if int(row.get("slot") or 0) == slot:
            return row
    return None


def save_slot(
    device_id: str,
    slot: int,
    name: str,
    pan: float,
    tilt: float,
    zoom: float,
    *,
    firmware: bool = False,
) -> dict:
    slot = max(1, min(MAX_SLOTS, int(slot)))
    name = (name or "").strip() or f"Preset {slot}"
    row = {
        "slot": slot,
        "id": f"slot{slot}",
        "name": name[:40],
        "pan": round(float(pan), 3),
        "tilt": round(float(tilt), 3),
        "zoom": round(float(zoom), 3),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "firmware": bool(firmware),
    }
    with _LOCK:
        data = _load()
        key = str(device_id)
        items = _normalize_items(list(data.get(key, []) or []))
        items = [r for r in items if int(r.get("slot") or 0) != slot]
        items.append(row)
        items.sort(key=lambda r: int(r.get("slot") or 0))
        data[key] = items
        _save(data)
    return row


def delete_preset(device_id: str, preset_id: str) -> bool:
    """Delete by slot id (`slot3`) or numeric slot string."""
    raw = str(preset_id or "")
    slot = 0
    if raw.startswith("slot"):
        try:
            slot = int(raw[4:])
        except ValueError:
            slot = 0
    else:
        try:
            slot = int(raw)
        except ValueError:
            slot = 0
    if slot < 1:
        return False
    with _LOCK:
        data = _load()
        key = str(device_id)
        items = _normalize_items(list(data.get(key, []) or []))
        nxt = [r for r in items if int(r.get("slot") or 0) != slot]
        if len(nxt) == len(items):
            return False
        data[key] = nxt
        _save(data)
    return True


# Back-compat for older callers
def add_preset(device_id: str, name: str, pan: float, tilt: float, zoom: float) -> dict:
    used = {int(r.get("slot") or 0) for r in list_presets(device_id)}
    slot = next((i for i in range(1, MAX_SLOTS + 1) if i not in used), MAX_SLOTS)
    return save_slot(device_id, slot, name, pan, tilt, zoom)
