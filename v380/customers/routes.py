"""HTTP handlers for /api/customers/* — isolated; does not touch other routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from v380.attendance.face_engine import FaceEngineError
from v380.customers.service import get_service
from v380.customers.watcher import CustomerWatcher


def try_handle(handler: Any, method: str, path: str) -> bool:
    if not path.startswith("/api/customers"):
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
    if path in {"/api/customers", "/api/customers/status"}:
        handler.send_json(svc.status())
        return True
    if path == "/api/customers/settings":
        handler.send_json({"ok": True, "settings": svc.get_settings()})
        return True
    if path == "/api/customers/list":
        handler.send_json({"ok": True, "customers": svc.list_customers(), "summary": svc.store.summary_today()})
        return True
    if path == "/api/customers/open":
        handler.send_json({"ok": True, "visits": svc.open_visits()})
        return True
    if path.startswith("/api/customers/") and path.endswith("/photo"):
        mid = path[len("/api/customers/") : -len("/photo")].strip("/")
        try:
            cid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        photo = svc.store.photo_path_for(cid)
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
    if path.startswith("/api/customers/"):
        mid = path[len("/api/customers/") :].strip("/")
        if mid.isdigit():
            detail = svc.customer_detail(int(mid))
            if detail is None:
                handler.send_json({"ok": False, "error": "missing"}, 404)
                return True
            handler.send_json(detail)
            return True
        # /api/customers/visits/{id}/playback
        if mid.startswith("visits/") and mid.endswith("/playback"):
            raw_id = mid[len("visits/") : -len("/playback")].strip("/")
            try:
                vid = int(raw_id)
            except ValueError:
                handler.send_json({"ok": False, "error": "bad visit id"}, 400)
                return True
            try:
                handler.send_json(svc.playback_for_visit(vid))
            except LookupError as exc:
                handler.send_json({"ok": False, "error": str(exc)}, 404)
            except ValueError as exc:
                handler.send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _post(handler: Any, path: str) -> bool:
    svc = get_service()
    if path == "/api/customers/enroll":
        body = handler.read_json()
        try:
            customer = svc.enroll(
                name=str(body.get("name") or ""),
                image_base64=body.get("image_base64") or body.get("photo_base64"),
                notes=str(body.get("notes") or ""),
            )
        except FaceEngineError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json({"ok": True, "customer": customer})
        return True
    if path == "/api/customers/watcher/start":
        handler.send_json(CustomerWatcher.instance().start())
        return True
    if path == "/api/customers/watcher/stop":
        handler.send_json(CustomerWatcher.instance().stop())
        return True
    if path == "/api/customers/cleanup-staff":
        # Remove guests that look like enrolled Attendance staff.
        handler.send_json(svc.purge_staff_lookalikes())
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _put(handler: Any, path: str) -> bool:
    svc = get_service()
    if path == "/api/customers/settings":
        body = handler.read_json()
        settings = svc.update_settings(body)
        handler.send_json({"ok": True, "settings": settings})
        return True
    if path.startswith("/api/customers/visits/") and path.endswith("/table"):
        mid = path[len("/api/customers/visits/") : -len("/table")].strip("/")
        try:
            vid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        body = handler.read_json()
        tid = body.get("table_id")
        visit = svc.set_visit_table(vid, None if tid in (None, "") else str(tid))
        if visit is None:
            handler.send_json({"ok": False, "error": "missing"}, 404)
            return True
        handler.send_json({"ok": True, "visit": visit})
        return True
    if path.startswith("/api/customers/"):
        mid = path[len("/api/customers/") :].strip("/")
        try:
            cid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        body = handler.read_json()
        customer = svc.rename(cid, str(body.get("name") or "")) if "name" in body else None
        if "notes" in body or "active" in body:
            customer = svc.store.update_customer(
                cid,
                name=body.get("name"),
                notes=body.get("notes"),
                active=body.get("active"),
            )
        if customer is None:
            handler.send_json({"ok": False, "error": "missing"}, 404)
            return True
        handler.send_json({"ok": True, "customer": customer})
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _delete(handler: Any, path: str) -> bool:
    svc = get_service()
    if path.startswith("/api/customers/"):
        mid = path[len("/api/customers/") :].strip("/")
        try:
            cid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        ok = svc.delete(cid)
        handler.send_json({"ok": ok, "error": "" if ok else "missing"}, 200 if ok else 404)
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True
