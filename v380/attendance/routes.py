"""HTTP handlers for /api/attendance/* — called only when path matches; never touches other routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from v380.attendance.face_engine import FaceEngineError
from v380.attendance.service import get_service
from v380.attendance.watcher import AttendanceWatcher


def try_handle(handler: Any, method: str, path: str) -> bool:
    """Return True if this request was handled (attendance paths only)."""
    if not path.startswith("/api/attendance"):
        return False
    if method == "GET":
        return _get(handler, path)
    if method == "POST":
        return _post(handler, path)
    if method == "PUT":
        return _put(handler, path)
    if method == "DELETE":
        return _delete(handler, path)
    handler.send_json({"ok": False, "error": "method not allowed"}, 405)
    return True


def _get(handler: Any, path: str) -> bool:
    svc = get_service()
    if path in {"/api/attendance", "/api/attendance/status"}:
        handler.send_json(svc.status())
        return True
    if path == "/api/attendance/settings":
        handler.send_json({"ok": True, "settings": svc.get_settings()})
        return True
    if path == "/api/attendance/staff":
        handler.send_json({"ok": True, "staff": svc.list_staff()})
        return True
    if path.startswith("/api/attendance/staff/") and path.endswith("/photo"):
        mid = path[len("/api/attendance/staff/") : -len("/photo")].strip("/")
        try:
            sid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        photo = svc.store.photo_path_for(sid)
        if photo is None:
            handler.send_json({"ok": False, "error": "no photo"}, 404)
            return True
        data = Path(photo).read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "private, max-age=60")
        handler.end_headers()
        handler.wfile.write(data)
        return True
    if path.startswith("/api/attendance/staff/"):
        mid = path[len("/api/attendance/staff/") :].strip("/")
        if mid.isdigit():
            qs = parse_qs(urlparse(handler.path).query)
            try:
                days = int((qs.get("days") or ["30"])[0])
            except ValueError:
                days = 30
            detail = svc.staff_detail(int(mid), days=days)
            if detail is None:
                handler.send_json({"ok": False, "error": "missing"}, 404)
                return True
            handler.send_json(detail)
            return True
    if path == "/api/attendance/events":
        qs = parse_qs(urlparse(handler.path).query)
        day = (qs.get("day") or qs.get("date") or [None])[0]
        try:
            limit = int((qs.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        handler.send_json(
            {
                "ok": True,
                "sessions": svc.list_sessions(day=day),
                "events": svc.list_events(day=day, limit=limit),
                "summary": svc.store.summary_today(),
            }
        )
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _post(handler: Any, path: str) -> bool:
    svc = get_service()
    if path == "/api/attendance/preview-faces":
        # Live enroll preview: detect faces + highlight who would be enrolled.
        body = {}
        try:
            body = handler.read_json() or {}
        except Exception:
            body = {}
        jpeg = None
        b64 = body.get("image_base64") or body.get("photo_base64")
        if b64:
            try:
                jpeg = svc._decode_b64(str(b64))
            except Exception:
                jpeg = None
        if not jpeg:
            cam = body.get("camera_id")
            if cam is None:
                cam = svc.get_settings().get("entrance_camera_id")
            if cam is None:
                handler.send_json(
                    {"ok": False, "error": "Set entrance camera or send image_base64"},
                    400,
                )
                return True
            get_jpeg = _jpeg_provider(handler)
            jpeg = get_jpeg(int(cam))
        if not jpeg:
            handler.send_json(
                {"ok": False, "error": "No snapshot from entrance camera"}, 400
            )
            return True
        try:
            out = svc.preview_faces(jpeg)
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/attendance/staff":
        body = handler.read_json()
        try:
            face_norm = body.get("face_norm")
            if isinstance(face_norm, list):
                face_norm = [float(v) for v in face_norm[:4]]
            else:
                face_norm = None
            face_index = body.get("face_index")
            try:
                face_index = int(face_index) if face_index is not None else None
            except (TypeError, ValueError):
                face_index = None
            staff = svc.enroll(
                name=str(body.get("name") or ""),
                role=str(body.get("role") or "staff"),
                image_base64=body.get("image_base64") or body.get("photo_base64"),
                face_index=face_index,
                face_norm=face_norm,
            )
        except FaceEngineError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json({"ok": True, "staff": staff})
        return True
    if path.startswith("/api/attendance/staff/") and path.endswith("/faces"):
        mid = path[len("/api/attendance/staff/") : -len("/faces")].strip("/")
        try:
            sid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        body = handler.read_json()
        try:
            out = svc.add_face(
                sid,
                image_base64=body.get("image_base64") or body.get("photo_base64"),
            )
        except FaceEngineError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json({"ok": True, **out})
        return True
    if path == "/api/attendance/identify":
        # Dry match only — does not check in / out (separate from Add staff).
        body = {}
        try:
            body = handler.read_json() or {}
        except Exception:
            body = {}
        jpeg = None
        b64 = body.get("image_base64") or body.get("photo_base64")
        if b64:
            try:
                jpeg = svc._decode_b64(str(b64))
            except Exception:
                jpeg = None
        if not jpeg:
            cam = body.get("camera_id")
            if cam is None:
                cam = svc.get_settings().get("entrance_camera_id")
            if cam is None:
                handler.send_json(
                    {"ok": False, "error": "Set entrance camera or send image_base64"},
                    400,
                )
                return True
            get_jpeg = _jpeg_provider(handler)
            jpeg = get_jpeg(int(cam))
        if not jpeg:
            handler.send_json(
                {"ok": False, "error": "No snapshot from entrance camera"}, 400
            )
            return True
        face_norm = body.get("face_norm")
        if isinstance(face_norm, list):
            try:
                face_norm = [float(v) for v in face_norm[:4]]
            except (TypeError, ValueError):
                face_norm = None
        else:
            face_norm = None
        try:
            out = svc.identify(jpeg, face_norm=face_norm)
        except FaceEngineError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json(out)
        return True
    if path == "/api/attendance/scan":
        body = handler.read_json()
        force = bool(body.get("force"))
        jpeg_b64 = body.get("image_base64") or body.get("photo_base64")
        try:
            if jpeg_b64:
                jpeg = svc._decode_b64(str(jpeg_b64))
                cam = body.get("camera_id")
                result = svc.process_frame(
                    jpeg,
                    camera_id=int(cam) if cam is not None else None,
                    force=force,
                )
            else:
                get_jpeg = _jpeg_provider(handler)
                result = svc.scan_entrance(get_jpeg, force=force)
        except FaceEngineError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json(result)
        return True
    if path == "/api/attendance/watcher/start":
        handler.send_json(AttendanceWatcher.instance().start())
        return True
    if path == "/api/attendance/watcher/stop":
        handler.send_json(AttendanceWatcher.instance().stop())
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _put(handler: Any, path: str) -> bool:
    svc = get_service()
    if path == "/api/attendance/settings":
        body = handler.read_json()
        settings = svc.update_settings(body)
        handler.send_json({"ok": True, "settings": settings})
        return True
    if path.startswith("/api/attendance/staff/"):
        mid = path[len("/api/attendance/staff/") :].strip("/")
        try:
            sid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        body = handler.read_json()
        if "active" in body:
            staff = svc.set_active(sid, bool(body.get("active")))
            if staff is None:
                handler.send_json({"ok": False, "error": "missing"}, 404)
                return True
            handler.send_json({"ok": True, "staff": staff})
            return True
        handler.send_json({"ok": False, "error": "nothing to update"}, 400)
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _delete(handler: Any, path: str) -> bool:
    svc = get_service()
    if path.startswith("/api/attendance/staff/"):
        mid = path[len("/api/attendance/staff/") :].strip("/")
        try:
            sid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        ok = svc.delete_staff(sid)
        handler.send_json({"ok": ok, "error": "" if ok else "missing"}, 200 if ok else 404)
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _jpeg_provider(handler: Any) -> Callable[[int], bytes | None]:
    """Prefer live hub snapshot when available on the handler module."""
    hub = getattr(handler, "hub", None)
    if hub is None:
        # Handler class methods use module-level HUB via closure in server — import lazily.
        try:
            from v380.api.http import server as srv

            hub = getattr(srv, "HUB", None)
        except Exception:
            hub = None

    def get_jpeg(cam_id: int) -> bytes | None:
        if hub is None:
            return None
        # Prefer HD main-stream grab used by face AI.
        fn = getattr(hub, "face_jpeg", None) or getattr(hub, "snapshot", None)
        if callable(fn):
            return fn(int(cam_id))
        return None

    return get_jpeg
