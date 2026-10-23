"""Lazy model registry for Detect Lab. Does not replace production OccupancyYolo."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from v380.paths import ROOT

_MODELS_DIR = ROOT / "v380" / "ai" / "models"  # ROOT = backend/

# COCO ids
_PERSON = 0
_CHAIR = 56
_DINING_TABLE = 60

MODEL_SPECS: dict[str, dict[str, Any]] = {
    "yolov8n": {
        "label": "YOLOv8n",
        "weights": "yolov8n.pt",
        "kind": "coco",
        "classes": ["person", "chair", "table"],
        "supports_prompts": False,
        "note": "Baseline COCO person / chair / dining table",
    },
    "yolov8s": {
        "label": "YOLOv8s",
        "weights": "yolov8s.pt",
        "kind": "coco",
        "classes": ["person", "chair", "table"],
        "supports_prompts": False,
        "note": "Larger COCO model for comparison",
    },
    "yolov8m": {
        "label": "YOLOv8m",
        "weights": "yolov8m.pt",
        "kind": "coco",
        "classes": ["person", "chair", "table"],
        "supports_prompts": False,
        "note": "Medium COCO model (slower, optional comparison)",
    },
    "yolo-world": {
        "label": "YOLO-World",
        "weights": "yolov8s-world.pt",
        "kind": "world",
        "classes": ["table"],
        "supports_prompts": True,
        "note": "Open-vocabulary; use text prompts for desks/tables (not COCO person/chair)",
    },
}


def list_models() -> list[dict[str, Any]]:
    out = []
    for mid, spec in MODEL_SPECS.items():
        wname = str(spec["weights"])
        local = _MODELS_DIR / wname
        out.append(
            {
                "id": mid,
                "label": spec["label"],
                "kind": spec["kind"],
                "classes": list(spec["classes"]),
                "supports_prompts": bool(spec["supports_prompts"]),
                "note": spec["note"],
                "weights": wname,
                "weights_present": local.is_file(),
                "weights_path": str(local),
            }
        )
    return out


class _LoadedModel:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.spec = MODEL_SPECS[model_id]
        self._model: Any = None
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._prompts: list[str] = []

    def available(self) -> tuple[bool, str]:
        try:
            import ultralytics  # noqa: F401
        except Exception as exc:
            return False, f"ultralytics missing: {exc}"
        if self._load_error and self._model is None:
            return False, self._load_error
        return True, "ok"

    def ensure_loaded(self, prompts: list[str] | None = None) -> bool:
        with self._lock:
            if self._model is not None:
                if self.spec["kind"] == "world" and prompts:
                    want = [p.strip() for p in prompts if str(p).strip()]
                    if want and want != self._prompts:
                        try:
                            self._model.set_classes(want)
                            self._prompts = want
                        except Exception as exc:
                            self._load_error = f"set_classes failed: {exc}"
                            return False
                return True
            try:
                import shutil

                from ultralytics import YOLO

                wname = str(self.spec["weights"])
                local = _MODELS_DIR / wname
                _MODELS_DIR.mkdir(parents=True, exist_ok=True)
                path = str(local if local.is_file() else wname)
                if self.spec["kind"] == "world":
                    try:
                        from ultralytics import YOLOWorld

                        self._model = YOLOWorld(path)
                    except Exception:
                        self._model = YOLO(path)
                    want = [p.strip() for p in (prompts or []) if str(p).strip()]
                    if not want:
                        want = ["office desk", "computer desk", "work desk"]
                    self._model.set_classes(want)
                    self._prompts = want
                else:
                    self._model = YOLO(path)
                try:
                    if not local.is_file():
                        src = getattr(self._model, "ckpt_path", None) or getattr(
                            self._model, "model_path", None
                        )
                        if src and Path(str(src)).is_file():
                            shutil.copy2(str(src), str(local))
                except Exception:
                    pass
                self._load_error = None
                return True
            except Exception as exc:
                self._load_error = str(exc)
                self._model = None
                return False

    def predict(
        self,
        rgb: np.ndarray,
        *,
        conf: float,
        iou: float,
        imgsz: int,
        want_person: bool,
        want_chair: bool,
        want_table: bool,
        prompts: list[str] | None,
        class_conf: dict[str, float] | None,
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        """Run inference → list of {label,conf,norm,xyxy} + infer_ms + error."""
        if not self.ensure_loaded(prompts):
            return [], 0.0, self._load_error or "model not loaded"

        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        if h < 2 or w < 2:
            return [], 0.0, "image too small"

        run_conf = max(0.01, min(0.95, float(conf)))
        run_iou = max(0.10, min(0.90, float(iou)))
        run_imgsz = int(imgsz) if int(imgsz) in {320, 640, 960, 1280} else 640

        unsupported: list[str] = []
        if self.spec["kind"] == "world":
            if want_person:
                unsupported.append("person")
            if want_chair:
                unsupported.append("chair")
            if not want_table:
                return [], 0.0, "YOLO-World lab mode detects via prompts (enable Table)"
        else:
            if want_table and "table" not in self.spec["classes"]:
                unsupported.append("table")

        t0 = time.perf_counter()
        try:
            with self._lock:
                results = self._model.predict(
                    source=rgb,
                    conf=run_conf,
                    iou=run_iou,
                    imgsz=run_imgsz,
                    verbose=False,
                    max_det=100,
                )
        except Exception as exc:
            return [], (time.perf_counter() - t0) * 1000.0, str(exc)
        ms = (time.perf_counter() - t0) * 1000.0

        boxes_out: list[dict[str, Any]] = []
        if not results:
            return boxes_out, ms, None
        r0 = results[0]
        boxes = getattr(r0, "boxes", None)
        names = getattr(r0, "names", None) or {}
        if boxes is None or len(boxes) == 0:
            return boxes_out, ms, None

        xyxy = (
            boxes.xyxy.cpu().numpy()
            if hasattr(boxes.xyxy, "cpu")
            else np.asarray(boxes.xyxy)
        )
        confs = (
            boxes.conf.cpu().numpy()
            if hasattr(boxes.conf, "cpu")
            else np.asarray(boxes.conf)
        )
        clss = (
            boxes.cls.cpu().numpy()
            if hasattr(boxes.cls, "cpu")
            else np.asarray(boxes.cls)
        )

        cc = class_conf or {}
        for i in range(len(xyxy)):
            cid = int(clss[i])
            conf_v = float(confs[i])
            x1, y1, x2, y2 = [float(v) for v in xyxy[i]]
            label = self._map_label(cid, names)
            if label == "person" and not want_person:
                continue
            if label == "chair" and not want_chair:
                continue
            if label == "table" and not want_table:
                continue
            if label not in {"person", "chair", "table"}:
                continue
            # Optional post-filter class confidence (does not change model logits).
            min_c = cc.get(label)
            if min_c is not None and conf_v < float(min_c):
                continue
            boxes_out.append(
                {
                    "label": label,
                    "kind": label,
                    "conf": round(conf_v, 4),
                    "xyxy": [x1, y1, x2, y2],
                    "norm": [
                        max(0.0, min(1.0, x1 / w)),
                        max(0.0, min(1.0, y1 / h)),
                        max(0.0, min(1.0, x2 / w)),
                        max(0.0, min(1.0, y2 / h)),
                    ],
                }
            )

        warn = None
        if unsupported:
            warn = (
                f"Unsupported for {self.spec['label']}: {', '.join(unsupported)} "
                "(ignored; not filtered as detections)"
            )
        return boxes_out, ms, warn

    def _map_label(self, cid: int, names: Any) -> str:
        if self.spec["kind"] == "world":
            return "table"
        if cid == _PERSON:
            return "person"
        if cid == _CHAIR:
            return "chair"
        if cid == _DINING_TABLE:
            return "table"
        # Fallback by name string
        try:
            name = str(names.get(cid, "") if hasattr(names, "get") else names[cid]).lower()
        except Exception:
            name = ""
        if "person" in name:
            return "person"
        if "chair" in name:
            return "chair"
        if "table" in name or "desk" in name:
            return "table"
        return name or f"class_{cid}"


class ModelRegistry:
    _instance: ModelRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._models: dict[str, _LoadedModel] = {}

    @classmethod
    def instance(cls) -> ModelRegistry:
        with cls._lock:
            if cls._instance is None:
                cls._instance = ModelRegistry()
            return cls._instance

    def get(self, model_id: str) -> _LoadedModel:
        mid = (model_id or "yolov8n").strip().lower()
        if mid not in MODEL_SPECS:
            raise ValueError(f"Unknown model '{model_id}'. Choose: {', '.join(MODEL_SPECS)}")
        if mid not in self._models:
            self._models[mid] = _LoadedModel(mid)
        return self._models[mid]
