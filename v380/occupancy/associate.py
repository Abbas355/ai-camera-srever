"""Spatial association: person → chair → table (Floor occupancy).

Prevents a seated person from falsely occupying a neighboring table when
bounding boxes overlap. Prefer chair bridge when chairs are detected.
"""

from __future__ import annotations

from typing import Any

from v380.occupancy.yolo_detector import BoxDet, _rect_iou


def _person_chair_score(person: BoxDet, chair: BoxDet) -> float | None:
    """How well this person sits on this chair."""
    cw = max(1.0, chair.x2 - chair.x1)
    ch = max(1.0, chair.y2 - chair.y1)
    ph = max(1.0, person.y2 - person.y1)
    # Reject FG chair-back claiming someone behind.
    if chair.y2 - person.y2 > max(ch * 0.22, ph * 0.25):
        return None
    if abs(person.cx - chair.cx) > cw * 0.70:
        return None
    chair_r = (chair.x1, chair.y1, chair.x2, chair.y2)
    lower = (person.x1, person.y1 + ph * 0.42, person.x2, person.y2)
    person_r = (person.x1, person.y1, person.x2, person.y2)
    iou_l = _rect_iou(lower, chair_r)
    iou_f = _rect_iou(person_r, chair_r)
    sx, sy = person.seat_x, person.seat_y
    seat_in = (
        chair.x1 - cw * 0.20 <= sx <= chair.x2 + cw * 0.20
        and chair.y1 - ch * 0.35 <= sy <= chair.y2 + ch * 0.15
    )
    if max(iou_l, iou_f) < 0.08 and not seat_in:
        return None
    return 1.0 + max(iou_l, iou_f) + (0.4 if seat_in else 0.0)


def _chair_table_score(chair: BoxDet, table: BoxDet) -> float | None:
    """Chair belongs to this table (beside / tucked under)."""
    tw = max(1.0, table.x2 - table.x1)
    th = max(1.0, table.y2 - table.y1)
    # Expanded table ring for chairs.
    rx1 = table.x1 - tw * 0.30
    rx2 = table.x2 + tw * 0.30
    ry1 = table.y1 - th * 0.10
    ry2 = table.y2 + th * 0.55
    cx, cy = chair.cx, chair.cy
    if not (rx1 <= cx <= rx2 and ry1 <= cy <= ry2):
        # Also allow IoU with expanded rect.
        chair_r = (chair.x1, chair.y1, chair.x2, chair.y2)
        table_exp = (rx1, ry1, rx2, ry2)
        if _rect_iou(chair_r, table_exp) < 0.04:
            return None
    # Prefer chairs whose seat faces into the table (center closer).
    dist = abs(chair.cx - table.cx) / tw + abs(chair.cy - table.cy) / th
    iou = _rect_iou(
        (chair.x1, chair.y1, chair.x2, chair.y2),
        (table.x1, table.y1, table.x2, table.y2),
    )
    return 1.5 + iou - 0.35 * dist


def _person_table_score(person: BoxDet, table: BoxDet) -> float | None:
    """Direct person↔table (weaker than chair bridge)."""
    tw = max(1.0, table.x2 - table.x1)
    th = max(1.0, table.y2 - table.y1)
    ph = max(1.0, person.y2 - person.y1)
    pw = max(1.0, person.x2 - person.x1)

    rx1 = table.x1 - tw * 0.28
    rx2 = table.x2 + tw * 0.28
    ry1 = table.y1 - th * 0.08
    ry2 = table.y2 + th * 0.48
    sx, sy = person.seat_x, person.seat_y
    fx, fy = person.foot_x, person.foot_y
    seat_in = rx1 <= sx <= rx2 and ry1 <= sy <= ry2
    foot_in = rx1 <= fx <= rx2 and ry1 <= fy <= ry2

    x_overlap = min(person.x2, table.x2 + tw * 0.22) - max(
        person.x1, table.x1 - tw * 0.22
    )
    if x_overlap < pw * 0.12 and abs(person.cx - table.cx) > tw * 0.62:
        return None
    if person.y2 < table.y1 + th * 0.12:
        return None

    table_core = (table.x1, table.y1, table.x2, table.y2)
    table_exp = (
        table.x1 - tw * 0.12,
        table.y1 - th * 0.05,
        table.x2 + tw * 0.12,
        table.y2 + th * 0.22,
    )
    person_r = (person.x1, person.y1, person.x2, person.y2)
    lower = (person.x1, person.y1 + ph * 0.45, person.x2, person.y2)
    upper = (person.x1, person.y1, person.x2, person.y1 + ph * 0.40)
    ring = (rx1, ry1, rx2, ry2)

    full_iou = max(_rect_iou(person_r, table_core), _rect_iou(person_r, table_exp))
    lower_on = max(_rect_iou(lower, table_core), _rect_iou(lower, table_exp))
    lower_ring = _rect_iou(lower, ring)
    upper_on = _rect_iou(upper, table_core)
    hips = (
        table.x1 - tw * 0.25 <= sx <= table.x2 + tw * 0.25
        and table.y1 - th * 0.05 <= sy <= table.y2 + th * 0.38
    )
    if not (seat_in or foot_in or hips or full_iou >= 0.10):
        return None
    if max(full_iou, lower_on, lower_ring) < 0.05 and not (seat_in or hips):
        return None
    if (
        not (seat_in or foot_in or hips)
        and upper_on >= 0.05
        and upper_on > max(lower_on, lower_ring) * 1.4
    ):
        return None
    # Direct link is weaker than person→chair→table.
    return (
        0.85
        + full_iou
        + 0.45 * max(lower_on, lower_ring)
        + (0.3 if seat_in or hips else 0.0)
        - (abs(person.cx - table.cx) / tw) * 0.25
    )


