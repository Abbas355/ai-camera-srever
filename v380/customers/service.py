"""Customer visit business logic — face match, visits, tables. Isolated feature."""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from v380.attendance.face_engine import FaceEngine, FaceEngineError, FaceHit
from v380.customers.store import CustomerStore

JpegProvider = Callable[[int], bytes | None]
CameraIdsProvider = Callable[[], list[int]]


@dataclass
class _PendingFace:
    """Unknown face waiting for a few consistent sightings before auto-enroll."""

    embedding: np.ndarray
    hits: int = 1
    last_seen: float = field(default_factory=time.time)
    photo: bytes = b""
    best_area: float = 0.0
    best_det: float = 0.0


@dataclass
class _ForgottenFace:
    """Recently deleted guest face — allow clean re-enroll next time they appear."""

    embedding: np.ndarray
    deleted_at: float = field(default_factory=time.time)


class CustomerService:
    def __init__(self, store: CustomerStore | None = None):
        self.store = store or CustomerStore()
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._pending: list[_PendingFace] = []
        self._pending_ttl_sec = 90.0
        # After delete, remember the face so soft re-id does not attach them to
        # another guest, and so clear-face / confirm gates do not block re-enroll.
        self._forgotten: list[_ForgottenFace] = []
        self._forgotten_ttl_sec = 48 * 3600.0
        self._forgotten_max = 80

    def status(self) -> dict[str, Any]:
        ok, msg = FaceEngine.available()
        from v380.customers.watcher import CustomerWatcher

        return {
            "ok": True,
            "face_engine": {"available": ok, "message": msg},
            "settings": self.store.get_settings(),
            "watcher": CustomerWatcher.instance().status(),
            "summary": self.store.summary_today(),
            "last_error": self._last_error,
            "pending_enrolls": len(self._pending),
        }

    def get_settings(self) -> dict[str, Any]:
        return self.store.get_settings()

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.store.update_settings(
            enabled=body.get("enabled"),
            match_threshold=body.get("match_threshold"),
            soft_match_threshold=body.get("soft_match_threshold"),
            staff_exclude_threshold=body.get("staff_exclude_threshold"),
            away_sec=body.get("away_sec"),
            poll_interval_sec=body.get("poll_interval_sec"),
            auto_enroll=body.get("auto_enroll"),
            min_face_area=body.get("min_face_area"),
            min_face_frac=body.get("min_face_frac"),
            min_det_score=body.get("min_det_score"),
            enroll_confirm_hits=body.get("enroll_confirm_hits"),
            max_faces_per_frame=body.get("max_faces_per_frame"),
            enroll_min_area=body.get("enroll_min_area"),
            enroll_min_det=body.get("enroll_min_det"),
            enroll_max_yaw=body.get("enroll_max_yaw"),
            enroll_max_pitch=body.get("enroll_max_pitch"),
            camera_ids=body.get("camera_ids"),
        )
        from v380.customers.watcher import CustomerWatcher

        # When turning tracking on, drop any guests that match staff faces.
        if settings.get("enabled"):
            try:
                self.purge_staff_lookalikes()
            except Exception:
                pass
        CustomerWatcher.instance().apply_settings(settings)
        return settings

    def list_customers(self) -> list[dict[str, Any]]:
        return self.store.list_customers()

    def get_customer(self, customer_id: int) -> dict[str, Any] | None:
        return self.store.get_customer(customer_id)

    def rename(self, customer_id: int, name: str) -> dict[str, Any] | None:
        return self.store.update_customer(customer_id, name=name)

    def delete(self, customer_id: int) -> bool:
        """Hard-delete a guest and remember their face so they can be re-enrolled."""
        cid = int(customer_id)
        emb_blob = self.store.get_embedding(cid)
        ok = self.store.delete_customer(cid)
        if not ok:
            return False
        with self._lock:
            if emb_blob:
                emb = FaceEngine.bytes_to_embedding(emb_blob)
                self._forgotten.append(_ForgottenFace(embedding=emb.copy()))
                if len(self._forgotten) > self._forgotten_max:
                    self._forgotten = self._forgotten[-self._forgotten_max :]
                # Drop in-flight pending that was this same person.
                self._pending = [
                    p
                    for p in self._pending
                    if FaceEngine.cosine(emb, p.embedding) < 0.28
                ]
        return True

    def enroll(
        self,
        *,
        name: str,
        image_base64: str | None = None,
        jpeg: bytes | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        data = jpeg or self._decode_b64(image_base64)
        if not data:
            raise FaceEngineError("Provide a face photo (image_base64)")
        emb = FaceEngine.embed_jpeg(data)
        # Manual enroll still must not create a staff doppelgänger.
        staff_score = self._best_staff_score(emb, self._staff_embeddings())
        if staff_score >= 0.22:
            raise FaceEngineError(
                f"This face matches enrolled staff (score {staff_score:.2f}). "
                "Staff are tracked under Attendance, not Customers."
            )
        return self.store.add_customer(
            name=name or self.store.next_guest_name(),
            embedding=FaceEngine.embedding_to_bytes(emb),
            photo_bytes=data,
            notes=notes,
        )

    def customer_detail(self, customer_id: int) -> dict[str, Any] | None:
        cust = self.store.get_customer(customer_id)
        if cust is None:
            return None
        return {
            "ok": True,
            "customer": cust,
            "visits": self.store.list_visits(customer_id),
            "sightings_today": self.store.list_sightings(customer_id=customer_id),
            "open_visit": self.store.open_visit(customer_id),
        }

    def set_visit_table(self, visit_id: int, table_id: str | None) -> dict[str, Any] | None:
        table_name = None
        if table_id:
            try:
                from v380.tables.store import TableStore

                t = TableStore().get(str(table_id))
                if t is not None:
                    table_name = t.name
                    table_id = t.id
            except Exception:
                table_name = str(table_id)
        return self.store.set_visit_table(visit_id, table_id, table_name)

    def open_visits(self) -> list[dict[str, Any]]:
        return self.store.list_open_visits_today()

    def playback_for_visit(self, visit_id: int) -> dict[str, Any]:
        """Build a follow-the-guest timeline: camera segments from sightings."""
        with self.store._lock:
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM customer_visits WHERE id=?", (int(visit_id),)
                ).fetchone()
                sight_rows = conn.execute(
                    "SELECT camera_id, created_at FROM customer_sightings "
                    "WHERE visit_id=? AND camera_id IS NOT NULL "
                    "ORDER BY created_at ASC, id ASC",
                    (int(visit_id),),
                ).fetchall()
        if row is None:
            raise LookupError("Visit not found")
        visit = self.store._visit_row(row)

        start_raw = visit.get("started_at") or ""
        end_raw = visit.get("ended_at") or visit.get("last_seen_at") or start_raw
        start_ts = _parse_visit_ts(start_raw)
        end_ts = _parse_visit_ts(end_raw) or start_ts
        if start_ts is None:
            raise ValueError("Visit has no valid start time")
        if end_ts is None or end_ts < start_ts:
            end_ts = start_ts

        date = start_ts.strftime("%Y-%m-%d")
        start_sec = _day_sec(start_ts)
        if end_ts.strftime("%Y-%m-%d") != date:
            end_sec = 86399
        else:
            end_sec = _day_sec(end_ts)
        if end_sec <= start_sec:
            end_sec = min(86399, start_sec + 60)

        # Collapse consecutive same-camera sightings into play segments.
        events: list[tuple[int, int]] = []  # (day_sec, camera_id)
        for sr in sight_rows:
            ts = _parse_visit_ts(sr["created_at"])
            if ts is None:
                continue
            if ts.strftime("%Y-%m-%d") != date:
                continue
            sec = _day_sec(ts)
            if sec < start_sec:
                sec = start_sec
            if sec > end_sec:
                continue
            cam = int(sr["camera_id"])
            if events and events[-1][1] == cam:
                continue
            events.append((sec, cam))

        fallback_cam = visit.get("camera_id")
        if not events:
            if fallback_cam is None:
                raise ValueError("This visit has no camera — no recording to open")
            events = [(start_sec, int(fallback_cam))]
        elif events[0][0] > start_sec:
            # Cover time before first sighting with visit camera or first sighting cam.
            head_cam = int(fallback_cam) if fallback_cam is not None else events[0][1]
            if head_cam == events[0][1]:
                events[0] = (start_sec, head_cam)
            else:
                events.insert(0, (start_sec, head_cam))

        from v380.paths import REC_DIR
        from v380.record.auto_record import (
            day_has_media,
            resolve_recording_folder_for_day,
        )
        from v380.store.camera_store import CameraStore

        cam_store = CameraStore()
        cam_cache: dict[int, tuple[str, str, bool]] = {}

        def _resolve(cid: int) -> tuple[str, str, bool] | None:
            if cid in cam_cache:
                return cam_cache[cid]
            cam = cam_store.get(int(cid))
            if cam is None:
                return None
            folder = resolve_recording_folder_for_day(cam, date)
            name = getattr(cam, "name", folder) or folder
            has_media = day_has_media(REC_DIR / folder / date)
            cam_cache[cid] = (folder, name, has_media)
            return folder, name, has_media

        segments: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for i, (sec, cid) in enumerate(events):
            next_sec = events[i + 1][0] if i + 1 < len(events) else end_sec
            if next_sec <= sec:
                next_sec = min(86399, sec + 1)
            resolved = _resolve(int(cid))
            if resolved is None:
                skipped.append({"camera_id": int(cid), "reason": "camera_missing"})
                continue
            folder, name, has_media = resolved
            segments.append(
                {
                    "camera_id": int(cid),
                    "camera_folder": folder,
                    "camera_name": name,
                    "date": date,
                    "start_sec": int(sec),
                    "end_sec": int(next_sec),
                    "has_media": bool(has_media),
                }
            )

        if not segments:
            raise ValueError(
                "No playable cameras for this visit "
                "(camera missing from server or no recording folders)"
            )

        cust = self.store.get_customer(int(visit["customer_id"]))
        first = segments[0]
        track = self._track_for_visit(int(visit_id), date=date, start_sec=start_sec, end_sec=end_sec)
        return {
            "ok": True,
            "visit_id": int(visit_id),
            "customer_id": int(visit["customer_id"]),
            "customer_name": (cust or {}).get("name") or "Guest",
            "camera_id": first["camera_id"],
            "camera_folder": first["camera_folder"],
            "camera_name": first["camera_name"],
            "date": date,
            "start_sec": int(start_sec),
            "end_sec": int(end_sec),
            "started_at": start_raw,
            "ended_at": visit.get("ended_at"),
            "last_seen_at": visit.get("last_seen_at"),
            "status": visit.get("status"),
            "segments": segments,
            "skipped": skipped,
            "track": track,
            "track_count": len(track),
        }

    def _track_for_visit(
        self,
        visit_id: int,
        *,
        date: str,
        start_sec: int,
        end_sec: int,
    ) -> list[dict[str, Any]]:
        """Normalized face boxes over time for the visit player overlay."""
        rows = self.store.list_sightings(visit_id=int(visit_id), day=date, limit=1000)
        # list_sightings is DESC — reverse to time order.
        rows = list(reversed(rows))
        track: list[dict[str, Any]] = []
        for s in rows:
            bbox = s.get("bbox")
            if not bbox or not s.get("camera_id"):
                continue
            ts = _parse_visit_ts(s.get("created_at"))
            if ts is None:
                continue
            if ts.strftime("%Y-%m-%d") != date:
                continue
            sec = _day_sec(ts)
            if sec < start_sec - 2 or sec > end_sec + 2:
                continue
            track.append(
                {
                    "day_sec": int(sec),
                    "camera_id": int(s["camera_id"]),
                    "score": s.get("score"),
                    "nx1": float(bbox["nx1"]),
                    "ny1": float(bbox["ny1"]),
                    "nx2": float(bbox["nx2"]),
                    "ny2": float(bbox["ny2"]),
                    "created_at": s.get("created_at"),
                }
            )
        return track

    def process_camera_jpeg(self, camera_id: int, jpeg: bytes) -> list[dict[str, Any]]:
        """Match / enroll faces on one frame; update visits. Returns actions taken."""
        if not jpeg:
            return []
        settings = self.store.get_settings()
        hard = float(settings["match_threshold"])
        soft = float(settings["soft_match_threshold"])
        soft = min(soft, hard - 0.02)
        # Low floor so front↔side of the same person rematches (often ~0.22–0.30).
        reid_floor = max(0.20, soft - 0.04)
        staff_th = float(settings["staff_exclude_threshold"])
        # Only block new guests on a fairly confident staff match.
        staff_enroll_th = max(0.26, min(staff_th, 0.32))
        min_area = float(settings["min_face_area"])
        min_frac = float(settings["min_face_frac"])
        min_det = float(settings["min_det_score"])
        auto_enroll = bool(settings["auto_enroll"])
        confirm_hits = int(settings["enroll_confirm_hits"])
        max_faces = int(settings["max_faces_per_frame"])
        enroll_min_area = float(settings["enroll_min_area"])
        enroll_min_det = float(settings["enroll_min_det"])
        enroll_max_yaw = float(settings["enroll_max_yaw"])
        enroll_max_pitch = float(settings.get("enroll_max_pitch") or max(enroll_max_yaw, 45.0))
        results: list[dict[str, Any]] = []

        frame_w, frame_h = _jpeg_size(jpeg)
        with self._lock:
            try:
                hits = FaceEngine.detect_jpeg(
                    jpeg,
                    min_area=min_area,
                    min_det_score=min_det,
                    min_face_frac=min_frac,
                )
            except Exception as exc:
                self._last_error = str(exc)
                return []

            hits = hits[:max_faces]
            self._expire_pending()
            staff_embs = self._staff_embeddings()
            known = self.store.list_embeddings()
            table_id, table_name = self._table_hint_for_camera(camera_id)

            self._expire_forgotten()
            for hit in hits:
                emb = hit.embedding
                # Never treat enrolled staff as customers.
                staff_score = self._best_staff_score(emb, staff_embs)
                if staff_score >= staff_th:
                    results.append({"skipped": "staff", "score": round(staff_score, 4)})
                    continue

                best_id: int | None = None
                best_score = -1.0
                for cid, blob in known:
                    score = FaceEngine.cosine(emb, FaceEngine.bytes_to_embedding(blob))
                    if score > best_score:
                        best_score = score
                        best_id = cid

                # Deleted guests must be allowed to enroll again. Soft re-id to a
                # *different* remaining guest was swallowing them after delete.
                forgotten_score = self._best_forgotten_score(emb)
                reenroll = False
                if forgotten_score >= soft and (best_id is None or best_score < hard):
                    best_id = None
                    best_score = -1.0
                    reenroll = True

                customer = None
                enrolled_new = False
                if best_id is not None and best_score >= reid_floor:
                    # Same known guest (soft / hard / near-soft) — never a new Guest.
                    customer = self.store.get_customer(best_id)
                    if customer is None:
                        # Stale id / race — fall through to enroll.
                        best_id = None
                        best_score = -1.0
                    else:
                        # Blend + keep extra angles so the next side/front view rematches.
                        weight = 0.35 if best_score >= hard else 0.22
                        self._blend_gallery(
                            best_id, emb, jpeg, hit, known, new_weight=weight
                        )
                        if best_score < hard:
                            self._maybe_add_angle(best_id, emb, hit, known)
                if customer is None and auto_enroll and (
                    best_id is None or best_score < reid_floor
                ):
                    if staff_score >= staff_enroll_th:
                        results.append(
                            {"skipped": "staff_soft", "score": round(staff_score, 4)}
                        )
                        continue
                    # Re-enroll after delete: 1 hit; otherwise need confirm_hits.
                    need_hits = 1 if reenroll else confirm_hits
                    gate_area = min(enroll_min_area, 900.0) if reenroll else enroll_min_area
                    gate_det = min(enroll_min_det, 0.40) if reenroll else enroll_min_det
                    gate_yaw = max(enroll_max_yaw, 45.0) if reenroll else enroll_max_yaw
                    gate_pitch = max(enroll_max_pitch, 50.0) if reenroll else enroll_max_pitch
                    clear_enough = hit.is_clear_for_enroll(
                        min_area=gate_area,
                        min_det=gate_det,
                        max_yaw=gate_yaw,
                        max_pitch=gate_pitch,
                    )
                    # Always accumulate unknowns (even looking-down). Finalize only when
                    # we have a good enough crop, or enough weak hits for a soft enroll.
                    # Pitch is unreliable on CCTV (looking at table); don't over-block.
                    soft_enroll_ok = (
                        hit.area >= max(500.0, gate_area * 0.5)
                        and hit.det_score >= max(0.32, gate_det - 0.10)
                        and abs(hit.yaw) <= gate_yaw + 20.0
                    )
                    customer = self._confirm_and_enroll(
                        emb,
                        jpeg,
                        hit,
                        soft=soft,
                        reid_floor=reid_floor,
                        staff_th=staff_enroll_th,
                        staff_embs=staff_embs,
                        confirm_hits=need_hits,
                        known=known,
                        skip_known_rematch=reenroll,
                        allow_finalize=clear_enough or soft_enroll_ok,
                        force_finalize_hits=(
                            need_hits + 1 if not (clear_enough or soft_enroll_ok) else need_hits
                        ),
                    )
                    if customer is None:
                        results.append(
                            {
                                "matched": False,
                                "pending": True,
                                "score": round(best_score, 4) if best_score >= 0 else None,
                                "reenroll": reenroll,
                                "area": round(hit.area, 1),
                                "yaw": round(hit.yaw, 1),
                                "pitch": round(hit.pitch, 1),
                            }
                        )
                        continue
                    enrolled_new = True
                    best_score = 1.0
                    if reenroll:
                        self._forget_match(emb)
                elif customer is None:
                    results.append(
                        {
                            "matched": False,
                            "score": round(best_score, 4) if best_score >= 0 else None,
                        }
                    )
                    continue

                if customer is None:
                    continue

                cid = int(customer["id"])
                visit = self.store.open_visit(cid)
                if visit is None:
                    visit = self.store.start_visit(
                        cid,
                        camera_id=camera_id,
                        table_id=table_id,
                        table_name=table_name,
                    )
                    action = "visit_start" if not enrolled_new else "enroll_visit"
                else:
                    visit = self.store.touch_visit(
                        int(visit["id"]),
                        camera_id=camera_id,
                        table_id=table_id,
                        table_name=table_name,
                    ) or visit
                    action = "visit_update"

                sighting = self.store.add_sighting(
                    customer_id=cid,
                    visit_id=int(visit["id"]),
                    camera_id=camera_id,
                    score=round(float(best_score), 4),
                    table_id=table_id or visit.get("table_id"),
                    table_name=table_name or visit.get("table_name"),
                    bbox=hit.bbox if hit else None,
                    frame_size=(frame_w, frame_h) if frame_w > 0 and frame_h > 0 else None,
                )
                results.append(
                    {
                        "matched": True,
                        "action": action,
                        "customer": customer,
                        "visit": visit,
                        "sighting": sighting,
                        "score": round(float(best_score), 4),
                        "hard_match": best_score >= hard,
                    }
                )
            self._last_error = None
        return results

    def close_stale(self) -> int:
        settings = self.store.get_settings()
        return self.store.close_stale_visits(int(settings["away_sec"]))

    def purge_staff_lookalikes(self) -> dict[str, Any]:
        """Deactivate customers that match attendance staff; merge near-duplicate guests."""
        settings = self.store.get_settings()
        staff_th = float(settings["staff_exclude_threshold"])
        soft = float(settings["soft_match_threshold"])
        staff_embs = self._staff_embeddings()
        removed: list[dict[str, Any]] = []
        if staff_embs:
            for cid, blob in self.store.list_embeddings():
                emb = FaceEngine.bytes_to_embedding(blob)
                score = self._best_staff_score(emb, staff_embs)
                if score >= staff_th:
                    cust = self.store.get_customer(cid)
                    self.store.update_customer(cid, active=False)
                    open_v = self.store.open_visit(cid)
                    if open_v:
                        self.store.close_visit(int(open_v["id"]))
                    removed.append(
                        {
                            "id": cid,
                            "name": (cust or {}).get("name"),
                            "score": round(score, 4),
                            "reason": "staff",
                        }
                    )
        merged = self.merge_near_duplicates(threshold=soft)
        return {
            "ok": True,
            "removed": removed,
            "count": len(removed),
            "merged": merged.get("merged", []),
            "merged_count": merged.get("count", 0),
        }

    def merge_near_duplicates(self, *, threshold: float | None = None) -> dict[str, Any]:
        """Merge newer guests that look like an older active guest (keep oldest id)."""
        settings = self.store.get_settings()
        th = float(
            threshold
            if threshold is not None
            else max(0.24, float(settings["soft_match_threshold"]))
        )
        # Unique customer ids with at least one embedding (use first seen / oldest).
        by_id: dict[int, list[bytes]] = {}
        for cid, blob in self.store.list_embeddings():
            by_id.setdefault(int(cid), []).append(blob)
        ids = sorted(by_id.keys())
        merged: list[dict[str, Any]] = []
        keepers: list[int] = []
        for cid in ids:
            best_id = None
            best_score = -1.0
            for kid in keepers:
                for blob in by_id[cid]:
                    emb = FaceEngine.bytes_to_embedding(blob)
                    for kblob in by_id[kid]:
                        score = FaceEngine.cosine(
                            emb, FaceEngine.bytes_to_embedding(kblob)
                        )
                        if score > best_score:
                            best_score = score
                            best_id = kid
            if best_id is not None and best_score >= th:
                cust = self.store.get_customer(cid)
                keep = self.store.get_customer(best_id)
                self.store.merge_into(best_id, cid)
                open_v = self.store.open_visit(cid)
                if open_v:
                    self.store.close_visit(int(open_v["id"]))
                # Absorb dropped embeddings into keeper cache for chain merges.
                by_id[best_id].extend(by_id.get(cid, []))
                merged.append(
                    {
                        "id": cid,
                        "name": (cust or {}).get("name"),
                        "kept_id": best_id,
                        "kept_name": (keep or {}).get("name"),
                        "score": round(best_score, 4),
                    }
                )
            else:
                keepers.append(cid)
        return {"ok": True, "merged": merged, "count": len(merged)}

    def _expire_pending(self) -> None:
        now = time.time()
        self._pending = [
            p for p in self._pending if (now - p.last_seen) <= self._pending_ttl_sec
        ]

    def _expire_forgotten(self) -> None:
        now = time.time()
        self._forgotten = [
            f
            for f in self._forgotten
            if (now - f.deleted_at) <= self._forgotten_ttl_sec
        ]

    def _best_forgotten_score(self, emb: np.ndarray) -> float:
        best = -1.0
        for f in self._forgotten:
            score = FaceEngine.cosine(emb, f.embedding)
            if score > best:
                best = score
        return best

    def _forget_match(self, emb: np.ndarray, *, threshold: float = 0.28) -> None:
        """Drop forgotten entries that match this newly re-enrolled face."""
        self._forgotten = [
            f
            for f in self._forgotten
            if FaceEngine.cosine(emb, f.embedding) < float(threshold)
        ]

    def _confirm_and_enroll(
        self,
        emb: np.ndarray,
        jpeg: bytes,
        hit: FaceHit,
        *,
        soft: float,
        reid_floor: float,
        staff_th: float,
        staff_embs: list[bytes],
        confirm_hits: int,
        known: list[tuple[int, bytes]],
        skip_known_rematch: bool = False,
        allow_finalize: bool = True,
        force_finalize_hits: int | None = None,
    ) -> dict[str, Any] | None:
        """Accumulate consistent unknown sightings; enroll after enough good hits."""
        # Last-chance rematch against gallery / staff before growing pending.
        # Skipped after an intentional delete so soft matches cannot absorb them.
        if not skip_known_rematch:
            best_id = None
            best_known = -1.0
            for cid, blob in known:
                score = FaceEngine.cosine(emb, FaceEngine.bytes_to_embedding(blob))
                if score > best_known:
                    best_known = score
                    best_id = cid
            if best_id is not None and best_known >= reid_floor:
                return self.store.get_customer(best_id)

        staff_score = self._best_staff_score(emb, staff_embs)
        if staff_score >= staff_th:
            return None

        now = time.time()
        best_i = -1
        best_score = -1.0
        for i, pend in enumerate(self._pending):
            score = FaceEngine.cosine(emb, pend.embedding)
            if score > best_score:
                best_score = score
                best_i = i

        photo = FaceEngine.crop_face_jpeg(jpeg, hit.bbox)
        need = max(1, int(confirm_hits))
        force_at = max(need, int(force_finalize_hits or need))
        # Pending merge is softer than gallery so side/front of one person stay one pending.
        pend_th = max(0.18, min(soft, reid_floor) - 0.04)

        def _finalize(pend: _PendingFace, pop_i: int | None) -> dict[str, Any] | None:
            if self._best_staff_score(pend.embedding, staff_embs) >= staff_th:
                if pop_i is not None:
                    self._pending.pop(pop_i)
                return None
            if not skip_known_rematch:
                for cid, blob in known:
                    score = FaceEngine.cosine(
                        pend.embedding, FaceEngine.bytes_to_embedding(blob)
                    )
                    if score >= reid_floor:
                        if pop_i is not None:
                            self._pending.pop(pop_i)
                        return self.store.get_customer(cid)
            customer = self.store.add_customer(
                name=self.store.next_guest_name(),
                embedding=FaceEngine.embedding_to_bytes(pend.embedding),
                photo_bytes=pend.photo or photo,
                photo_area=pend.best_area or hit.area,
                photo_det=pend.best_det or float(hit.det_score or 0.0),
            )
            known.append(
                (int(customer["id"]), FaceEngine.embedding_to_bytes(pend.embedding))
            )
            if pop_i is not None:
                self._pending.pop(pop_i)
            return customer

        def _maybe_finalize(pend: _PendingFace, pop_i: int | None) -> dict[str, Any] | None:
            # Good crop → enroll at confirm_hits. Weaker crops need a few more hits.
            if pend.hits < need:
                return None
            if allow_finalize and pend.hits >= need:
                return _finalize(pend, pop_i)
            if pend.hits >= force_at:
                return _finalize(pend, pop_i)
            return None

        if best_i >= 0 and best_score >= pend_th:
            pend = self._pending[best_i]
            # Running average keeps the pending identity stable across angles.
            blended = pend.embedding * 0.6 + emb * 0.4
            n = float(np.linalg.norm(blended))
            if n > 1e-6:
                pend.embedding = (blended / n).astype(np.float32)
            pend.hits += 1
            pend.last_seen = now
            if hit.area >= pend.best_area:
                pend.best_area = hit.area
                pend.best_det = float(hit.det_score or 0.0)
                pend.photo = photo
            return _maybe_finalize(pend, best_i)

        pend = _PendingFace(
            embedding=emb.astype(np.float32, copy=True),
            hits=1,
            last_seen=now,
            photo=photo,
            best_area=hit.area,
            best_det=float(hit.det_score or 0.0),
        )
        if need <= 1 and allow_finalize:
            return _finalize(pend, None)

        self._pending.append(pend)
        # Cap pending list so memory stays bounded.
        if len(self._pending) > 40:
            self._pending = self._pending[-40:]
        return _maybe_finalize(pend, len(self._pending) - 1)

    def _maybe_add_angle(
        self,
        customer_id: int,
        emb: np.ndarray,
        hit: FaceHit,
        known: list[tuple[int, bytes]],
    ) -> None:
        """Keep a distinct angle when soft-matched (prevents Guest N+1 on turn)."""
        try:
            if hit.area < 500 or hit.det_score < 0.35:
                return
            # Skip if any stored face for this guest is already nearly identical.
            for cid, blob in known:
                if int(cid) != int(customer_id):
                    continue
                if FaceEngine.cosine(emb, FaceEngine.bytes_to_embedding(blob)) >= 0.82:
                    return
            blob = FaceEngine.embedding_to_bytes(emb)
            if self.store.add_customer_face(
                int(customer_id), blob, area=float(hit.area), max_faces=4
            ):
                known.append((int(customer_id), blob))
        except Exception:
            pass

    def _blend_gallery(
        self,
        customer_id: int,
        emb: np.ndarray,
        jpeg: bytes,
        hit: FaceHit,
        known: list[tuple[int, bytes]],
        *,
        new_weight: float = 0.3,
    ) -> None:
        """Nudge stored embedding toward a new sighting (helps angle changes)."""
        try:
            old_blob = next((b for cid, b in known if cid == customer_id), None)
            if old_blob is None:
                return
            old = FaceEngine.bytes_to_embedding(old_blob)
            w = float(max(0.05, min(0.5, new_weight)))
            blended = old * (1.0 - w) + emb * w
            n = float(np.linalg.norm(blended))
            if n < 1e-6:
                return
            blended = (blended / n).astype(np.float32)
            blob = FaceEngine.embedding_to_bytes(blended)
            # Embedding can update often; portrait only upgrades until locked clear.
            # Prefer clear/frontal crops so soft side-angle shots don't dilute photos.
            photo = None
            if hit.is_clear_for_enroll(
                min_area=2000.0, min_det=0.45, max_yaw=30.0, max_pitch=30.0
            ) or hit.area >= 4500.0:
                photo = FaceEngine.crop_face_jpeg(jpeg, hit.bbox)
            self.store.update_embedding(
                customer_id,
                blob,
                photo_bytes=photo,
                photo_area=hit.area,
                photo_det=float(hit.det_score or 0.0),
            )
            for i, (cid, _) in enumerate(known):
                if cid == customer_id:
                    known[i] = (cid, blob)
                    break
        except Exception:
            pass

    @staticmethod
    def _staff_embeddings() -> list[bytes]:
        try:
            from v380.attendance.store import AttendanceStore

            return [blob for _, blob in AttendanceStore().list_embeddings()]
        except Exception:
            return []

    @staticmethod
    def _best_staff_score(emb: Any, staff_embs: list[bytes]) -> float:
        best = -1.0
        for blob in staff_embs:
            score = FaceEngine.cosine(emb, FaceEngine.bytes_to_embedding(blob))
            if score > best:
                best = score
        return best

    @staticmethod
    def _matches_staff(emb: Any, staff_embs: list[bytes], threshold: float) -> bool:
        return CustomerService._best_staff_score(emb, staff_embs) >= threshold

    @staticmethod
    def _table_hint_for_camera(camera_id: int) -> tuple[str | None, str | None]:
        """If this camera has exactly one table, auto-link it."""
        try:
            from v380.tables.store import TableStore

            tables = TableStore().list_for_camera(int(camera_id))
            if len(tables) == 1:
                t = tables[0]
                return t.id, t.name
        except Exception:
            pass
        return None, None

    @staticmethod
    def _decode_b64(raw: str | None) -> bytes:
        if not raw:
            return b""
        s = raw.strip()
        if "," in s and s.lower().startswith("data:"):
            s = s.split(",", 1)[1]
        try:
            return base64.b64decode(s, validate=False)
        except Exception as exc:
            raise FaceEngineError(f"Invalid image_base64: {exc}") from exc


def _jpeg_size(jpeg: bytes) -> tuple[float, float]:
    """Return (width, height) of a JPEG, or (0, 0) on failure."""
    if not jpeg:
        return 0.0, 0.0
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return 0.0, 0.0
        h, w = img.shape[:2]
        return float(w), float(h)
    except Exception:
        return 0.0, 0.0


def _parse_visit_ts(value: str | None):
    from datetime import datetime

    raw = (value or "").strip().replace("Z", "").replace(" ", "T")
    if not raw:
        return None
    for n, fmt in ((19, "%Y-%m-%dT%H:%M:%S"), (16, "%Y-%m-%dT%H:%M")):
        try:
            return datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    return None


def _day_sec(ts) -> int:
    return int(ts.hour * 3600 + ts.minute * 60 + ts.second)


_service: CustomerService | None = None
_svc_lock = threading.Lock()


def get_service() -> CustomerService:
    global _service
    with _svc_lock:
        if _service is None:
            _service = CustomerService()
        return _service
