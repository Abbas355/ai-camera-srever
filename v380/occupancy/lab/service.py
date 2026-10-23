"""Detect Lab facade — experimental only; does not change Floor production detect."""

from __future__ import annotations

import base64
import csv
import io
import threading
from typing import Any

from v380.occupancy.lab.eval_metrics import evaluate as eval_gt
from v380.occupancy.lab.models import ModelRegistry, list_models
from v380.occupancy.lab.preprocess import apply_preprocess, decode_image, encode_jpeg
from v380.occupancy.lab.store import LabStore


def _decode_b64(raw: Any) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    s = str(raw).strip()
    if not s:
        return b""
    if "," in s and s.lower().startswith("data:"):
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return b""


def _counts(dets: list[dict[str, Any]]) -> dict[str, int]:
    c = {"person": 0, "chair": 0, "table": 0, "total": 0}
    for d in dets:
        lab = str(d.get("label") or "").lower()
        if lab in c:
            c[lab] += 1
        c["total"] += 1
    return c


def _annotate_occupancy(
    dets: list[dict[str, Any]],
    *,
    frame_w: int,
    frame_h: int,
    check_tables: bool = True,
    check_chairs: bool = True,
) -> dict[str, Any]:
    """Mark free/occupied separately for tables and/or chairs.

    Single-frame only — no temporal confirm. Experimental; does not touch Floor zones.
    """
    from v380.occupancy.associate import associate_occupancy
    from v380.occupancy.yolo_detector import BoxDet, match_chair_occupancy

    w = max(1, int(frame_w))
    h = max(1, int(frame_h))

    def _to_box(d: dict[str, Any]) -> BoxDet | None:
        xy = d.get("xyxy")
        if isinstance(xy, (list, tuple)) and len(xy) >= 4:
            x1, y1, x2, y2 = [float(v) for v in xy[:4]]
        else:
            n = d.get("norm")
            if not isinstance(n, (list, tuple)) or len(n) < 4:
                return None
            x1, y1, x2, y2 = float(n[0]) * w, float(n[1]) * h, float(n[2]) * w, float(n[3]) * h
        return BoxDet(
            label=str(d.get("label") or "obj"),
            conf=float(d.get("conf") or 0.0),
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
        )

    persons: list[BoxDet] = []
    chairs: list[BoxDet] = []
    tables: list[BoxDet] = []
    person_idx: list[int] = []
    chair_idx: list[int] = []
    table_idx: list[int] = []
    for i, d in enumerate(dets):
        lab = str(d.get("label") or "").lower()
        # Strip prior " · occupied" style labels from a previous pass
        if "person" in lab:
            kind = "person"
        elif "chair" in lab:
            kind = "chair"
        elif "table" in lab or "desk" in lab:
            kind = "table"
        else:
            continue
        b = _to_box(d)
        if b is None:
            continue
        if kind == "person":
            persons.append(b)
            person_idx.append(i)
        elif kind == "chair":
            chairs.append(b)
            chair_idx.append(i)
        else:
            tables.append(b)
            table_idx.append(i)

    if not check_tables and not check_chairs:
        return {"applied": False, "reason": "both checks off"}

    # Chairs: person↔chair only (clear occupied / empty).
    if check_chairs and chairs:
        match_chair_occupancy(chairs, persons, synthesize_occupied=False)
        for bi, c in zip(chair_idx, chairs):
            st = c.status or "empty"
            ui = "occupied" if st == "occupied" else "empty"
            dets[bi]["status"] = ui
            dets[bi]["kind"] = "chair"
            dets[bi]["label"] = f"chair · {ui}"

    # Tables: person→chair→table (occupied / free / uncertain).
    if check_tables and tables:
        associate_occupancy(tables, chairs, persons)
        for t in tables:
            if t.status in {"filled", "occupied"}:
                continue
            if t.status == "empty" and persons:
                tw = max(1.0, t.x2 - t.x1)
                th = max(1.0, t.y2 - t.y1)
                for p in persons:
                    near = (
                        t.x1 - tw * 0.2 <= p.cx <= t.x2 + tw * 0.2
                        and t.y1 - th * 0.15 <= p.seat_y <= t.y2 + th * 0.35
                    )
                    if near and (t.status or "empty") == "empty":
                        t.status = "uncertain"
                        break
        for bi, t in zip(table_idx, tables):
            st = t.status or "empty"
            if st in {"filled", "occupied"}:
                ui = "occupied"
            elif st == "uncertain":
                ui = "uncertain"
            else:
                ui = "free"
            dets[bi]["status"] = ui
            dets[bi]["sitters"] = int(t.sitter_count or 0)
            dets[bi]["kind"] = "table"
            dets[bi]["label"] = f"table · {ui}"

    occ_t = sum(
        1
        for t in tables
        if check_tables and (t.status or "") in {"filled", "occupied"}
    )
    unc_t = sum(1 for t in tables if check_tables and t.status == "uncertain")
    free_t = (len(tables) - occ_t - unc_t) if check_tables else 0
    occ_c = sum(1 for c in chairs if check_chairs and c.status == "occupied")
    empty_c = (len(chairs) - occ_c) if check_chairs else 0

    applied = (check_tables and bool(tables)) or (check_chairs and bool(chairs))
    reasons = []
    if check_tables and not tables:
        reasons.append("no tables detected")
    if check_chairs and not chairs:
        reasons.append("no chairs detected")

    return {
        "applied": applied,
        "check_tables": check_tables,
        "check_chairs": check_chairs,
        "method": "person-chair (chairs) + person-chair-table (tables)",
        "temporal": False,
        "reason": "; ".join(reasons) if reasons and not applied else None,
        "note": (
            "Separate checks: chair empty/occupied (person on chair); "
            "table free/occupied/uncertain (person-chair-table). Single frame only."
        ),
        "tables_occupied": occ_t if check_tables else None,
        "tables_free": free_t if check_tables else None,
        "tables_uncertain": unc_t if check_tables else None,
        "chairs_occupied": occ_c if check_chairs else None,
        "chairs_empty": empty_c if check_chairs else None,
        "people": len(persons),
    }


