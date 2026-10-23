"""YOLO detectors for Floor occupancy (isolated).

- YOLOv8n (COCO): person + chair
- YOLO-World (open-vocab): desk / office table / dining table
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# COCO class ids
_PERSON = 0
_CHAIR = 56
_DINING_TABLE = 60

_LABELS = {
    _PERSON: "person",
    _CHAIR: "chair",
    _DINING_TABLE: "dining_table",
}


@dataclass
class BoxDet:
    label: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float
    # Set by match_* for chair/table boxes.
    status: str | None = None  # chair: occupied|empty · table: filled|empty|uncertain
    seated_person_conf: float | None = None
    sitter_count: int = 0  # persons matched to this table (or 0/1 for chair)
    track_id: int | None = None  # stable ID across frames (ByteTrack-style)

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0

    @property
    def foot_x(self) -> float:
        return self.cx

    @property
    def foot_y(self) -> float:
        # Bottom-center — better for “standing/sitting at table”.
        return self.y1 + (self.y2 - self.y1) * 0.88

    @property
    def seat_x(self) -> float:
        return self.cx

    @property
    def seat_y(self) -> float:
        # Lower torso / hips — better for “sitting on chair”.
        return self.y1 + (self.y2 - self.y1) * 0.72

    def as_norm_rect(self, w: float, h: float) -> list[list[float]]:
        """4-corner polygon in normalized 0..1 coords."""
        if w <= 1 or h <= 1:
            return []
        x1 = max(0.0, min(1.0, self.x1 / w))
        y1 = max(0.0, min(1.0, self.y1 / h))
        x2 = max(0.0, min(1.0, self.x2 / w))
        y2 = max(0.0, min(1.0, self.y2 / h))
        # Modest pad: side chairs count, but not the next desk over.
        pad_x = (x2 - x1) * 0.16
        pad_top = (y2 - y1) * 0.08
        pad_bot = (y2 - y1) * 0.22
        x1 = max(0.0, x1 - pad_x)
        y1 = max(0.0, y1 - pad_top)
        x2 = min(1.0, x2 + pad_x)
        y2 = min(1.0, y2 + pad_bot)
        return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

    def norm_center(self, w: float, h: float) -> tuple[float, float]:
        if w <= 1 or h <= 1:
            return 0.0, 0.0
        return self.cx / w, self.cy / h

    def norm_foot(self, w: float, h: float) -> tuple[float, float]:
        if w <= 1 or h <= 1:
            return 0.0, 0.0
        return self.foot_x / w, min(1.0, self.foot_y / h)


@dataclass
class OccupancyDetectResult:
    tables: list[BoxDet] = field(default_factory=list)
    chairs: list[BoxDet] = field(default_factory=list)
    persons: list[BoxDet] = field(default_factory=list)
    frame_w: int = 0
    frame_h: int = 0
    infer_ms: float = 0.0
    error: str | None = None

    def to_dict(
        self,
        *,
        camera_id: int | None = None,
        use_association: bool = True,
        apply_temporal: bool = True,
    ) -> dict[str, Any]:
        w, h = float(self.frame_w), float(self.frame_h)
        # Annotate occupancy when persons were also detected in this pass.
        # Prefer person→chair→table association; fall back to table-only match.
        if self.persons:
            if use_association and self.tables:
                from v380.occupancy.associate import (
                    apply_temporal_to_tables,
                    associate_occupancy,
                )
                from v380.occupancy.tracker import CameraTemporalHub

                associate_occupancy(self.tables, self.chairs, self.persons)
                if apply_temporal and camera_id is not None:
                    apply_temporal_to_tables(
                        self.tables,
                        CameraTemporalHub.instance().temporal(camera_id),
                        camera_key=f"cam{int(camera_id)}",
                    )
            else:
                if self.chairs:
                    match_chair_occupancy(
                        self.chairs, self.persons, synthesize_occupied=False
                    )
                if self.tables:
                    match_table_occupancy(self.tables, self.persons)

        def _box(b: BoxDet) -> dict[str, Any]:
            nx1 = max(0.0, min(1.0, b.x1 / w)) if w > 1 else 0.0
            ny1 = max(0.0, min(1.0, b.y1 / h)) if h > 1 else 0.0
            nx2 = max(0.0, min(1.0, b.x2 / w)) if w > 1 else 0.0
            ny2 = max(0.0, min(1.0, b.y2 / h)) if h > 1 else 0.0
            is_table = b.label.startswith("dining") or b.label == "table"
            is_chair = "chair" in b.label
            out: dict[str, Any] = {
                "label": b.label,
                "conf": round(b.conf, 3),
                "x1": int(b.x1),
                "y1": int(b.y1),
                "x2": int(b.x2),
                "y2": int(b.y2),
                "norm": [round(nx1, 5), round(ny1, 5), round(nx2, 5), round(ny2, 5)],
                "points": b.as_norm_rect(w, h),
                "kind": "table" if is_table else ("chair" if is_chair else "person"),
            }
            if b.track_id is not None:
                out["track_id"] = int(b.track_id)
            if getattr(b, "_uncertain", False):
                out["uncertain"] = True
            if b.status:
                out["status"] = b.status
                out["occupied"] = b.status in {"occupied", "filled"}
                out["sitters"] = int(b.sitter_count)
                if is_table:
                    n = int(b.sitter_count)
                    if b.status == "filled":
                        out["label"] = f"table · occupied · {n} sit"
                        out["status"] = "occupied"  # UI: occupied/free
                    else:
                        out["label"] = "table · free"
                        out["status"] = "empty"
                elif is_chair:
                    # occupied / free (empty)
                    st = "free" if b.status == "empty" else (b.status or "empty")
                    out["label"] = f"chair · {st}"
                    if b.status == "empty":
                        out["status"] = "empty"
            if b.seated_person_conf is not None:
                out["person_conf"] = round(b.seated_person_conf, 3)
            return out

        chair_boxes = [_box(c) for c in self.chairs]
        table_boxes = [_box(t) for t in self.tables]
        person_boxes = [_box(p) for p in self.persons]
        occ_n = sum(1 for c in self.chairs if c.status == "occupied")
        empty_n = sum(1 for c in self.chairs if c.status == "empty")
        filled_n = sum(1 for t in self.tables if t.status == "filled")
        empty_t = sum(1 for t in self.tables if t.status == "empty")
        people_at_tables = sum(int(t.sitter_count) for t in self.tables)
        return {
            "tables": table_boxes,
            "chairs": chair_boxes,
            "persons": person_boxes,
            # Separate lists — clients should pick one mode, not mix.
            "items": table_boxes + chair_boxes + person_boxes,
            "table_count": len(self.tables),
            "chair_count": len(self.chairs),
            "person_count": len(self.persons),
            "chairs_occupied": occ_n,
            "chairs_empty": empty_n,
            "tables_filled": filled_n,
            "tables_empty": empty_t,
            "people_at_tables": people_at_tables,
            "frame_w": self.frame_w,
            "frame_h": self.frame_h,
            "infer_ms": round(self.infer_ms, 1),
            "error": self.error,
        }


def _rect_iou(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter / union) if union > 1e-12 else 0.0


def match_chair_occupancy(
    chairs: list[BoxDet],
    persons: list[BoxDet],
    *,
    synthesize_occupied: bool = True,
) -> tuple[int, int]:
    """Mark each chair empty/occupied by matching nearby persons.

    Goals:
    - Do not mark a foreground empty chair-back as occupied by someone behind it
    - Do mark a chair occupied when someone is clearly sitting in it
      (even if the first strict seat-point test misses)
    """
    if not chairs and not persons:
        return 0, 0
    if not persons:
        for c in chairs:
            c.status = "empty"
            c.sitter_count = 0
            c.seated_person_conf = None
        return 0, len(chairs)

    def _sit_score(chair: BoxDet, person: BoxDet) -> float | None:
        bw = max(1.0, chair.x2 - chair.x1)
        bh = max(1.0, chair.y2 - chair.y1)
        ph = max(1.0, person.y2 - person.y1)

        # Reject FG empty chairs: chair bottom much lower in the frame than person.
        depth_gap = chair.y2 - person.y2
        if depth_gap > max(bh * 0.18, ph * 0.22):
            return None
        # Person entirely above the chair (walking behind) .
        if person.y2 < chair.y1 + bh * 0.15:
            return None

        cx1 = chair.x1 - bw * 0.18
        cy1 = chair.y1 - bh * 0.45
        cx2 = chair.x2 + bw * 0.18
        cy2 = chair.y2 + bh * 0.12

        sx, sy = person.seat_x, person.seat_y
        fx, fy = person.foot_x, person.foot_y
        seat_in = cx1 <= sx <= cx2 and cy1 <= sy <= cy2
        foot_in = cx1 <= fx <= cx2 and cy1 <= fy <= cy2

        chair_r = (chair.x1, chair.y1, chair.x2, chair.y2)
        lower = (person.x1, person.y1 + ph * 0.45, person.x2, person.y2)
        iou = _rect_iou(lower, chair_r)

        # Need seat/foot in band OR strong lower-body overlap (side-angle sits).
        if not (seat_in or foot_in or iou >= 0.14):
            return None

        if abs(person.cx - chair.cx) > bw * 0.65:
            return None

        # Vertical centers should be close — FG chair-back vs mid-room sitter diverge.
        if abs(person.cy - chair.cy) > max(bh, ph) * 0.55:
            return None

        depth_pen = abs(person.y2 - chair.y2) / max(bh, 1.0)
        center_pen = abs(person.cx - chair.cx) / max(bw, 1.0)
        return 1.0 + iou + (0.35 if seat_in else 0.0) - 0.4 * depth_pen - 0.25 * center_pen

    candidates: list[tuple[float, int, int]] = []
    for ci, chair in enumerate(chairs):
        for pi, person in enumerate(persons):
            score = _sit_score(chair, person)
            if score is None:
                continue
            candidates.append((score, ci, pi))

    candidates.sort(key=lambda t: t[0], reverse=True)
    taken_chairs: set[int] = set()
    taken_persons: set[int] = set()
    assigned: dict[int, int] = {}
    for _score, ci, pi in candidates:
        if ci in taken_chairs or pi in taken_persons:
            continue
        taken_chairs.add(ci)
        taken_persons.add(pi)
        assigned[ci] = pi

    # Second pass: empty chair + unmatched person with clear overlap → occupy
    # that chair (fixes “person sitting but chair still free”).
    for ci, chair in enumerate(chairs):
        if ci in assigned:
            continue
        bw = max(1.0, chair.x2 - chair.x1)
        bh = max(1.0, chair.y2 - chair.y1)
        chair_r = (chair.x1, chair.y1, chair.x2, chair.y2)
        best_pi: int | None = None
        best_s = 0.0
        for pi, person in enumerate(persons):
            if pi in taken_persons:
                continue
            ph = max(1.0, person.y2 - person.y1)
            if chair.y2 - person.y2 > max(bh * 0.18, ph * 0.22):
                continue
            if abs(person.cx - chair.cx) > bw * 0.70:
                continue
            lower = (person.x1, person.y1 + ph * 0.40, person.x2, person.y2)
            iou = _rect_iou(lower, chair_r)
            person_r = (person.x1, person.y1, person.x2, person.y2)
            full_iou = _rect_iou(person_r, chair_r)
            # Sitting in chair: body heavily overlaps the chair box.
            if max(iou, full_iou) < 0.12:
                continue
            s = max(iou, full_iou)
            if s > best_s:
                best_s = s
                best_pi = pi
        if best_pi is not None:
            assigned[ci] = best_pi
            taken_persons.add(best_pi)
            taken_chairs.add(ci)

    for ci, chair in enumerate(chairs):
        if ci in assigned:
            chair.status = "occupied"
            chair.sitter_count = 1
            chair.seated_person_conf = persons[assigned[ci]].conf
        else:
            chair.status = "empty"
            chair.sitter_count = 0
            chair.seated_person_conf = None

    # Unmatched person, no overlapping empty chair → synthetic occupied seat.
    if synthesize_occupied:
        for pi, person in enumerate(persons):
            if pi in taken_persons:
                continue
            pw = max(1.0, person.x2 - person.x1)
            ph = max(1.0, person.y2 - person.y1)
            if ph > pw * 2.8:
                continue
            sx1 = person.x1 + pw * 0.08
            sx2 = person.x2 - pw * 0.08
            sy1 = person.y1 + ph * 0.48
            sy2 = person.y2
            if sx2 <= sx1 or sy2 <= sy1:
                continue
            seat_rect = (sx1, sy1, sx2, sy2)
            # Prefer claiming a nearby empty chair instead of inventing a box.
            claimed = False
            for ci, c in enumerate(chairs):
                if c.status != "empty":
                    continue
                if _rect_iou(seat_rect, (c.x1, c.y1, c.x2, c.y2)) < 0.10:
                    continue
                cbh = max(1.0, c.y2 - c.y1)
                if c.y2 - person.y2 > cbh * 0.22:
                    continue
                c.status = "occupied"
                c.sitter_count = 1
                c.seated_person_conf = float(person.conf)
                taken_persons.add(pi)
                claimed = True
                break
            if claimed:
                continue
            if any(
                c.status == "occupied"
                and _rect_iou(seat_rect, (c.x1, c.y1, c.x2, c.y2)) >= 0.22
                for c in chairs
            ):
                continue
            chairs.append(
                BoxDet(
                    label="chair",
                    conf=float(person.conf),
                    x1=sx1,
                    y1=sy1,
                    x2=sx2,
                    y2=sy2,
                    status="occupied",
                    sitter_count=1,
                    seated_person_conf=float(person.conf),
                )
            )

    occ = sum(1 for c in chairs if c.status == "occupied")
    empty = sum(1 for c in chairs if c.status == "empty")
    return occ, empty


def match_table_occupancy(
    tables: list[BoxDet],
    persons: list[BoxDet],
) -> tuple[int, int]:
    """Mark each table filled/empty and count how many people sit at it.

    Matches front *and* side sitters (hips/feet near table perimeter).
    Rejects walk-bys where only the upper body grazes the desktop.
    Each person → at most one table (best score).
    """
    if not tables:
        return 0, 0
    if not persons:
        for t in tables:
            t.status = "empty"
            t.sitter_count = 0
            t.seated_person_conf = None
        return 0, len(tables)

    def _at_table(table: BoxDet, person: BoxDet) -> float | None:
        tw = max(1.0, table.x2 - table.x1)
        th = max(1.0, table.y2 - table.y1)
        ph = max(1.0, person.y2 - person.y1)
        pw = max(1.0, person.x2 - person.x1)

        # Balanced ring: side sits OK, not so wide that walk-bys mark occupied.
        rx1 = table.x1 - tw * 0.28
        rx2 = table.x2 + tw * 0.28
        ry1 = table.y1 - th * 0.08
        ry2 = table.y2 + th * 0.48
        ex1 = table.x1 - tw * 0.12
        ey1 = table.y1 - th * 0.05
        ex2 = table.x2 + tw * 0.12
        ey2 = table.y2 + th * 0.22
        table_exp = (ex1, ey1, ex2, ey2)

        sx, sy = person.seat_x, person.seat_y
        fx, fy = person.foot_x, person.foot_y
        seat_in = rx1 <= sx <= rx2 and ry1 <= sy <= ry2
        foot_in = rx1 <= fx <= rx2 and ry1 <= fy <= ry2

        x_overlap = min(person.x2, table.x2 + tw * 0.22) - max(
            person.x1, table.x1 - tw * 0.22
        )
        if x_overlap < pw * 0.12 and abs(person.cx - table.cx) > tw * 0.62:
            return None

        # Walking behind / above the table.
        if person.y2 < table.y1 + th * 0.12:
            return None

        table_core = (table.x1, table.y1, table.x2, table.y2)
        person_r = (person.x1, person.y1, person.x2, person.y2)
        lower = (person.x1, person.y1 + ph * 0.45, person.x2, person.y2)
        upper = (person.x1, person.y1, person.x2, person.y1 + ph * 0.40)
        ring = (rx1, ry1, rx2, ry2)

        full_iou = max(_rect_iou(person_r, table_core), _rect_iou(person_r, table_exp))
        lower_on_table = max(
            _rect_iou(lower, table_core), _rect_iou(lower, table_exp)
        )
        lower_on_ring = _rect_iou(lower, ring)
        upper_on_table = _rect_iou(upper, table_core)

        hips_on_table = (
            table.x1 - tw * 0.25 <= sx <= table.x2 + tw * 0.25
            and table.y1 - th * 0.05 <= sy <= table.y2 + th * 0.38
        )

        # Need hips/seat near table OR solid body overlap — not a distant passerby.
        if not (seat_in or foot_in or hips_on_table or full_iou >= 0.10):
            return None
        if max(full_iou, lower_on_table, lower_on_ring) < 0.05 and not (
            seat_in or hips_on_table
        ):
            return None

        # Shoulder/arm graze only: reject.
        if (
            not (seat_in or foot_in or hips_on_table)
            and upper_on_table >= 0.05
            and upper_on_table > max(lower_on_table, lower_on_ring) * 1.4
        ):
            return None

        score = (
            1.0
            + full_iou
            + 0.5 * max(lower_on_table, lower_on_ring)
            + (0.35 if seat_in or hips_on_table else 0.0)
            - (abs(person.cx - table.cx) / max(tw, 1.0)) * 0.25
        )
        return score

    # Collect (score, table_i, person_i); assign each person to best table only.
    candidates: list[tuple[float, int, int]] = []
    for ti, table in enumerate(tables):
        for pi, person in enumerate(persons):
            score = _at_table(table, person)
            if score is None:
                continue
            candidates.append((score, ti, pi))
    candidates.sort(key=lambda t: t[0], reverse=True)

    taken_persons: set[int] = set()
    sitters: dict[int, list[int]] = {i: [] for i in range(len(tables))}
    for score, ti, pi in candidates:
        if pi in taken_persons:
            continue
        taken_persons.add(pi)
        sitters[ti].append(pi)

    # Second pass: clear overlap only (side lean) — keep tight to avoid false occupied.
    for ti, table in enumerate(tables):
        if sitters[ti]:
            continue
        tw = max(1.0, table.x2 - table.x1)
        th = max(1.0, table.y2 - table.y1)
        table_r = (
            table.x1 - tw * 0.14,
            table.y1 - th * 0.06,
            table.x2 + tw * 0.14,
            table.y2 + th * 0.28,
        )
        best_pi: int | None = None
        best_s = 0.0
        for pi, person in enumerate(persons):
            if pi in taken_persons:
                continue
            ph = max(1.0, person.y2 - person.y1)
            if person.y2 < table.y1 + th * 0.12:
                continue
            person_r = (person.x1, person.y1, person.x2, person.y2)
            iou = _rect_iou(person_r, table_r)
            sx, sy = person.seat_x, person.seat_y
            hips_near = (
                table.x1 - tw * 0.25 <= sx <= table.x2 + tw * 0.25
                and table.y1 - th * 0.06 <= sy <= table.y2 + th * 0.40
            )
            if iou < 0.08 and not hips_near:
                continue
            if iou < 0.05:
                continue
            standing = ph > max(1.0, person.x2 - person.x1) * 2.6 and iou < 0.12
            if standing and not hips_near:
                continue
            s = iou + (0.4 if hips_near else 0.0)
            if s > best_s:
                best_s = s
                best_pi = pi
        if best_pi is not None:
            sitters[ti].append(best_pi)
            taken_persons.add(best_pi)

    filled = 0
    for ti, table in enumerate(tables):
        n = len(sitters[ti])
        table.sitter_count = n
        if n > 0:
            table.status = "filled"
            table.seated_person_conf = persons[sitters[ti][0]].conf
            filled += 1
        else:
            table.status = "empty"
            table.seated_person_conf = None
    return filled, len(tables) - filled


class OccupancyYoloWorld:
    """Open-vocab YOLO-World for office desks / tables."""

    _instance: OccupancyYoloWorld | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._prompts: list[str] = []
        self.conf = 0.20
        self.iou = 0.45
        self.infer_width = 640
        self.device = "cpu"

    @classmethod
    def instance(cls) -> OccupancyYoloWorld:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = OccupancyYoloWorld()
            return cls._instance

    def available(self) -> tuple[bool, str]:
        try:
            import ultralytics  # noqa: F401
        except Exception as exc:
            return False, f"ultralytics missing: {exc}"
        if self._load_error:
            return False, self._load_error
        return True, "ok"

    def ensure_loaded(self) -> bool:
        from v380.ai.config import WorldDetectConfig

        cfg = WorldDetectConfig()
        want_prompts = [p for p in cfg.class_prompts if str(p).strip()]
        if self._model is not None:
            # Always refresh conf/prompts — otherwise a warm process keeps the old low bar.
            with self._lock:
                self.conf = float(cfg.conf)
                self.iou = float(cfg.iou)
                self.infer_width = int(cfg.infer_width)
                self.device = str(cfg.device)
                if want_prompts and want_prompts != self._prompts:
                    try:
                        self._model.set_classes(want_prompts)
                        self._prompts = want_prompts
                    except Exception:
                        pass
            return True
        with self._lock:
            if self._model is not None:
                return True
            try:
                import shutil
                from pathlib import Path

                weights = cfg.weights_path()
                path = str(weights if weights.is_file() else cfg.weights_name)
                try:
                    from ultralytics import YOLOWorld

                    self._model = YOLOWorld(path)
                except Exception:
                    from ultralytics import YOLO

                    self._model = YOLO(path)
                # No empty "" class — it was dragging confidences down.
                self._model.set_classes(want_prompts)
                self._prompts = want_prompts
                self.conf = float(cfg.conf)
                self.iou = float(cfg.iou)
                self.infer_width = int(cfg.infer_width)
                self.device = str(cfg.device)
                try:
                    if not weights.is_file():
                        src = getattr(self._model, "ckpt_path", None) or getattr(
                            self._model, "model_path", None
                        )
                        if src and Path(str(src)).is_file():
                            shutil.copy2(str(src), str(weights))
                except Exception:
                    pass
                self._load_error = None
                return True
            except Exception as exc:
                self._load_error = str(exc)
                self._model = None
                return False

    def detect_tables_rgb(
        self,
        rgb: np.ndarray,
        *,
        conf: float | None = None,
    ) -> tuple[list[BoxDet], float, str | None]:
        """Return (table boxes, infer_ms, error)."""
        if rgb is None or getattr(rgb, "size", 0) == 0:
            return [], 0.0, "empty frame"
        if not self.ensure_loaded():
            return [], 0.0, self._load_error or "yolo-world not loaded"

        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        run_conf = float(self.conf if conf is None else conf)
        run_conf = max(0.35, min(0.99, run_conf))
        imgsz = max(960, int(self.infer_width))

        # Full frame + dual panes (office multi-desk rooms need coverage).
        tiles: list[tuple[np.ndarray, float, float]] = [(rgb, 0.0, 0.0)]
        if h >= int(w * 1.15):
            mid = h // 2
            overlap = max(24, h // 12)
            tiles.append((rgb[0 : mid + overlap, :, :], 0.0, 0.0))
            tiles.append((rgb[mid - overlap : h, :, :], 0.0, float(mid - overlap)))

        t0 = time.perf_counter()
        out: list[BoxDet] = []
        try:
            for crop, x_off, y_off in tiles:
                ch, cw = int(crop.shape[0]), int(crop.shape[1])
                src = crop
                scale = 1.0
                max_side = max(ch, cw)
                target = max(imgsz, int(imgsz * 2.2))
                if max_side > target:
                    scale = target / float(max_side)
                    try:
                        import cv2

                        src = cv2.resize(
                            crop,
                            (max(1, int(cw * scale)), max(1, int(ch * scale))),
                            interpolation=cv2.INTER_AREA,
                        )
                    except Exception:
                        src = crop
                        scale = 1.0
                with self._lock:
                    results = self._model.predict(
                        source=src,
                        conf=run_conf,
                        iou=float(self.iou),
                        imgsz=int(imgsz),
                        device=self.device,
                        verbose=False,
                        max_det=50,
                    )
                if not results:
                    continue
                r0 = results[0]
                boxes = getattr(r0, "boxes", None)
                names = getattr(r0, "names", None) or getattr(self._model, "names", {}) or {}
                if boxes is None or len(boxes) == 0:
                    continue
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
                inv = (1.0 / scale) if scale > 0 else 1.0
                frame_area = float(max(1, w * h))
                for i in range(len(xyxy)):
                    x1, y1, x2, y2 = [float(v) * inv for v in xyxy[i][:4]]
                    x1 = max(0.0, x1 + x_off)
                    y1 = max(0.0, y1 + y_off)
                    x2 = min(float(w - 1), x2 + x_off)
                    y2 = min(float(h - 1), y2 + y_off)
                    conf_v = float(confs[i]) if i < len(confs) else 0.0
                    if not _is_plausible_table_box(
                        x1,
                        y1,
                        x2,
                        y2,
                        frame_w=float(w),
                        frame_h=float(h),
                        conf=conf_v,
                        min_conf=run_conf,
                    ):
                        continue
                    cid = int(clss[i]) if i < len(clss) else -1
                    raw_name = str(names.get(cid, "") if isinstance(names, dict) else "").strip()
                    if not raw_name:
                        continue
                    out.append(
                        BoxDet(
                            label="table",
                            conf=conf_v,
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                        )
                    )
        except Exception as exc:
            return [], (time.perf_counter() - t0) * 1000.0, str(exc)

        # Stronger NMS — merge duplicate desk hits (was creating T10/T11 twins).
        out = _nms_boxes(out, iou_thresh=0.45)
        out = _filter_table_boxes(out, frame_w=float(w), frame_h=float(h), min_conf=run_conf)
        ms = (time.perf_counter() - t0) * 1000.0
        return out, ms, None


class OccupancyYolo:
    """Thread-safe lazy YOLOv8n wrapper — person + chair (COCO)."""

    _instance: OccupancyYolo | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self.conf = 0.35
        self.iou = 0.45
        self.infer_width = 640
        self.device = "cpu"

    @classmethod
    def instance(cls) -> OccupancyYolo:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = OccupancyYolo()
            return cls._instance

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def available(self) -> tuple[bool, str]:
        try:
            import ultralytics  # noqa: F401
        except Exception as exc:
            return False, f"ultralytics missing: {exc}"
        if self._load_error:
            return False, self._load_error
        return True, "ok"

    def ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            try:
                import shutil
                from pathlib import Path

                from ultralytics import YOLO

                from v380.ai.config import PersonDetectConfig

                cfg = PersonDetectConfig()
                weights = cfg.weights_path()
                path = str(weights if weights.is_file() else cfg.weights_name)
                self._model = YOLO(path)
                try:
                    if not weights.is_file():
                        src = getattr(self._model, "ckpt_path", None) or getattr(
                            self._model, "model_path", None
                        )
                        if src and Path(str(src)).is_file():
                            shutil.copy2(str(src), str(weights))
                except Exception:
                    pass
                self._load_error = None
                return True
            except Exception as exc:
                self._load_error = str(exc)
                self._model = None
                return False

    def detect_jpeg(
        self,
        jpeg: bytes,
        *,
        tables: bool = True,
        chairs: bool = False,
        persons: bool = True,
        conf: float | None = None,
        camera_id: int | None = None,
    ) -> OccupancyDetectResult:
        if not jpeg:
            return OccupancyDetectResult(error="no jpeg")
        try:
            import cv2
        except Exception as exc:
            return OccupancyDetectResult(error=f"cv2 missing: {exc}")

        arr = np.frombuffer(jpeg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            return OccupancyDetectResult(error="bad jpeg")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return self.detect_rgb(
            rgb,
            tables=tables,
            chairs=chairs,
            persons=persons,
            conf=conf,
            camera_id=camera_id,
        )

    def detect_rgb(
        self,
        rgb: np.ndarray,
        *,
        tables: bool = True,
        chairs: bool = False,
        persons: bool = True,
        conf: float | None = None,
        camera_id: int | None = None,
    ) -> OccupancyDetectResult:
        if rgb is None or getattr(rgb, "size", 0) == 0:
            return OccupancyDetectResult(error="empty frame")

        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        run_conf = float(self.conf if conf is None else conf)
        run_conf = max(0.01, min(0.99, run_conf))

        tables_out: list[BoxDet] = []
        chairs_out: list[BoxDet] = []
        persons_out: list[BoxDet] = []
        total_ms = 0.0
        errors: list[str] = []

        # --- Tables: YOLO-World (desks) + COCO dining_table (merge both) ---
        if tables:
            # Desks only — never inherit the low person/chair confidence floor.
            world_conf = max(0.40, float(run_conf))
            world = OccupancyYoloWorld.instance()
            w_boxes, w_ms, w_err = world.detect_tables_rgb(rgb, conf=world_conf)
            total_ms += w_ms
            if w_err:
                errors.append(f"world: {w_err}")
            else:
                tables_out.extend(w_boxes)
            # COCO dining_table is noisy on walls — keep it strict or skip if World ok.
            if self.ensure_loaded() and not tables_out:
                try:
                    coco_tables, coco_ms = self._detect_coco_classes(
                        rgb,
                        class_ids=[_DINING_TABLE],
                        conf=max(0.40, float(run_conf)),
                        detail=True,
                    )
                    total_ms += coco_ms
                    for b in coco_tables:
                        b.label = "table"
                        tables_out.append(b)
                except Exception as exc:
                    errors.append(f"coco-table: {exc}")
            elif not tables_out and not self.ensure_loaded():
                errors.append(self._load_error or "yolo not loaded")
            tables_out = _filter_table_boxes(
                tables_out, frame_w=float(w), frame_h=float(h), min_conf=0.35
            )
            tables_out = _nms_boxes(tables_out, iou_thresh=0.50)

        # --- Persons + chairs: YOLOv8n COCO ---
        need_coco = bool(chairs or persons)
        if need_coco:
            if not self.ensure_loaded():
                err = self._load_error or "yolo not loaded"
                if not tables_out:
                    return OccupancyDetectResult(frame_w=w, frame_h=h, error=err)
                errors.append(err)
            else:
                class_ids: list[int] = []
                if persons:
                    class_ids.append(_PERSON)
                if chairs:
                    class_ids.append(_CHAIR)
                try:
                    coco_boxes, coco_ms = self._detect_coco_classes(
                        rgb,
                        class_ids=class_ids,
                        conf=run_conf,
                        detail=True,
                    )
                    total_ms += coco_ms
                    for b in coco_boxes:
                        if b.label == "chair":
                            chairs_out.append(b)
                        elif b.label == "person":
                            persons_out.append(b)
                except Exception as exc:
                    if not tables_out:
                        return OccupancyDetectResult(frame_w=w, frame_h=h, error=str(exc))
                    errors.append(str(exc))

        chairs_out = _nms_boxes(chairs_out, iou_thresh=0.45)
        persons_out = _nms_boxes(persons_out, iou_thresh=0.45)

        # Stable track IDs across sparse JPEG polls (ByteTrack-style IoU).
        try:
            from v380.occupancy.tracker import CameraTrackHub

            tracker = CameraTrackHub.instance().tracker(camera_id)
            if persons_out:
                tracker.update(persons_out, kind="person", frame_w=w, frame_h=h)
            if chairs_out:
                tracker.update(chairs_out, kind="chair", frame_w=w, frame_h=h)
            if tables_out:
                tracker.update(tables_out, kind="table", frame_w=w, frame_h=h)
        except Exception:
            pass

        if not tables and not chairs_out and not persons_out and not tables_out:
            if errors:
                return OccupancyDetectResult(
                    frame_w=w, frame_h=h, error="; ".join(errors)
                )

        return OccupancyDetectResult(
            tables=tables_out,
            chairs=chairs_out,
            persons=persons_out,
            frame_w=w,
            frame_h=h,
            infer_ms=total_ms,
            error="; ".join(errors) if (errors and not (tables_out or chairs_out or persons_out)) else None,
        )

    def _detect_coco_classes(
        self,
        rgb: np.ndarray,
        *,
        class_ids: list[int],
        conf: float,
        detail: bool,
    ) -> tuple[list[BoxDet], float]:
        """Run YOLOv8n for the given COCO class ids; return (boxes, ms).

        Dual-pane cameras: always run full frame + top/bottom tiles (wide overlap)
        so people sitting in a corner of one pane are less likely to be missed.
        """
        if not class_ids:
            return [], 0.0
        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        imgsz = max(int(self.infer_width), 960 if detail else int(self.infer_width))
        tiles: list[tuple[np.ndarray, float, float]] = [(rgb, 0.0, 0.0)]
        if detail and h >= int(w * 1.15):
            mid = h // 2
            overlap = max(24, h // 12)  # ~8%+ — avoid cutting people on the seam
            tiles.append((rgb[0 : mid + overlap, :, :], 0.0, 0.0))
            tiles.append((rgb[mid - overlap : h, :, :], 0.0, float(mid - overlap)))

        t0 = time.perf_counter()
        out: list[BoxDet] = []
        for crop, x_off, y_off in tiles:
            boxes = self._predict_boxes(
                crop,
                class_ids=class_ids,
                conf=conf,
                imgsz=imgsz,
                detail=detail,
            )
            for cid, conf_v, x1, y1, x2, y2 in boxes:
                label = _LABELS.get(cid, f"class_{cid}")
                out.append(
                    BoxDet(
                        label=label,
                        conf=conf_v,
                        x1=max(0.0, x1 + x_off),
                        y1=max(0.0, y1 + y_off),
                        x2=min(float(w - 1), x2 + x_off),
                        y2=min(float(h - 1), y2 + y_off),
                    )
                )
        out = _nms_boxes(out, iou_thresh=0.50)
        return out, (time.perf_counter() - t0) * 1000.0

    def _predict_boxes(
        self,
        rgb: np.ndarray,
        *,
        class_ids: list[int],
        conf: float,
        imgsz: int,
        detail: bool,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """Run YOLO on one crop; returns (class_id, conf, x1, y1, x2, y2) in crop coords."""
        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        src = rgb
        scale = 1.0
        # Keep more resolution for chair/person; still cap for CPU speed.
        max_side = max(h, w)
        target = max(imgsz, int(imgsz * 2.5) if detail else imgsz)
        if max_side > target:
            scale = target / float(max_side)
            try:
                import cv2

                src = cv2.resize(
                    rgb,
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            except Exception:
                src = rgb
                scale = 1.0

        with self._lock:
            results = self._model.predict(
                source=src,
                conf=float(conf),
                iou=float(self.iou),
                classes=class_ids,
                imgsz=int(imgsz),
                device=self.device,
                verbose=False,
                max_det=100,
            )

        out: list[tuple[int, float, float, float, float, float]] = []
        if not results:
            return out
        r0 = results[0]
        boxes = getattr(r0, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return out
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
        inv = (1.0 / scale) if scale > 0 else 1.0
        for i in range(len(xyxy)):
            x1, y1, x2, y2 = [float(v) * inv for v in xyxy[i][:4]]
            cid = int(clss[i]) if i < len(clss) else -1
            conf_v = float(confs[i]) if i < len(confs) else 0.0
            out.append(
                (
                    cid,
                    conf_v,
                    max(0.0, x1),
                    max(0.0, y1),
                    min(float(w - 1), x2),
                    min(float(h - 1), y2),
                )
            )
        return out


def _is_plausible_table_box(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    frame_w: float,
    frame_h: float,
    conf: float = 0.0,
    min_conf: float = 0.22,
) -> bool:
    """Reject wall / poster / door / shelf shapes that YOLO-World mislabels as desks."""
    if conf < float(min_conf):
        return False
    bw = max(0.0, x2 - x1)
    bh = max(0.0, y2 - y1)
    if bw < 24 or bh < 18:
        return False
    fw = max(1.0, float(frame_w))
    fh = max(1.0, float(frame_h))
    area_frac = (bw * bh) / (fw * fh)
    # Too small = shelf/poster; too big = wall / floor plane.
    if area_frac < 0.018 or area_frac > 0.35:
        return False
    # Desks are wider than tall — reject tall wall / door strips.
    aspect = bw / max(bh, 1.0)
    if aspect < 0.75 or aspect > 4.0:
        return False
    if bh > fh * 0.42:
        return False
    if bw > fw * 0.80 and bh > fh * 0.28:
        return False
    cy = (y1 + y2) * 0.5 / fh
    if cy < 0.22 and area_frac < 0.08:
        return False
    return True


def _filter_table_boxes(
    boxes: list[BoxDet],
    *,
    frame_w: float,
    frame_h: float,
    min_conf: float = 0.25,
) -> list[BoxDet]:
    return [
        b
        for b in boxes
        if _is_plausible_table_box(
            b.x1,
            b.y1,
            b.x2,
            b.y2,
            frame_w=frame_w,
            frame_h=frame_h,
            conf=float(b.conf),
            min_conf=min_conf,
        )
    ]


def _nms_boxes(boxes: list[BoxDet], *, iou_thresh: float = 0.45) -> list[BoxDet]:
    """Keep highest-conf box when duplicates overlap (dual-pane seam)."""
    if len(boxes) <= 1:
        return boxes
    order = sorted(range(len(boxes)), key=lambda i: boxes[i].conf, reverse=True)
    keep: list[BoxDet] = []
    suppressed: set[int] = set()
    for i in order:
        if i in suppressed:
            continue
        a = boxes[i]
        keep.append(a)
        ar = (a.x1, a.y1, a.x2, a.y2)
        for j in order:
            if j == i or j in suppressed:
                continue
            b = boxes[j]
            if _rect_iou(ar, (b.x1, b.y1, b.x2, b.y2)) >= iou_thresh:
                suppressed.add(j)
    return keep


def box_iou_norm(
    a: list[list[float]], b: list[list[float]]
) -> float:
    """IoU of axis-aligned bboxes from polygon point lists (normalized)."""
    if not a or not b:
        return 0.0

    def _bb(pts: list[list[float]]) -> tuple[float, float, float, float]:
        xs = [float(p[0]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        ys = [float(p[1]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not xs or not ys:
            return 0.0, 0.0, 0.0, 0.0
        return min(xs), min(ys), max(xs), max(ys)

    ax1, ay1, ax2, ay2 = _bb(a)
    bx1, by1, bx2, by2 = _bb(b)
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter / union) if union > 1e-12 else 0.0
