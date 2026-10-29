"""Attendance business logic — enroll, match, check-in. No HTTP / live / tables coupling."""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable

import numpy as np

from v380.attendance.face_engine import FaceEngine, FaceEngineError, FaceHit
from v380.attendance.store import AttendanceStore

JpegProvider = Callable[[int], bytes | None]


class AttendanceService:
    """Singleton-friendly facade used by HTTP routes and the entrance watcher."""

    # Reject when top staff is not clearly ahead of the next staff (false IDs).
    MATCH_MARGIN = 0.08

    def __init__(self, store: AttendanceStore | None = None):
        self.store = store or AttendanceStore()
        self._match_lock = threading.Lock()
        self._last_match: dict[str, Any] | None = None
        self._last_error: str | None = None

    @classmethod
    def _match_staff(
        cls,
        hit: FaceHit,
        staff_embs: list[tuple[int, bytes]],
        *,
        threshold: float,
        min_margin: float | None = None,
    ) -> tuple[int | None, float, float, float]:
        """Pick best staff for one face.

        Returns (staff_id|None, best_score, second_best_score, effective_threshold).
        Requires score >= threshold and a clear gap vs the next *different* staff.
        """
        margin = float(cls.MATCH_MARGIN if min_margin is None else min_margin)
        per_staff: dict[int, float] = {}
        for sid, blob in staff_embs:
            known = FaceEngine.bytes_to_embedding(blob)
            score = FaceEngine.cosine(hit.embedding, known)
            sid_i = int(sid)
            if score > per_staff.get(sid_i, -1.0):
                per_staff[sid_i] = score
        if not per_staff:
            return None, -1.0, -1.0, float(threshold)
        ranked = sorted(per_staff.items(), key=lambda kv: kv[1], reverse=True)
        best_id, best_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        # Keep threshold firm — old large-face discounts caused stranger→staff hits.
        effective_th = float(threshold)
        if best_score < effective_th:
            return None, best_score, second, effective_th
        if second >= 0.0 and (best_score - second) < margin:
            return None, best_score, second, effective_th
        return best_id, best_score, second, effective_th

    def status(self) -> dict[str, Any]:
        ok, msg = FaceEngine.available()
        settings = self.store.get_settings()
        from v380.attendance.watcher import AttendanceWatcher

        return {
            "ok": True,
            "face_engine": {"available": ok, "message": msg},
            "settings": settings,
            "watcher": AttendanceWatcher.instance().status(),
            "summary": self.store.summary_today(),
            "last_match": self._last_match,
            "last_error": self._last_error,
        }

    def get_settings(self) -> dict[str, Any]:
        return self.store.get_settings()

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.store.update_settings(
            enabled=body.get("enabled"),
            entrance_camera_id=body.get("entrance_camera_id"),
            entrance_camera_enabled=body.get("entrance_camera_enabled"),
            exit_camera_enabled=body.get("exit_camera_enabled"),
            exit_camera_id=body.get("exit_camera_id"),
            closing_time=body.get("closing_time"),
            exit_window_minutes=body.get("exit_window_minutes"),
            auto_checkout_at_close=body.get("auto_checkout_at_close"),
            match_threshold=body.get("match_threshold"),
            cooldown_sec=body.get("cooldown_sec"),
            poll_interval_sec=body.get("poll_interval_sec"),
            min_shift_sec=body.get("min_shift_sec"),
        )
        from v380.attendance.watcher import AttendanceWatcher

        AttendanceWatcher.instance().apply_settings(settings)
        return settings

    def list_staff(self) -> list[dict[str, Any]]:
        return self.store.list_staff()

    @staticmethod
    def _pick_staff_face(hits: list[FaceHit]) -> FaceHit | None:
        """Prefer a clear/frontal live face (skip poster/banner hits)."""
        if not hits:
            return None
        live = [h for h in hits if not h.likely_print]
        pool_src = live or list(hits)
        clear = [
            h
            for h in pool_src
            if h.is_clear_for_enroll(
                min_area=900.0, min_det=0.42, max_yaw=40.0, max_pitch=45.0
            )
        ]
        pool = clear or pool_src
        return max(
            pool,
            key=lambda h: float(h.area)
            * (0.55 + float(h.det_score))
            * (1.15 - min(0.55, (abs(h.yaw) + abs(h.pitch) * 0.5) / 90.0)),
        )

    def preview_faces(
        self,
        jpeg: bytes,
    ) -> dict[str, Any]:
        """Detect faces for enroll UI — returns boxes + which face would be enrolled."""
        if not jpeg:
            return {"ok": False, "error": "no jpeg", "faces": [], "pick_index": None}
        try:
            # Higher det floor + print reject drops wall-banner faces in enroll UI.
            hits = FaceEngine.detect_jpeg(
                jpeg,
                min_area=500.0,
                min_det_score=0.45,
                reject_prints=True,
                upscale_small=False,
            )
        except FaceEngineError as exc:
            return {"ok": False, "error": str(exc), "faces": [], "pick_index": None}

        # Infer frame size from jpeg via OpenCV when possible.
        frame_w, frame_h = 0, 0
        try:
            import cv2

            arr = np.frombuffer(jpeg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                frame_h, frame_w = int(img.shape[0]), int(img.shape[1])
        except Exception:
            pass
        if frame_w < 1 or frame_h < 1:
            # Fallback from bbox extents
            for h in hits:
                x1, y1, x2, y2 = h.bbox
                frame_w = max(frame_w, int(x2) + 1)
                frame_h = max(frame_h, int(y2) + 1)
        frame_w = max(1, frame_w)
        frame_h = max(1, frame_h)

        pick = self._pick_staff_face(hits)
        pick_index: int | None = None
        faces_out: list[dict[str, Any]] = []
        for i, hit in enumerate(hits):
            x1, y1, x2, y2 = hit.bbox
            clear = hit.is_clear_for_enroll(
                min_area=900.0, min_det=0.40, max_yaw=40.0, max_pitch=45.0
            )
            is_pick = pick is not None and hit is pick
            if is_pick:
                pick_index = i
            faces_out.append(
                {
                    "index": i,
                    "norm": [
                        max(0.0, min(1.0, float(x1) / frame_w)),
                        max(0.0, min(1.0, float(y1) / frame_h)),
                        max(0.0, min(1.0, float(x2) / frame_w)),
                        max(0.0, min(1.0, float(y2) / frame_h)),
                    ],
                    "det_score": round(float(hit.det_score), 3),
                    "area": round(float(hit.area), 1),
                    "clear_for_enroll": clear,
                    "will_enroll": is_pick,
                    "label": "Staff to enroll" if is_pick else "Other person",
                }
            )

        return {
            "ok": True,
            "faces": faces_out,
            "face_count": len(faces_out),
            "pick_index": pick_index,
            "ready": pick_index is not None
            and bool(faces_out[pick_index]["clear_for_enroll"]),
            "frame": {"w": frame_w, "h": frame_h},
            "hint": (
                "Tap a face to choose who to enroll (green = selected)."
                if pick_index is not None
                else "No clear face yet — stand alone facing the entrance camera."
            ),
        }

    @staticmethod
    def _box_iou_norm(a: list[float], b: tuple[float, float, float, float], w: float, h: float) -> float:
        """IoU between normalized [x1,y1,x2,y2] and pixel bbox."""
        if w < 1 or h < 1 or len(a) < 4:
            return 0.0
        ax1, ay1, ax2, ay2 = [float(v) for v in a[:4]]
        bx1, by1, bx2, by2 = b
        bx1, by1, bx2, by2 = bx1 / w, by1 / h, bx2 / w, by2 / h
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return float(inter / union) if union > 0 else 0.0

    def enroll(
        self,
        *,
        name: str,
        role: str = "staff",
        image_base64: str | None = None,
        jpeg: bytes | None = None,
        face_index: int | None = None,
        face_norm: list[float] | None = None,
    ) -> dict[str, Any]:
        data = jpeg or self._decode_b64(image_base64)
        if not data:
            raise FaceEngineError("Provide a face photo (image_base64)")
        hits = FaceEngine.detect_jpeg(
            data, min_area=600.0, min_det_score=0.40, reject_prints=True
        )
        if not hits:
            raise FaceEngineError("No face detected — face the camera clearly and try again")

        hit: FaceHit | None = None
        # Prefer explicit selection from the enroll UI.
        if face_norm is not None and len(face_norm) >= 4:
            frame_w, frame_h = 0, 0
            try:
                import cv2

                arr = np.frombuffer(data, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    frame_h, frame_w = int(img.shape[0]), int(img.shape[1])
            except Exception:
                pass
            if frame_w < 1 or frame_h < 1:
                for h in hits:
                    x1, y1, x2, y2 = h.bbox
                    frame_w = max(frame_w, int(x2) + 1)
                    frame_h = max(frame_h, int(y2) + 1)
            best_i, best_iou = -1, 0.0
            for i, h in enumerate(hits):
                iou = self._box_iou_norm(list(face_norm), h.bbox, float(frame_w), float(frame_h))
                if iou > best_iou:
                    best_iou, best_i = iou, i
            if best_i >= 0 and best_iou >= 0.15:
                hit = hits[best_i]
            else:
                raise FaceEngineError(
                    "Selected face not found in this frame — tap again and confirm"
                )
        elif face_index is not None:
            try:
                fi = int(face_index)
            except (TypeError, ValueError) as exc:
                raise FaceEngineError("Invalid face_index") from exc
            if fi < 0 or fi >= len(hits):
                raise FaceEngineError(
                    f"face_index {fi} out of range ({len(hits)} faces)"
                )
            hit = hits[fi]
        else:
            hit = self._pick_staff_face(hits)

        assert hit is not None
        if not hit.is_clear_for_enroll(
            min_area=900.0, min_det=0.40, max_yaw=40.0, max_pitch=45.0
        ):
            raise FaceEngineError(
                "Selected face not clear enough — face the camera with good lighting"
            )
        # Block duplicate enroll when this face already matches staff.
        settings = self.store.get_settings()
        threshold = float(settings["match_threshold"])
        staff_embs = self.store.list_embeddings()
        sid, score, _second, _th = self._match_staff(
            hit, staff_embs, threshold=threshold
        )
        if sid is not None:
            existing = self.store.get_staff(sid) or {}
            ename = existing.get("name") or f"Staff #{sid}"
            raise FaceEngineError(
                f"Already enrolled as {ename} (score {score:.2f}) — "
                "use Already enrolled? or add a face angle on that staff"
            )
        photo = FaceEngine.crop_face_jpeg(data, hit.bbox)
        staff = self.store.add_staff(
            name=name,
            role=role,
            embedding=FaceEngine.embedding_to_bytes(hit.embedding),
            photo_bytes=photo,
            photo_area=float(hit.area),
        )
        return staff

    def add_face(
        self,
        staff_id: int,
        *,
        image_base64: str | None = None,
        jpeg: bytes | None = None,
    ) -> dict[str, Any]:
        """Register an extra angle; also refreshes the primary match embedding."""
        data = jpeg or self._decode_b64(image_base64)
        if not data:
            raise FaceEngineError("Provide a face photo (image_base64)")
        hits = FaceEngine.detect_jpeg(
            data, min_area=600.0, min_det_score=0.40, reject_prints=True
        )
        if not hits:
            raise FaceEngineError("No face detected")
        hit = self._pick_staff_face(hits)
        assert hit is not None
        photo = FaceEngine.crop_face_jpeg(data, hit.bbox)
        emb_bytes = FaceEngine.embedding_to_bytes(hit.embedding)
        out = self.store.add_staff_face(
            int(staff_id),
            embedding=emb_bytes,
            photo_bytes=photo,
            photo_area=float(hit.area),
            max_faces=5,
        )
        if out is None:
            raise FaceEngineError("Staff not found")
        # Manual capture refreshes primary so a bad first enroll can be corrected.
        self.store.update_primary_embedding(
            int(staff_id),
            embedding=emb_bytes,
            photo_bytes=photo,
            photo_area=float(hit.area),
        )
        out["primary_updated"] = True
        return out

    def delete_staff(self, staff_id: int) -> bool:
        return self.store.delete_staff(staff_id)

    def set_active(self, staff_id: int, active: bool) -> dict[str, Any] | None:
        return self.store.set_staff_active(staff_id, active)

    def list_events(self, *, day: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_events(day=day, limit=limit)

    def list_sessions(self, *, day: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_sessions(day=day)

    def staff_detail(self, staff_id: int, *, days: int = 30) -> dict[str, Any] | None:
        return self.store.staff_detail(int(staff_id), days=days)

    def process_frame(
        self,
        jpeg: bytes,
        *,
        camera_id: int | None = None,
        force: bool = False,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Match a face and record check-in or check-out for the day session.

        purpose:
          None / \"auto\" — entrance does both (legacy)
          \"enter\" — check-in only (used when exit camera is enabled)
          \"exit\" — check-out only (dedicated exit camera)
        """
        purpose_n = (purpose or "auto").strip().lower()
        if purpose_n not in {"auto", "enter", "exit"}:
            purpose_n = "auto"
        with self._match_lock:
            try:
                # Live door poll: no 2× upscale; skip if UI enroll/identify holds the model.
                hits = FaceEngine.detect_jpeg(
                    jpeg,
                    min_area=400.0,
                    min_det_score=0.40,
                    reject_prints=True,
                    upscale_small=False,
                    wait=False,
                )
            except FaceEngineError as exc:
                msg = str(exc)
                self._last_error = msg
                if "busy" in msg.lower():
                    return {"ok": True, "matched": False, "skipped": True, "error": "busy"}
                return {"ok": False, "matched": False, "error": msg}
            if not hits:
                self._last_error = "No face detected"
                return {"ok": False, "matched": False, "error": "No face detected"}

            settings = self.store.get_settings()
            threshold = float(settings["match_threshold"])
            min_shift = int(settings.get("min_shift_sec") or 1800)
            staff_embs = self.store.list_embeddings()
            # Check EVERY face — keep the strongest *confident* staff match.
            best_id: int | None = None
            best_score = -1.0
            best_second = -1.0
            best_hit: FaceHit | None = None
            effective_th = threshold
            for hit in hits:
                sid, score, second, th = self._match_staff(
                    hit, staff_embs, threshold=threshold
                )
                if sid is None:
                    if score > best_score:
                        best_score = score
                        best_second = second
                        effective_th = th
                    continue
                if score > best_score:
                    best_id = sid
                    best_score = score
                    best_second = second
                    best_hit = hit
                    effective_th = th

            if best_id is None or best_hit is None:
                self._last_error = None
                self._last_match = {
                    "matched": False,
                    "score": round(best_score, 4) if best_score >= 0 else None,
                    "second_score": round(best_second, 4) if best_second >= 0 else None,
                    "faces": len(hits),
                    "threshold": threshold,
                    "at": _now_iso(),
                }
                return {
                    "ok": True,
                    "matched": False,
                    "score": round(best_score, 4) if best_score >= 0 else None,
                    "second_score": round(best_second, 4) if best_second >= 0 else None,
                    "faces": len(hits),
                    "threshold": effective_th,
                }

            hit = best_hit
            emb = hit.embedding
            staff = self.store.get_staff(best_id)
            if staff is None:
                return {"ok": False, "matched": False, "error": "staff missing"}

            # Learn a new angle when match is solid but not near-identical.
            self._maybe_learn_angle(best_id, emb, jpeg, hit, best_score, threshold)

            open_session = self.store.open_session(best_id)
            score_r = round(best_score, 4)
            faces_n = len(hits)

            # Already working — check out only after min shift (or force via Scan).
            if open_session is not None:
                # Entrance-only mode: never check out here when exit camera is used.
                if purpose_n == "enter" and not force:
                    self._last_match = {
                        "matched": True,
                        "action": "working",
                        "staff": staff,
                        "score": score_r,
                        "session": open_session,
                        "at": _now_iso(),
                    }
                    self._last_error = None
                    return {
                        "ok": True,
                        "matched": True,
                        "action": "working",
                        "already_checked_in": True,
                        "staff": staff,
                        "score": score_r,
                        "session": open_session,
                        "message": (
                            f"{staff.get('name')} is checked in — use exit camera to check out"
                        ),
                    }

                worked = int(open_session.get("duration_sec") or 0)
                # Dedicated exit purpose: short grace only (door exit should punch Out).
                # Auto/same-cam mode keeps full min_shift so In→Out doesn't flip instantly.
                need_sec = 60 if purpose_n == "exit" else min_shift
                if not force and worked < need_sec:
                    left = max(0, need_sec - worked)
                    self._last_match = {
                        "matched": True,
                        "action": "working",
                        "staff": staff,
                        "score": score_r,
                        "session": open_session,
                        "at": _now_iso(),
                    }
                    self._last_error = None
                    return {
                        "ok": True,
                        "matched": True,
                        "action": "working",
                        "already_checked_in": True,
                        "staff": staff,
                        "score": score_r,
                        "session": open_session,
                        "min_shift_sec": need_sec,
                        "seconds_until_checkout": left,
                        "message": (
                            f"{staff.get('name')} is checked in since "
                            f"{open_session.get('check_in_time')} "
                            f"({open_session.get('duration')} so far)"
                            + (
                                f" — check-out in ~{left // 60}m {left % 60}s"
                                if left > 0
                                else ""
                            )
                        ),
                    }

                event = self.store.add_event(
                    staff_id=best_id,
                    event_type="check_out",
                    camera_id=camera_id,
                    score=score_r,
                )
                session = self.store.open_session(best_id)  # should be None
                # Rebuild completed session from check-in + this check-out.
                sessions = self.store.list_sessions()
                completed = next(
                    (
                        s
                        for s in sessions
                        if int(s["staff_id"]) == int(best_id) and s.get("status") == "completed"
                    ),
                    None,
                )
                self._last_match = {
                    "matched": True,
                    "action": "check_out",
                    "staff": staff,
                    "score": score_r,
                    "event": event,
                    "session": completed,
                    "at": _now_iso(),
                }
                self._last_error = None
                return {
                    "ok": True,
                    "matched": True,
                    "action": "check_out",
                    "staff": staff,
                    "score": score_r,
                    "event": event,
                    "session": completed,
                    "message": (
                        f"Checked out: {staff.get('name')}"
                        + (
                            f" · {completed.get('duration')}"
                            if completed and completed.get("duration")
                            else ""
                        )
                    ),
                }

            # Exit camera: only check out — do not create a new check-in.
            if purpose_n == "exit":
                self._last_match = {
                    "matched": True,
                    "action": "ignored",
                    "staff": staff,
                    "score": score_r,
                    "at": _now_iso(),
                }
                self._last_error = None
                return {
                    "ok": True,
                    "matched": True,
                    "action": "ignored",
                    "staff": staff,
                    "score": score_r,
                    "message": (
                        f"{staff.get('name')} is not checked in — exit camera ignored"
                    ),
                }

            # No open session → check in (new day or after prior check-out).
            event = self.store.add_event(
                staff_id=best_id,
                event_type="check_in",
                camera_id=camera_id,
                score=score_r,
            )
            session = self.store.open_session(best_id)
            self._last_match = {
                "matched": True,
                "action": "check_in",
                "staff": staff,
                "score": score_r,
                "event": event,
                "session": session,
                "at": _now_iso(),
            }
            self._last_error = None
            return {
                "ok": True,
                "matched": True,
                "action": "check_in",
                "already_checked_in": False,
                "staff": staff,
                "score": score_r,
                "event": event,
                "session": session,
                "message": f"Checked in: {staff.get('name')} at {session.get('check_in_time') if session else ''}",
            }

    def scan_entrance(self, get_jpeg: JpegProvider, *, force: bool = False) -> dict[str, Any]:
        settings = self.store.get_settings()
        cam_id = settings.get("entrance_camera_id")
        if cam_id is None:
            raise FaceEngineError("Set an entrance camera in Attendance settings first")
        jpeg = get_jpeg(int(cam_id))
        if not jpeg:
            raise FaceEngineError("No snapshot from entrance camera — is it online?")
        return self.process_frame(jpeg, camera_id=int(cam_id), force=force)

    def identify(
        self,
        jpeg: bytes,
        *,
        face_norm: list[float] | None = None,
    ) -> dict[str, Any]:
        """Match face(s) to enrolled staff — no check-in / check-out.

        Returns every detected face with already_enrolled + label so the UI
        can auto-tell without a separate Check tap.
        """
        if not jpeg:
            raise FaceEngineError("Provide a face photo")
        hits = FaceEngine.detect_jpeg(
            jpeg,
            min_area=400.0,
            min_det_score=0.40,
            reject_prints=True,
            upscale_small=False,
        )
        frame_w, frame_h = 0, 0
        try:
            import cv2

            arr = np.frombuffer(jpeg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                frame_h, frame_w = int(img.shape[0]), int(img.shape[1])
        except Exception:
            pass
        if frame_w < 1 or frame_h < 1:
            for h in hits:
                x1, y1, x2, y2 = h.bbox
                frame_w = max(frame_w, int(x2) + 1)
                frame_h = max(frame_h, int(y2) + 1)
        frame_w = max(1, frame_w)
        frame_h = max(1, frame_h)

        settings = self.store.get_settings()
        threshold = float(settings["match_threshold"])
        staff_embs = self.store.list_embeddings()

        faces_out: list[dict[str, Any]] = []
        for i, hit in enumerate(hits):
            x1, y1, x2, y2 = hit.bbox
            sid, score, second, eff_th = self._match_staff(
                hit, staff_embs, threshold=threshold
            )
            staff = self.store.get_staff(sid) if sid is not None else None
            already = staff is not None
            name = (staff or {}).get("name") if staff else None
            clear = hit.is_clear_for_enroll(
                min_area=900.0, min_det=0.40, max_yaw=40.0, max_pitch=45.0
            )
            faces_out.append(
                {
                    "index": i,
                    "norm": [
                        max(0.0, min(1.0, float(x1) / frame_w)),
                        max(0.0, min(1.0, float(y1) / frame_h)),
                        max(0.0, min(1.0, float(x2) / frame_w)),
                        max(0.0, min(1.0, float(y2) / frame_h)),
                    ],
                    "det_score": round(float(hit.det_score), 3),
                    "area": round(float(hit.area), 1),
                    "clear_for_enroll": clear,
                    "already_enrolled": already,
                    "score": round(score, 4) if score >= 0 else None,
                    "second_score": round(second, 4) if second >= 0 else None,
                    "threshold": eff_th,
                    "staff": staff,
                    "will_enroll": already,
                    "label": (
                        f"Already · {name}"
                        if already and name
                        else ("Already enrolled" if already else "New · not enrolled")
                    ),
                }
            )

        # Optional focus on one tapped face.
        focus: dict[str, Any] | None = None
        if face_norm is not None and len(face_norm) >= 4 and faces_out:
            best_i, best_iou = -1, 0.0
            for i, f in enumerate(faces_out):
                n = f.get("norm") or []
                if len(n) < 4:
                    continue
                iou = self._box_iou_norm(
                    list(face_norm),
                    (
                        float(n[0]) * frame_w,
                        float(n[1]) * frame_h,
                        float(n[2]) * frame_w,
                        float(n[3]) * frame_h,
                    ),
                    float(frame_w),
                    float(frame_h),
                )
                if iou > best_iou:
                    best_iou, best_i = iou, i
            if best_i >= 0 and best_iou >= 0.12:
                focus = faces_out[best_i]
        if focus is None and faces_out:
            # Prefer an already-enrolled face when present, else largest.
            enrolled = [f for f in faces_out if f.get("already_enrolled")]
            focus = max(
                enrolled or faces_out,
                key=lambda f: float(f.get("area") or 0),
            )

        if not faces_out:
            return {
                "ok": True,
                "already_enrolled": False,
                "matched": False,
                "faces": [],
                "face_count": 0,
                "message": "No face yet — stand in front of the camera",
                "hint": "Looking for a face…",
            }

        already = bool(focus and focus.get("already_enrolled"))
        staff = focus.get("staff") if focus else None
        name = (staff or {}).get("name") if isinstance(staff, dict) else None
        if already:
            msg = f"Already enrolled as {name}" if name else "Already enrolled"
        else:
            msg = "Not in staff list — safe to Add staff"

        return {
            "ok": True,
            "already_enrolled": already,
            "matched": already,
            "score": focus.get("score") if focus else None,
            "threshold": focus.get("threshold") if focus else threshold,
            "faces": faces_out,
            "face_count": len(faces_out),
            "face": focus,
            "staff": staff,
            "message": msg,
            "hint": msg,
        }

    def _maybe_learn_angle(
        self,
        staff_id: int,
        emb: np.ndarray,
        jpeg: bytes,
        hit: FaceHit,
        best_score: float,
        threshold: float,
    ) -> None:
        """Store an extra embedding when this angle differs from existing ones."""
        try:
            # Only learn from strong matches — weak ones pollute the gallery.
            if best_score < max(float(threshold), 0.55):
                return
            if best_score >= 0.88:
                return  # Already covered tightly.
            if self.store.staff_face_count(staff_id) >= 5:
                return
            # Prefer reasonably clear detections for gallery quality.
            if hit.det_score < 0.50 or hit.area < 1200:
                return
            # Skip if any stored face is already very close.
            for sid, blob in self.store.list_embeddings():
                if int(sid) != int(staff_id):
                    continue
                known = FaceEngine.bytes_to_embedding(blob)
                if FaceEngine.cosine(emb, known) >= 0.85:
                    return
            photo = FaceEngine.crop_face_jpeg(jpeg, hit.bbox)
            self.store.add_staff_face(
                int(staff_id),
                embedding=FaceEngine.embedding_to_bytes(emb),
                photo_bytes=photo,
                photo_area=float(hit.area),
                max_faces=5,
            )
        except Exception:
            pass

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


_service: AttendanceService | None = None
_service_lock = threading.Lock()


def get_service() -> AttendanceService:
    global _service
    with _service_lock:
        if _service is None:
            _service = AttendanceService()
        return _service


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