class LabService:
    _instance: LabService | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.store = LabStore.instance()
        self.registry = ModelRegistry.instance()

    @classmethod
    def instance(cls) -> LabService:
        with cls._lock:
            if cls._instance is None:
                cls._instance = LabService()
            return cls._instance

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "feature": "detect_lab",
            "models": list_models(),
            "defaults": {
                "model": "yolov8n",
                "conf": 0.25,
                "nms_iou": 0.45,
                "imgsz": 640,
                "eval_iou": 0.50,
                "classes": {"person": True, "chair": True, "table": True},
                "world_prompts": ["office desk", "computer desk", "work desk"],
            },
            "configs": self.store.list_configs(),
            "results": self.store.list_results()[:20],
            "ground_truth": self.store.list_ground_truth()[:20],
            "notes": [
                "Detect Lab is experimental and does not change Floor production detection.",
                "Class-specific confidence is a post-filter on model scores, not training.",
                "NMS IoU suppresses duplicate boxes; it is not person↔table association.",
                "Tracking / occupancy experiments require a sequence of frames, not one still.",
            ],
        }

    def _parse_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model") or "yolov8n").strip().lower()
        conf = float(body.get("conf") if body.get("conf") is not None else 0.25)
        nms = float(body.get("nms_iou") if body.get("nms_iou") is not None else 0.45)
        imgsz = int(body.get("imgsz") if body.get("imgsz") is not None else 640)
        conf = max(0.01, min(0.95, conf))
        nms = max(0.10, min(0.90, nms))
        if imgsz not in {320, 640, 960, 1280}:
            imgsz = 640
        classes = body.get("classes") or {}
        want_person = bool(classes.get("person", body.get("persons", True)))
        want_chair = bool(classes.get("chair", body.get("chairs", True)))
        want_table = bool(classes.get("table", body.get("tables", True)))
        prompts = body.get("world_prompts") or body.get("prompts") or [
            "office desk",
            "computer desk",
            "work desk",
        ]
        if isinstance(prompts, str):
            prompts = [p.strip() for p in prompts.split(",") if p.strip()]
        class_conf = None
        if body.get("class_conf_enabled"):
            raw = body.get("class_conf") or {}
            class_conf = {}
            for k in ("person", "chair", "table"):
                if k in raw and raw[k] is not None:
                    class_conf[k] = max(0.01, min(0.95, float(raw[k])))
        prep = body.get("preprocess") or {}
        return {
            "model": model,
            "conf": conf,
            "nms_iou": nms,
            "imgsz": imgsz,
            "want_person": want_person,
            "want_chair": want_chair,
            "want_table": want_table,
            "prompts": list(prompts),
            "class_conf_enabled": bool(body.get("class_conf_enabled")),
            "class_conf": class_conf,
            "preprocess": {
                "enabled": bool(prep.get("enabled", False)),
                "brightness": float(prep.get("brightness") or 0.0),
                "contrast": float(prep.get("contrast") if prep.get("contrast") is not None else 1.0),
                "crop_norm": prep.get("crop_norm"),
            },
        }

    def detect(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = _decode_b64(body.get("image_base64") or body.get("photo_base64"))
        if not raw:
            return {
                "ok": False,
                "error": "Provide image_base64 or source=camera with camera_id",
            }
        settings = self._parse_settings(body)
        # Prefer separate flags; fall back to legacy occupancy_check.
        legacy = bool(body.get("occupancy_check", False))
        check_tables = bool(
            body["table_occupancy"]
            if "table_occupancy" in body
            else legacy
        )
        check_chairs = bool(
            body["chair_occupancy"]
            if "chair_occupancy" in body
            else legacy
        )
        any_occ = check_tables or check_chairs
        source = str(body.get("source") or "image").strip().lower()
        rgb, err = decode_image(raw)
        if rgb is None:
            return {"ok": False, "error": err or "bad image"}
        prep = settings["preprocess"]
        processed, prep_meta = apply_preprocess(
            rgb,
            enabled=prep["enabled"],
            brightness=prep["brightness"],
            contrast=prep["contrast"],
            crop_norm=prep.get("crop_norm"),
        )
        try:
            model = self.registry.get(settings["model"])
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Occupancy needs people; chairs needed for chair-check or table bridge.
        want_person = settings["want_person"] or any_occ
        want_chair = settings["want_chair"] or check_chairs or check_tables
        want_table = settings["want_table"] or check_tables

        dets, infer_ms, warn = model.predict(
            processed,
            conf=settings["conf"],
            iou=settings["nms_iou"],
            imgsz=settings["imgsz"],
            want_person=want_person and settings["model"] != "yolo-world",
            want_chair=want_chair and settings["model"] != "yolo-world",
            want_table=want_table,
            prompts=settings["prompts"],
            class_conf=settings["class_conf"] if settings["class_conf_enabled"] else None,
        )
        # YOLO-World has no person/chair — pull them from yolov8n for occupancy.
        if any_occ and settings["model"] == "yolo-world":
            try:
                coco = self.registry.get("yolov8n")
                extra, extra_ms, extra_warn = coco.predict(
                    processed,
                    conf=settings["conf"],
                    iou=settings["nms_iou"],
                    imgsz=settings["imgsz"],
                    want_person=True,
                    want_chair=True,
                    want_table=False,
                    prompts=None,
                    class_conf=settings["class_conf"]
                    if settings["class_conf_enabled"]
                    else None,
                )
                infer_ms += extra_ms
                dets = list(dets) + list(extra)
                if extra_warn:
                    warn = f"{warn}; {extra_warn}" if warn else extra_warn
                warn = (
                    f"{warn}; " if warn else ""
                ) + "Occupancy: persons/chairs from YOLOv8n; tables from YOLO-World"
            except Exception as exc:
                warn = f"{warn}; occupancy coco assist failed: {exc}" if warn else str(exc)

        ok_av, av_msg = model.available()
        if not ok_av and not dets:
            return {"ok": False, "error": av_msg, "settings": settings}

        occupancy: dict[str, Any] | None = None
        if any_occ:
            occupancy = _annotate_occupancy(
                dets,
                frame_w=int(processed.shape[1]),
                frame_h=int(processed.shape[0]),
                check_tables=check_tables,
                check_chairs=check_chairs,
            )

        # Always return the frame used so Live-cam clients can display it.
        source_b64 = base64.b64encode(encode_jpeg(rgb) or raw).decode("ascii")
        processed_b64 = None
        if prep_meta.get("enabled"):
            jpg = encode_jpeg(processed)
            if jpg:
                processed_b64 = base64.b64encode(jpg).decode("ascii")

        return {
            "ok": True,
            "mode": "detect",
            "source": source if source in {"camera", "live", "cam", "image"} else "image",
            "camera_id": body.get("camera_id"),
            "settings": settings,
            "table_occupancy": check_tables,
            "chair_occupancy": check_chairs,
            "occupancy": occupancy,
            "detections": dets,
            "counts": _counts(dets),
            "timing": {
                "infer_ms": round(infer_ms, 1),
                "preprocess_applied": bool(prep_meta.get("enabled")),
            },
            "preprocess": prep_meta,
            "source_image_base64": source_b64,
            "processed_image_base64": processed_b64,
            "frame": {"w": int(processed.shape[1]), "h": int(processed.shape[0])},
            "warning": warn,
            "model_label": model.spec["label"],
        }

    def compare(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run two models on the same decoded/preprocessed image and settings."""
        raw = _decode_b64(body.get("image_base64") or body.get("photo_base64"))
        if not raw:
            return {"ok": False, "error": "Provide image_base64"}
        model_a = str(body.get("model_a") or body.get("model") or "yolov8n")
        model_b = str(body.get("model_b") or "yolov8s")
        if model_a == model_b:
            return {"ok": False, "error": "Choose two different models to compare"}
        base = dict(body)
        # Shared conf / nms / imgsz / preprocess / class toggles
        shared = self._parse_settings(base)
        rgb, err = decode_image(raw)
        if rgb is None:
            return {"ok": False, "error": err or "bad image"}
        prep = shared["preprocess"]
        processed, prep_meta = apply_preprocess(
            rgb,
            enabled=prep["enabled"],
            brightness=prep["brightness"],
            contrast=prep["contrast"],
            crop_norm=prep.get("crop_norm"),
        )

        results = []
        for mid in (model_a, model_b):
            try:
                model = self.registry.get(mid)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            dets, infer_ms, warn = model.predict(
                processed,
                conf=shared["conf"],
                iou=shared["nms_iou"],
                imgsz=shared["imgsz"],
                want_person=shared["want_person"],
                want_chair=shared["want_chair"],
                want_table=shared["want_table"],
                prompts=shared["prompts"],
                class_conf=shared["class_conf"] if shared["class_conf_enabled"] else None,
            )
            results.append(
                {
                    "model": mid,
                    "model_label": model.spec["label"],
                    "detections": dets,
                    "counts": _counts(dets),
                    "infer_ms": round(infer_ms, 1),
                    "warning": warn,
                    "settings": {
                        **shared,
                        "model": mid,
                    },
                }
            )

        return {
            "ok": True,
            "mode": "compare",
            "shared_settings": {
                "conf": shared["conf"],
                "nms_iou": shared["nms_iou"],
                "imgsz": shared["imgsz"],
                "preprocess": prep_meta,
            },
            "note": "Both sides used the same source image, imgsz, conf, and NMS IoU.",
            "a": results[0],
            "b": results[1],
        }

    def evaluate(self, body: dict[str, Any]) -> dict[str, Any]:
        preds = body.get("predictions") or body.get("detections") or []
        gts = body.get("ground_truth") or body.get("annotations") or []
        if not isinstance(preds, list) or not isinstance(gts, list):
            return {"ok": False, "error": "predictions and ground_truth must be lists"}
        eval_iou = float(body.get("eval_iou") if body.get("eval_iou") is not None else 0.50)
        metrics = eval_gt(preds, gts, eval_iou=eval_iou)
        return metrics

    def occupancy_experiment(self, body: dict[str, Any]) -> dict[str, Any]:
        """Experimental person→chair→table on a frame sequence (not production Floor)."""
        frames_b64 = body.get("frames_base64") or body.get("images_base64") or []
        if isinstance(frames_b64, str):
            frames_b64 = [frames_b64]
        if not isinstance(frames_b64, list) or len(frames_b64) < 2:
            return {
                "ok": False,
                "error": (
                    "Occupancy experiment needs a sequence of at least 2 frames "
                    "(frames_base64[]). A single still cannot provide temporal tracking."
                ),
                "temporal_required": True,
            }

        min_occ_frames = max(1, int(body.get("min_occupied_frames") or 2))
        free_confirm = max(1, int(body.get("min_free_frames") or 3))
        assoc_dist = float(body.get("assoc_distance_norm") or 0.12)
        assoc_dist = max(0.02, min(0.5, assoc_dist))
        settings = self._parse_settings(body)
        # Force association classes
        settings["want_person"] = True
        settings["want_chair"] = True
        settings["want_table"] = True

        from v380.occupancy.associate import associate_occupancy
        from v380.occupancy.yolo_detector import BoxDet

        try:
            model = self.registry.get(settings["model"] if settings["model"] != "yolo-world" else "yolov8n")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Simple temporal: require table filled for min_occ_frames consecutive
        table_hist: dict[int, list[str]] = {}
        sequence_out: list[dict[str, Any]] = []
        for fi, b64 in enumerate(frames_b64[:30]):  # cap
            raw = _decode_b64(b64)
            rgb, err = decode_image(raw)
            if rgb is None:
                sequence_out.append({"frame": fi, "error": err})
                continue
            dets, infer_ms, warn = model.predict(
                rgb,
                conf=settings["conf"],
                iou=settings["nms_iou"],
                imgsz=settings["imgsz"],
                want_person=True,
                want_chair=True,
                want_table=True,
                prompts=settings["prompts"],
                class_conf=None,
            )
            h, w = int(rgb.shape[0]), int(rgb.shape[1])
            persons = [
                BoxDet("person", float(d["conf"]), *d["xyxy"])
                for d in dets
                if d.get("label") == "person"
            ]
            chairs = [
                BoxDet("chair", float(d["conf"]), *d["xyxy"])
                for d in dets
                if d.get("label") == "chair"
            ]
            tables = [
                BoxDet("table", float(d["conf"]), *d["xyxy"])
                for d in dets
                if d.get("label") == "table"
            ]
            # Scale assoc by tweaking seat pad via distance: filter distant sitters after
            associate_occupancy(tables, chairs, persons)
            # Soften: if person center far from table, mark uncertain
            for ti, t in enumerate(tables):
                if t.status == "filled" and t.sitter_count > 0:
                    # keep
                    pass
                elif any(
                    ((p.cx - t.cx) / max(1.0, w)) ** 2 + ((p.cy - t.cy) / max(1.0, h)) ** 2
                    < assoc_dist**2
                    for p in persons
                ):
                    if t.status != "filled":
                        t.status = "uncertain"
                hist = table_hist.setdefault(ti, [])
                hist.append(t.status or "empty")
                if len(hist) > max(min_occ_frames, free_confirm) + 2:
                    hist.pop(0)
                # Temporal confirm
                recent = hist[-min_occ_frames:]
                if len(recent) >= min_occ_frames and all(s == "filled" for s in recent):
                    t.status = "filled"
                elif len(hist) >= free_confirm and all(
                    s in {"empty", None} for s in hist[-free_confirm:]
                ):
                    t.status = "empty"

            sequence_out.append(
                {
                    "frame": fi,
                    "infer_ms": round(infer_ms, 1),
                    "warning": warn,
                    "persons": [
                        {
                            "id": i,
                            "norm": [
                                p.x1 / w,
                                p.y1 / h,
                                p.x2 / w,
                                p.y2 / h,
                            ],
                            "conf": p.conf,
                        }
                        for i, p in enumerate(persons)
                    ],
                    "chairs": [
                        {
                            "id": i,
                            "norm": [c.x1 / w, c.y1 / h, c.x2 / w, c.y2 / h],
                            "conf": c.conf,
                            "status": c.status,
                        }
                        for i, c in enumerate(chairs)
                    ],
                    "tables": [
                        {
                            "id": i,
                            "norm": [t.x1 / w, t.y1 / h, t.x2 / w, t.y2 / h],
                            "conf": t.conf,
                            "status": t.status or "uncertain",
                            "sitters": t.sitter_count,
                        }
                        for i, t in enumerate(tables)
                    ],
                    "tracker": "iou-bytetrack-style (experimental; per-call reset without camera_id)",
                }
            )

        return {
            "ok": True,
            "mode": "occupancy_experiment",
            "experimental": True,
            "production_unchanged": True,
            "params": {
                "min_occupied_frames": min_occ_frames,
                "min_free_frames": free_confirm,
                "assoc_distance_norm": assoc_dist,
                "tracker": "iou-bytetrack-style",
            },
            "frames": sequence_out,
            "note": "Experimental only. Does not modify Floor zones or watcher.",
        }

    def export_csv(self, result_id: str) -> tuple[str | None, str | None]:
        doc = self.store.get_result(result_id)
        if not doc:
            return None, "result not found"
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["section", "key", "value"])
        w.writerow(["meta", "id", doc.get("id")])
        w.writerow(["meta", "name", doc.get("name")])
        w.writerow(["meta", "mode", doc.get("mode")])
        counts = (doc.get("counts") or {})
        for k, v in counts.items():
            w.writerow(["counts", k, v])
        timing = doc.get("timing") or {}
        for k, v in timing.items():
            w.writerow(["timing", k, v])
        for i, d in enumerate(doc.get("detections") or []):
            w.writerow(
                [
                    "detection",
                    i,
                    f"{d.get('label')} conf={d.get('conf')} norm={d.get('norm')}",
                ]
            )
        return buf.getvalue(), None


_lab: LabService | None = None
_lab_lock = threading.Lock()


def get_lab_service() -> LabService:
    global _lab
    with _lab_lock:
        if _lab is None:
            _lab = LabService.instance()
        return _lab