def associate_occupancy(
    tables: list[BoxDet],
    chairs: list[BoxDet],
    persons: list[BoxDet],
) -> tuple[int, int]:
    """Mark tables filled/empty using person→chair→table when possible.

    Each person → at most one table (best score).
    Returns (filled_count, empty_count).
    """
    if not tables:
        return 0, 0
    if not persons:
        for t in tables:
            t.status = "empty"
            t.sitter_count = 0
            t.seated_person_conf = None
        for c in chairs:
            c.status = "empty"
            c.sitter_count = 0
            c.seated_person_conf = None
        return 0, len(tables)

    # --- Person → chair ---
    pc_pairs: list[tuple[float, int, int]] = []
    for pi, person in enumerate(persons):
        for ci, chair in enumerate(chairs):
            s = _person_chair_score(person, chair)
            if s is None:
                continue
            pc_pairs.append((s, pi, ci))
    pc_pairs.sort(key=lambda t: t[0], reverse=True)
    person_chair: dict[int, int] = {}
    chair_person: dict[int, int] = {}
    for _s, pi, ci in pc_pairs:
        if pi in person_chair or ci in chair_person:
            continue
        person_chair[pi] = ci
        chair_person[ci] = pi

    for ci, chair in enumerate(chairs):
        if ci in chair_person:
            chair.status = "occupied"
            chair.sitter_count = 1
            chair.seated_person_conf = persons[chair_person[ci]].conf
        else:
            chair.status = "empty"
            chair.sitter_count = 0
            chair.seated_person_conf = None

    # --- Chair → table ---
    ct_pairs: list[tuple[float, int, int]] = []
    for ci, chair in enumerate(chairs):
        for ti, table in enumerate(tables):
            s = _chair_table_score(chair, table)
            if s is None:
                continue
            ct_pairs.append((s, ci, ti))
    ct_pairs.sort(key=lambda t: t[0], reverse=True)
    chair_table: dict[int, int] = {}
    for _s, ci, ti in ct_pairs:
        if ci in chair_table:
            continue
        chair_table[ci] = ti

    # --- Score person → table (chair bridge preferred) ---
    # (score, table_i, person_i)
    candidates: list[tuple[float, int, int]] = []
    for pi, person in enumerate(persons):
        if pi in person_chair:
            ci = person_chair[pi]
            if ci in chair_table:
                ti = chair_table[ci]
                # Strong boost for full chain.
                base = _chair_table_score(chairs[ci], tables[ti]) or 1.0
                candidates.append((2.5 + base, ti, pi))
                continue
        for ti, table in enumerate(tables):
            s = _person_table_score(person, table)
            if s is None:
                continue
            candidates.append((s, ti, pi))

    candidates.sort(key=lambda t: t[0], reverse=True)
    taken_persons: set[int] = set()
    sitters: dict[int, list[int]] = {i: [] for i in range(len(tables))}
    for _score, ti, pi in candidates:
        if pi in taken_persons:
            continue
        taken_persons.add(pi)
        sitters[ti].append(pi)

    filled = 0
    for ti, table in enumerate(tables):
        n = len(sitters[ti])
        table.sitter_count = n
        if n > 0:
            table.status = "filled"
            p0 = persons[sitters[ti][0]]
            table.seated_person_conf = p0.conf
            # Remember which track owns this table (for temporal).
            tid = getattr(p0, "track_id", None)
            setattr(table, "_sitter_track_id", tid)
            filled += 1
        else:
            table.status = "empty"
            table.seated_person_conf = None
            setattr(table, "_sitter_track_id", None)
    return filled, len(tables) - filled


def apply_temporal_to_tables(
    tables: list[BoxDet],
    temporal: Any,
    *,
    camera_key: str = "",
) -> None:
    """Update table.status using TemporalOccupancy (filled/empty/uncertain)."""
    if temporal is None:
        return
    for i, table in enumerate(tables):
        key = f"{camera_key}:t{i}"
        # Prefer stable zone/table id if stamped later; index is fine per-frame
        # until bind assigns zone — temporal hub also keyed by zone after bind.
        tid = getattr(table, "track_id", None)
        if tid is not None:
            key = f"{camera_key}:trk{tid}"
        cand = table.status == "filled"
        person_tid = getattr(table, "_sitter_track_id", None)
        st = temporal.update(
            key=key,
            candidate_filled=cand,
            person_track_id=person_tid if isinstance(person_tid, int) else None,
        )
        if st == "filled":
            table.status = "filled"
        elif st == "uncertain":
            # Hold as empty for zone timers until confirmed (avoid false start).
            table.status = "empty"
            table.sitter_count = 0
            setattr(table, "_uncertain", True)
        else:
            table.status = "empty"
            table.sitter_count = 0
