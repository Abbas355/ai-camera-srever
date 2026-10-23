"""Persist Detect Lab configs and experiment outputs (JSON under data/detect_lab/)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from v380.paths import DATA_DIR

LAB_DIR = DATA_DIR / "detect_lab"
CONFIGS_DIR = LAB_DIR / "configs"
RESULTS_DIR = LAB_DIR / "results"
GT_DIR = LAB_DIR / "ground_truth"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class LabStore:
    _instance: LabStore | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        for d in (LAB_DIR, CONFIGS_DIR, RESULTS_DIR, GT_DIR):
            d.mkdir(parents=True, exist_ok=True)
        self._io = threading.Lock()

    @classmethod
    def instance(cls) -> LabStore:
        with cls._lock:
            if cls._instance is None:
                cls._instance = LabStore()
            return cls._instance

    def save_config(self, name: str, settings: dict[str, Any]) -> dict[str, Any]:
        clean = (name or "").strip() or f"config-{uuid.uuid4().hex[:8]}"
        sid = uuid.uuid4().hex[:12]
        doc = {
            "id": sid,
            "kind": "config",
            "name": clean,
            "created_at": _now(),
            "settings": settings,
        }
        path = CONFIGS_DIR / f"{sid}.json"
        with self._io:
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return doc

    def list_configs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._io:
            for p in sorted(CONFIGS_DIR.glob("*.json")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    out.append(
                        {
                            "id": doc.get("id") or p.stem,
                            "name": doc.get("name"),
                            "created_at": doc.get("created_at"),
                            "kind": "config",
                        }
                    )
                except Exception:
                    continue
        return out

    def get_config(self, config_id: str) -> dict[str, Any] | None:
        path = CONFIGS_DIR / f"{config_id}.json"
        if not path.is_file():
            return None
        with self._io:
            return json.loads(path.read_text(encoding="utf-8"))

    def delete_config(self, config_id: str) -> bool:
        path = CONFIGS_DIR / f"{config_id}.json"
        with self._io:
            if path.is_file():
                path.unlink()
                return True
        return False

    def save_result(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean = (name or "").strip() or f"result-{uuid.uuid4().hex[:8]}"
        rid = uuid.uuid4().hex[:12]
        doc = {
            "id": rid,
            "kind": "result",
            "name": clean,
            "created_at": _now(),
            **payload,
        }
        path = RESULTS_DIR / f"{rid}.json"
        with self._io:
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return {"id": rid, "name": clean, "kind": "result", "created_at": doc["created_at"]}

    def list_results(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._io:
            for p in sorted(RESULTS_DIR.glob("*.json"), reverse=True):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    out.append(
                        {
                            "id": doc.get("id") or p.stem,
                            "name": doc.get("name"),
                            "created_at": doc.get("created_at"),
                            "kind": "result",
                            "mode": doc.get("mode"),
                        }
                    )
                except Exception:
                    continue
        return out

    def get_result(self, result_id: str) -> dict[str, Any] | None:
        path = RESULTS_DIR / f"{result_id}.json"
        if not path.is_file():
            return None
        with self._io:
            return json.loads(path.read_text(encoding="utf-8"))

    def save_ground_truth(
        self, name: str, annotations: list[dict[str, Any]], meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Save GT boxes. Format documented in meta.format."""
        clean = (name or "").strip() or f"gt-{uuid.uuid4().hex[:8]}"
        gid = uuid.uuid4().hex[:12]
        doc = {
            "id": gid,
            "kind": "ground_truth",
            "name": clean,
            "created_at": _now(),
            "format": {
                "boxes": "list of {label: person|chair|table, norm: [x1,y1,x2,y2] in 0..1}",
                "coords": "normalized image coordinates, independent of model predictions",
            },
            "annotations": annotations,
            "meta": meta or {},
        }
        path = GT_DIR / f"{gid}.json"
        with self._io:
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return doc

    def list_ground_truth(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._io:
            for p in sorted(GT_DIR.glob("*.json"), reverse=True):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    anns = doc.get("annotations") or []
                    out.append(
                        {
                            "id": doc.get("id") or p.stem,
                            "name": doc.get("name"),
                            "created_at": doc.get("created_at"),
                            "count": len(anns) if isinstance(anns, list) else 0,
                            "kind": "ground_truth",
                        }
                    )
                except Exception:
                    continue
        return out

    def get_ground_truth(self, gt_id: str) -> dict[str, Any] | None:
        path = GT_DIR / f"{gt_id}.json"
        if not path.is_file():
            return None
        with self._io:
            return json.loads(path.read_text(encoding="utf-8"))
