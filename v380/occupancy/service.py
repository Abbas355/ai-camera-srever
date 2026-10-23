"""Occupancy business logic — zones, face/YOLO occupancy, sessions."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from v380.attendance.face_engine import FaceEngine
from v380.occupancy.store import OccupancyStore
from v380.occupancy.yolo_detector import (
    OccupancyYolo,
    OccupancyYoloWorld,
    box_iou_norm,
)



JpegProvider = Callable[[int], bytes | None]





def point_in_polygon(x: float, y: float, points: list[list[float]]) -> bool:
    """Ray-casting; points are normalized [[nx, ny], ...] in 0..1."""
    if len(points) < 3:
        return False
    inside = False
    n = len(points)
    j = n - 1
    for i in range(n):
        xi, yi = float(points[i][0]), float(points[i][1])
        xj, yj = float(points[j][0]), float(points[j][1])
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def inflate_polygon(
    points: list[list[float]], *, frac: float = 0.18
) -> list[list[float]]:
    """Grow a zone polygon from its centroid so side-sitters still hit the table."""
    if len(points) < 3:
        return points
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    out: list[list[float]] = []
    scale = 1.0 + max(0.0, float(frac))
    for x, y in zip(xs, ys):
        out.append(
            [
                max(0.0, min(1.0, cx + (x - cx) * scale)),
                max(0.0, min(1.0, cy + (y - cy) * scale)),
            ]
        )
    return out





class OccupancyService:
    def __init__(self, store: OccupancyStore | None = None):
        self.store = store or OccupancyStore()
        self._lock = threading.Lock()
        self._last_error: str | None = None
        # zone_id -> last time a person/face was seen inside
        self._last_seen: dict[int, float] = {}
        self._empty_since: dict[int, float] = {}
        self._last_yolo: dict[str, Any] | None = None
        # camera_id -> zone_id -> (cx, cy) last matched center (stable ID lock)
        self._table_centers: dict[int, dict[int, tuple[float, float]]] = {}
        # camera_id -> zone_id -> consecutive frames with no matching desk detect
        self._zone_miss: dict[int, dict[int, int]] = {}



    def status(self) -> dict[str, Any]:
        ok, msg = FaceEngine.available()
        yolo = OccupancyYolo.instance()
        yolo_ok, yolo_msg = yolo.available()
        world = OccupancyYoloWorld.instance()
        world_ok, world_msg = world.available()
        # Warm models in background so first detect click is faster.
        if yolo_ok and yolo._model is None:
            threading.Thread(
                target=yolo.ensure_loaded,
                name="yolo-warmup",
                daemon=True,
            ).start()
        if world_ok and world._model is None:
            threading.Thread(
                target=world.ensure_loaded,
                name="yolo-world-warmup",
                daemon=True,
            ).start()
        from v380.occupancy.watcher import OccupancyWatcher

        settings = self.store.get_settings()
        zones = self.store.list_zones(active_only=True)
        live = []
        for z in zones:
            open_s = self.store.open_session(int(z["id"]))
            live.append(
                {
                    **z,
                    "status": (open_s or {}).get("status") or "free",
                    "since": (open_s or {}).get("started_at"),
                    "since_time": (open_s or {}).get("start_time"),
                    "duration_sec": (open_s or {}).get("duration_sec"),
                    "duration": (open_s or {}).get("duration"),
                }
            )
        return {
            "ok": True,
            "face_engine": {"available": ok, "message": msg},
            "yolo": {"available": yolo_ok, "message": yolo_msg},
            "yolo_world": {
                "available": world_ok,
                "message": world_msg,
                "weights": "yolov8s-world.pt",
                "prompts": ["office desk", "computer desk", "work desk"],
                "conf": 0.40,
            },
            "settings": settings,
            "watcher": OccupancyWatcher.instance().status(),
            "zones": live,
            "summary": self.store.summary_today(),
            "last_error": self._last_error,
            "last_yolo": self._last_yolo,
        }



    def get_settings(self) -> dict[str, Any]:
        return self.store.get_settings()



    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        # Only apply keys present in the request — otherwise camera_id was
        # cleared whenever threshold / sync_tables was updated alone.
        allowed = (
            "enabled",
            "camera_id",
            "poll_interval_sec",
            "empty_confirm_sec",
            "min_face_area",
            "detect_mode",
            "sync_tables",
            "yolo_conf",
        )
        patch = {k: body[k] for k in allowed if k in body}
        settings = self.store.update_settings(**patch)
        # Apply confidence immediately for next Detect / YOLO tick.
        try:
            OccupancyYolo.instance().conf = float(settings.get("yolo_conf") or 0.35)
        except Exception:
            pass
        # Manual detect only — keep occupancy poll loop stopped.
        from v380.occupancy.watcher import OccupancyWatcher

        OccupancyWatcher.instance().stop()
        return settings

    def _apply_yolo_conf(self) -> float:
        settings = self.store.get_settings()
        try:
            conf = float(settings.get("yolo_conf") or 0.35)
        except (TypeError, ValueError):
            conf = 0.35
        conf = max(0.05, min(0.95, conf))
        OccupancyYolo.instance().conf = conf
        return conf



    def list_zones(self) -> list[dict[str, Any]]:
        return self.store.list_zones(active_only=False)



    def add_zone(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.store.get_settings()
        cam = body.get("camera_id")
        if cam is None:
            cam = settings.get("camera_id")
        if cam is None:
            raise ValueError("Set an occupancy camera first")
        return self.store.add_zone(
            name=str(body.get("name") or ""),
            camera_id=int(cam),
            points=list(body.get("points") or []),
        )



    def update_zone(self, zone_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.update_zone(
            int(zone_id),
            name=body.get("name"),
            points=body.get("points"),
            active=body.get("active"),
        )



    def delete_zone(self, zone_id: int) -> bool:
        return self.store.delete_zone(int(zone_id))



    def list_sessions(self, *, day: str | None = None, zone_id: int | None = None) -> list[dict[str, Any]]:
        return self.store.list_sessions(day=day, zone_id=zone_id)



    def preview_detect(
        self,
        jpeg: bytes,
        *,
        tables: bool = True,
        chairs: bool = False,
        persons: bool = False,
        conf: float | None = None,
    ) -> dict[str, Any]:
        """One-shot YOLO preview — boxes only, does NOT save zones.

        Runs at a low floor confidence so the client can filter by the
        threshold slider without another round-trip.
        """
        if not jpeg:
            return {"ok": False, "error": "no jpeg"}
        ui_conf = self._apply_yolo_conf()
        # Low floor so the client threshold slider can refilter; persons need it
        # especially on dual-pane / corner sits.
        if conf is None:
            run_conf = 0.03 if (persons or chairs) else 0.05
        else:
            run_conf = max(0.03, min(0.95, float(conf)))
        # Manual detect: only run what the client asked for (no forced table/chair/person).
        want_persons = bool(persons)
        want_chairs = bool(chairs)
        # Occupancy association needs persons (+ optional chairs) with tables.
        if tables and persons:
            want_persons = True
        cam = self.store.get_settings().get("camera_id")
        cam_id = int(cam) if cam is not None else None
        det = OccupancyYolo.instance().detect_jpeg(
            jpeg,
            tables=tables,
            chairs=want_chairs,
            persons=want_persons,
            conf=run_conf,
            camera_id=cam_id,
        )
        payload = det.to_dict(
            camera_id=cam_id,
            use_association=bool(tables and want_persons),
            apply_temporal=False,
        )
        # Preview only — never auto-create / wipe zones (manual Zone / Detect save).
        self._last_yolo = payload
        if det.error:
            self._last_error = det.error
            return {
                "ok": False,
                "error": det.error,
                "detect": payload,
                "yolo_conf": ui_conf,
            }
        self._last_error = None
        uncertain_n = sum(
            1
            for t in (payload.get("tables") or [])
            if isinstance(t, dict) and t.get("uncertain")
        )
        return {
            "ok": True,
            "yolo_conf": ui_conf,
            "preview_conf": run_conf,
            "saved": False,
            "detect": payload,
            "table_detector": "yolo-world" if tables else None,
            "chair_person_detector": "yolov8n" if (want_chairs or want_persons) else None,
            "association": "person-chair-table" if tables and want_persons else None,
            "tracking": "iou-bytetrack-style",
            "chairs_occupied": payload.get("chairs_occupied", 0),
            "chairs_empty": payload.get("chairs_empty", 0),
            "tables_filled": payload.get("tables_filled", 0),
            "tables_empty": payload.get("tables_empty", 0),
            "tables_uncertain": uncertain_n,
            "people_at_tables": payload.get("people_at_tables", 0),
        }

    def detect_tables(
        self,
        jpeg: bytes,
        *,
        camera_id: int,
        create_missing: bool = False,
        update_matched: bool = False,
        replace_all: bool = False,
        save: bool = False,
    ) -> dict[str, Any]:
        """Detect tables in this frame. Default: preview only (no save)."""
        if not jpeg:
            return {"ok": False, "error": "no jpeg"}
        # Preview path (default) — just show tables.
        if not save and not create_missing and not update_matched and not replace_all:
            return self.preview_detect(jpeg, tables=True, chairs=False, persons=False)

        conf = self._apply_yolo_conf()
        det = OccupancyYolo.instance().detect_jpeg(
            jpeg, tables=True, chairs=False, persons=False
        )
        self._last_yolo = det.to_dict()
        if det.error:
            self._last_error = det.error
            return {"ok": False, "error": det.error, "detect": det.to_dict()}

        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        existing = self.store.list_zones(camera_id=int(camera_id), active_only=True)

        if replace_all:
            for z in list(existing):
                self.store.delete_zone(int(z["id"]))
            existing = []

        used_zone_ids: set[int] = set()
        for table in det.tables:
            pts = table.as_norm_rect(float(det.frame_w), float(det.frame_h))
            if len(pts) < 3:
                continue
            best_id = None
            best_iou = 0.0
            for z in existing:
                zid = int(z["id"])
                if zid in used_zone_ids:
                    continue
                iou = box_iou_norm(pts, z.get("points") or [])
                if iou > best_iou:
                    best_iou = iou
                    best_id = zid
            if best_id is not None and best_iou >= 0.15 and update_matched:
                zone = self.store.update_zone(best_id, points=pts)
                used_zone_ids.add(best_id)
                if zone:
                    updated.append(zone)
            elif (save or create_missing) and (best_id is None or best_iou < 0.15):
                zone = self.store.add_zone(
                    name="",
                    camera_id=int(camera_id),
                    points=pts,
                )
                used_zone_ids.add(int(zone["id"]))
                created.append(zone)

        self._last_error = None
        return {
            "ok": True,
            "yolo_conf": conf,
            "saved": bool(created or updated),
            "detect": det.to_dict(),
            "created": created,
            "updated": updated,
            "zones": self.store.list_zones(camera_id=int(camera_id), active_only=True),
        }



    def process_frame(self, jpeg: bytes, *, camera_id: int) -> dict[str, Any]:
        """Detect occupancy for zones on this camera (face or YOLO mode)."""
        if not jpeg:
            return {"ok": False, "error": "no jpeg"}
        settings = self.store.get_settings()
        mode = str(settings.get("detect_mode") or "face")
        if mode == "yolo":
            return self._process_yolo(jpeg, camera_id=int(camera_id), settings=settings)
        return self._process_face(jpeg, camera_id=int(camera_id), settings=settings)



    def _process_face(
        self, jpeg: bytes, *, camera_id: int, settings: dict[str, Any]
    ) -> dict[str, Any]:
        min_area = float(settings["min_face_area"])
        empty_confirm = float(settings["empty_confirm_sec"])
        zones = [
            z
            for z in self.store.list_zones(camera_id=int(camera_id), active_only=True)
        ]
        if not zones:
            return {"ok": True, "mode": "face", "zones": 0, "faces": 0, "changes": []}



        with self._lock:
            try:
                hits = FaceEngine.detect_jpeg(
                    jpeg, min_area=min_area, min_det_score=0.32, min_face_frac=0.0004
                )
            except Exception as exc:
                self._last_error = str(exc)
                return {"ok": False, "error": str(exc), "mode": "face"}



            frame_w, frame_h = _jpeg_size(jpeg)
            centers: list[tuple[float, float]] = []
            if frame_w > 1 and frame_h > 1:
                for h in hits:
                    x1, y1, x2, y2 = h.bbox
                    cx = ((x1 + x2) / 2.0) / frame_w
                    cy = ((y1 + y2) / 2.0) / frame_h + 0.04
                    centers.append((cx, cy))



            return self._apply_occupancy(
                zones,
                centers,
                empty_confirm=empty_confirm,
                mode="face",
                hit_count=len(hits),
            )



    @staticmethod
    def _poly_center(pts: list[list[float]]) -> tuple[float, float]:
        xs = [float(p[0]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        ys = [float(p[1]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not xs or not ys:
            return 0.5, 0.5
        return sum(xs) / len(xs), sum(ys) / len(ys)

    @staticmethod
    def _stamp_table_label(td: dict[str, Any], *, name: str, zone_id: int) -> None:
        """Put stable T## on the live box label (occupied / free)."""
        td["table_id"] = int(zone_id)
        td["name"] = name
        td["zone_id"] = int(zone_id)
        st = str(td.get("status") or "").lower()
        occ = st in {"occupied", "filled"} or td.get("occupied") is True
        sit = td.get("sitters")
        n = int(sit) if isinstance(sit, (int, float)) else 0
        if occ:
            td["status"] = "occupied"
            td["occupied"] = True
            td["label"] = f"{name} · occupied" + (f" · {n}" if n > 0 else "")
        else:
            td["status"] = "empty"
            td["occupied"] = False
            td["label"] = f"{name} · free"

    def _bind_tables_to_zones(
        self,
        *,
        camera_id: int,
        table_dicts: list[dict[str, Any]],
        create_missing: bool = True,
        min_conf: float = 0.40,
        max_zones: int = 8,
    ) -> dict[int, bool]:
        """Match each YOLO table to exactly one zone ID.

        - Existing zone keeps its T## forever (not reassigned to another desk)
        - New desks get a new unique T## (only at high conf — avoids wall junk)
        - 1:1 matching by IoU + last center (prevents ID swap)
        Returns zone_id -> occupied for this frame.
        """
        cam = int(camera_id)
        existing = self.store.list_zones(camera_id=cam, active_only=True)
        centers = self._table_centers.setdefault(cam, {})
        zone_occ: dict[int, bool] = {}

        # Build candidate scores: (score, table_i, zone_id)
        pairs: list[tuple[float, int, int]] = []
        for ti, td in enumerate(table_dicts):
            pts = td.get("points") or []
            if len(pts) < 3:
                # Fallback from norm box.
                norm = td.get("norm")
                if isinstance(norm, list) and len(norm) >= 4:
                    x1, y1, x2, y2 = (float(norm[0]), float(norm[1]), float(norm[2]), float(norm[3]))
                    pts = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                    td["points"] = pts
                else:
                    continue
            tcx, tcy = self._poly_center(pts)
            for z in existing:
                zid = int(z["id"])
                zpts = z.get("points") or []
                iou = box_iou_norm(pts, zpts)
                # Prefer continuity with last matched center for this zone.
                bonus = 0.0
                prev = centers.get(zid)
                if prev is not None:
                    dx = tcx - prev[0]
                    dy = tcy - prev[1]
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist < 0.12:
                        bonus = 0.25 * (1.0 - dist / 0.12)
                score = iou + bonus
                if score >= 0.10:
                    pairs.append((score, ti, zid))

        pairs.sort(key=lambda t: t[0], reverse=True)
        used_tables: set[int] = set()
        used_zones: set[int] = set()
        assigned: dict[int, int] = {}  # table_i -> zone_id

        for score, ti, zid in pairs:
            if ti in used_tables or zid in used_zones:
                continue
            # Require real overlap unless center continuity is strong.
            pts = table_dicts[ti].get("points") or []
            z = next((x for x in existing if int(x["id"]) == zid), None)
            iou = box_iou_norm(pts, (z or {}).get("points") or [])
            if iou < 0.08 and score < 0.28:
                continue
            used_tables.add(ti)
            used_zones.add(zid)
            assigned[ti] = zid

        # Apply matches — update geometry gently; never rename.
        for ti, zid in assigned.items():
            td = table_dicts[ti]
            pts = td.get("points") or []
            z = next((x for x in existing if int(x["id"]) == zid), None)
            name = str((z or {}).get("name") or f"T{zid:02d}")
            iou = box_iou_norm(pts, (z or {}).get("points") or [])
            # Only nudge points when clearly the same desk (avoids stealing nearby table).
            if 0.12 <= iou < 0.92 and pts:
                self.store.update_zone(zid, points=pts)
            tcx, tcy = self._poly_center(pts)
            centers[zid] = (tcx, tcy)
            filled = bool(
                td.get("occupied")
                or str(td.get("status") or "").lower() in {"occupied", "filled"}
            )
            zone_occ[zid] = filled
            self._stamp_table_label(td, name=name, zone_id=zid)

        # New desks → brand-new unique T## (never reuse another table's id).
        if create_missing:
            for ti, td in enumerate(table_dicts):
                if ti in used_tables:
                    continue
                try:
                    c = float(td.get("conf") or 0.0)
                except (TypeError, ValueError):
                    c = 0.0
                if c < float(min_conf):
                    continue
                if len(existing) >= int(max_zones):
                    break
                pts = td.get("points") or []
                if len(pts) < 3:
                    continue
                # Skip if heavily overlapping an already-used zone (duplicate detect).
                overlap_existing = False
                for z in existing:
                    if box_iou_norm(pts, z.get("points") or []) >= 0.25:
                        overlap_existing = True
                        break
                if overlap_existing:
                    continue
                zone = self.store.add_zone(name="", camera_id=cam, points=pts)
                zid = int(zone["id"])
                name = str(zone.get("name") or self.store.next_zone_name())
                existing.append(zone)
                used_tables.add(ti)
                used_zones.add(zid)
                tcx, tcy = self._poly_center(pts)
                centers[zid] = (tcx, tcy)
                filled = bool(
                    td.get("occupied")
                    or str(td.get("status") or "").lower() in {"occupied", "filled"}
                )
                zone_occ[zid] = filled
                self._stamp_table_label(td, name=name, zone_id=zid)

        # Drop stale junk zones (walls/posters) that never match a real desk.
        if table_dicts:
            misses = self._zone_miss.setdefault(cam, {})
            for z in list(existing):
                zid = int(z["id"])
                if zid in used_zones:
                    misses[zid] = 0
                    continue
                misses[zid] = int(misses.get(zid, 0)) + 1
                if misses[zid] >= 2:
                    try:
                        self.store.update_zone(zid, active=False)
                    except Exception:
                        pass
                    misses.pop(zid, None)
                    centers.pop(zid, None)
                    zone_occ.pop(zid, None)

        return zone_occ

    def dedupe_overlapping_zones(self, camera_id: int, *, iou_thresh: float = 0.18) -> int:
        """Merge duplicate zones on the same desk (keep lowest id, deactivate rest)."""
        cam = int(camera_id)
        zones = self.store.list_zones(camera_id=cam, active_only=True)
        if len(zones) < 2:
            return 0
        # Sort by id ascending — keep the oldest.
        zones = sorted(zones, key=lambda z: int(z["id"]))
        removed = 0
        alive: list[dict[str, Any]] = []
        for z in zones:
            pts = z.get("points") or []
            drop = False
            for kept in alive:
                iou = box_iou_norm(pts, kept.get("points") or [])
                if iou >= float(iou_thresh):
                    drop = True
                    break
                # Also merge if centers are very close (split long desk).
                c1 = self._poly_center(pts)
                c2 = self._poly_center(kept.get("points") or [])
                dist = ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2) ** 0.5
                if dist < 0.12 and iou >= 0.08:
                    drop = True
                    break
            if drop:
                try:
                    self.store.update_zone(int(z["id"]), active=False)
                    removed += 1
                except Exception:
                    pass
            else:
                alive.append(z)
        return removed

    def _process_yolo(
        self, jpeg: bytes, *, camera_id: int, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Live table free/occupied every tick — detect desks + people, timers on zones."""
        empty_confirm = float(settings["empty_confirm_sec"])
        conf = self._apply_yolo_conf()

        with self._lock:
            # Manual detect only for table boxes. Watcher: persons vs saved zones.
            det = OccupancyYolo.instance().detect_jpeg(
                jpeg,
                tables=False,
                chairs=False,
                persons=True,
                conf=conf,
                camera_id=int(camera_id),
            )
            payload = det.to_dict(
                camera_id=int(camera_id),
                use_association=False,
                apply_temporal=False,
            )
            self._last_yolo = payload
            if det.error and not det.persons:
                self._last_error = det.error
                return {
                    "ok": False,
                    "error": det.error,
                    "mode": "yolo",
                    "detect": payload,
                }

            zones = self.store.list_zones(
                camera_id=int(camera_id), active_only=True
            )
            w, h = float(det.frame_w), float(det.frame_h)
            hit_points: list[tuple[float, float]] = []
            if w > 1 and h > 1:
                for p in det.persons:
                    hit_points.append(p.norm_foot(w, h))
                    hit_points.append(
                        (
                            max(0.0, min(1.0, p.seat_x / w)),
                            max(0.0, min(1.0, p.seat_y / h)),
                        )
                    )

            out = self._apply_occupancy(
                zones,
                hit_points,
                empty_confirm=empty_confirm,
                mode="yolo",
                hit_count=len(det.persons),
                inflate_frac=0.18,
            )
            out["tables"] = 0
            out["persons"] = len(det.persons)
            out["detect"] = payload
            out["live_tables"] = []
            return out



    def _apply_occupancy(
        self,
        zones: list[dict[str, Any]],
        centers: list[tuple[float, float]],
        *,
        empty_confirm: float,
        mode: str,
        hit_count: int,
        inflate_frac: float = 0.0,
        occupied_override: dict[int, bool] | None = None,
    ) -> dict[str, Any]:
        """Flip zones free↔occupied and open/close session timers."""
        now = time.time()
        changes: list[dict[str, Any]] = []
        states: list[dict[str, Any]] = []
        override = occupied_override or {}
        for z in zones:
            zid = int(z["id"])
            pts = z.get("points") or []
            hit_poly = (
                inflate_polygon(pts, frac=inflate_frac)
                if inflate_frac > 0
                else pts
            )
            if zid in override:
                occupied_now = bool(override[zid])
            else:
                occupied_now = any(
                    point_in_polygon(cx, cy, hit_poly) for cx, cy in centers
                )
            open_s = self.store.open_session(zid)
            cur = (open_s or {}).get("status") or "free"

            if occupied_now:
                self._last_seen[zid] = now
                self._empty_since.pop(zid, None)
                if cur != "occupied":
                    # Timer starts here (new occupied session).
                    sess = self.store.set_status(zid, "occupied")
                    changes.append(
                        {"zone_id": zid, "name": z["name"], "status": "occupied"}
                    )
                    cur = "occupied"
                    open_s = sess
            else:
                if zid not in self._empty_since:
                    self._empty_since[zid] = now
                empty_for = now - self._empty_since[zid]
                if cur == "occupied" and empty_for >= empty_confirm:
                    # Timer stops here (occupied session closed).
                    sess = self.store.set_status(zid, "free")
                    changes.append(
                        {"zone_id": zid, "name": z["name"], "status": "free"}
                    )
                    cur = "free"
                    open_s = sess
                elif cur == "free":
                    self._empty_since[zid] = now

            states.append(
                {
                    "zone_id": zid,
                    "name": z["name"],
                    "status": cur,
                    "hits_inside": occupied_now,
                    "duration": (open_s or {}).get("duration"),
                    "duration_sec": (open_s or {}).get("duration_sec"),
                    "since": (open_s or {}).get("started_at"),
                }
            )

        self._last_error = None
        key = "persons" if mode == "yolo" else "faces"
        return {
            "ok": True,
            "mode": mode,
            key: hit_count,
            "zones": len(zones),
            "changes": changes,
            "states": states,
        }





def _jpeg_size(jpeg: bytes) -> tuple[float, float]:
    if not jpeg:
        return 0.0, 0.0
    try:
        import cv2
        import numpy as np



        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return 0.0, 0.0
        h, w = img.shape[:2]
        return float(w), float(h)
    except Exception:
        return 0.0, 0.0





_service: OccupancyService | None = None
_svc_lock = threading.Lock()





def get_service() -> OccupancyService:
    global _service
    with _svc_lock:
        if _service is None:
            _service = OccupancyService()
        return _service