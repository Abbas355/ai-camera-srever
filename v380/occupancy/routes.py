"""HTTP handlers for /api/occupancy/* — isolated feature routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from v380.occupancy.service import get_service
from v380.occupancy.watcher import OccupancyWatcher


def try_handle(handler: Any, method: str, path: str) -> bool:
    if not path.startswith("/api/occupancy"):
        return False
    # Detect Lab (experimental) — isolated under /api/occupancy/lab/*
    if path.startswith("/api/occupancy/lab"):
        from v380.occupancy.lab.routes import try_handle as lab_handle

        return lab_handle(handler, method, path)
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
    if path in {"/api/occupancy", "/api/occupancy/status"}:
        handler.send_json(svc.status())
        return True
    if path == "/api/occupancy/settings":
        handler.send_json({"ok": True, "settings": svc.get_settings()})
        return True
    if path == "/api/occupancy/zones":
        handler.send_json({"ok": True, "zones": svc.list_zones()})
        return True
    if path == "/api/occupancy/sessions":
        qs = parse_qs(urlparse(handler.path).query)
        day = (qs.get("day") or qs.get("date") or [None])[0]
        zone_raw = (qs.get("zone_id") or [None])[0]
        zone_id = int(zone_raw) if zone_raw and str(zone_raw).isdigit() else None
        handler.send_json(
            {
                "ok": True,
                "sessions": svc.list_sessions(day=day, zone_id=zone_id),
                "summary": svc.store.summary_today(),
            }
        )
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _post(handler: Any, path: str) -> bool:
    svc = get_service()
    if path == "/api/occupancy/zones":
        body = handler.read_json()
        try:
            zone = svc.add_zone(body)
        except ValueError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json({"ok": True, "zone": zone})
        return True
    if path == "/api/occupancy/watcher/start":
        handler.send_json(OccupancyWatcher.instance().start())
        return True
    if path == "/api/occupancy/watcher/stop":
        handler.send_json(OccupancyWatcher.instance().stop())
        return True
    if path == "/api/occupancy/scan":
        # Manual one-shot scan of the configured camera.
        settings = svc.get_settings()
        cam = settings.get("camera_id")
        if cam is None:
            handler.send_json({"ok": False, "error": "Set a floor camera first"}, 400)
            return True
        get_jpeg = _jpeg_provider(handler)
        jpeg = get_jpeg(int(cam))
        if not jpeg:
            handler.send_json({"ok": False, "error": "No snapshot from floor camera"}, 400)
            return True
        handler.send_json(svc.process_frame(jpeg, camera_id=int(cam)))
        return True
    if path == "/api/occupancy/detect-tables":
        # Default: preview tables only (no save). Pass save=true to write zones.
        settings = svc.get_settings()
        cam = settings.get("camera_id")
        if cam is None:
            handler.send_json({"ok": False, "error": "Set a floor camera first"}, 400)
            return True
        body = {}
        try:
            body = handler.read_json() or {}
        except Exception:
            body = {}
        get_jpeg = _jpeg_provider(handler)
        jpeg = get_jpeg(int(cam))
        if not jpeg:
            handler.send_json({"ok": False, "error": "No snapshot from floor camera"}, 400)
            return True
        try:
            out = svc.detect_tables(
                jpeg,
                camera_id=int(cam),
                create_missing=bool(body.get("create_missing", False)),
                update_matched=bool(body.get("update_matched", False)),
                replace_all=bool(body.get("replace_all", False)),
                save=bool(body.get("save", False)),
            )
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/occupancy/detect-preview":
        # Preview boxes only: tables / chairs / persons (no zone save).
        settings = svc.get_settings()
        cam = settings.get("camera_id")
        if cam is None:
            handler.send_json({"ok": False, "error": "Set a floor camera first"}, 400)
            return True
        body = {}
        try:
            body = handler.read_json() or {}
        except Exception:
            body = {}
        get_jpeg = _jpeg_provider(handler)
        jpeg = get_jpeg(int(cam))
        if not jpeg:
            handler.send_json({"ok": False, "error": "No snapshot from floor camera"}, 400)
            return True
        try:
            out = svc.preview_detect(
                jpeg,
                tables=bool(body.get("tables", True)),
                chairs=bool(body.get("chairs", True)),
                persons=bool(body.get("persons", True)),
            )
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/occupancy/detect-image":
        # Manual test: run YOLO on an uploaded still (no camera, no zone save).
        body = {}
        try:
            body = handler.read_json() or {}
        except Exception:
            body = {}
        jpeg = _decode_image_bytes(
            body.get("image_base64") or body.get("photo_base64")
        )
        if not jpeg:
            handler.send_json(
                {"ok": False, "error": "Provide image_base64 (JPEG/PNG)"}, 400
            )
            return True
        try:
            out = svc.preview_detect(
                jpeg,
                tables=bool(body.get("tables", True)),
                chairs=bool(body.get("chairs", True)),
                persons=bool(body.get("persons", True)),
            )
            out["source"] = "image"
        except Exception as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 500)
            return True
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _put(handler: Any, path: str) -> bool:
    svc = get_service()
    if path == "/api/occupancy/settings":
        body = handler.read_json()
        settings = svc.update_settings(body)
        handler.send_json({"ok": True, "settings": settings})
        return True
    if path.startswith("/api/occupancy/zones/"):
        mid = path[len("/api/occupancy/zones/") :].strip("/")
        try:
            zid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        body = handler.read_json()
        try:
            zone = svc.update_zone(zid, body)
        except ValueError as exc:
            handler.send_json({"ok": False, "error": str(exc)}, 400)
            return True
        if zone is None:
            handler.send_json({"ok": False, "error": "missing"}, 404)
            return True
        handler.send_json({"ok": True, "zone": zone})
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _delete(handler: Any, path: str) -> bool:
    svc = get_service()
    if path.startswith("/api/occupancy/zones/"):
        mid = path[len("/api/occupancy/zones/") :].strip("/")
        try:
            zid = int(mid)
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        ok = svc.delete_zone(zid)
        handler.send_json({"ok": ok, "error": "" if ok else "missing"}, 200 if ok else 404)
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _decode_image_bytes(raw: Any) -> bytes | None:
    """Decode data-URL or plain base64 image to bytes (JPEG/PNG via cv2)."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        return data if data else None
    s = str(raw).strip()
    if not s:
        return None
    if "," in s and s.lower().startswith("data:"):
        s = s.split(",", 1)[1]
    try:
        import base64

        data = base64.b64decode(s, validate=False)
    except Exception:
        return None
    return data if data else None


def _jpeg_provider(handler: Any) -> Any:
    hub = getattr(handler, "hub", None)
    if hub is None:
        try:
            from v380.api.http import server as srv

            hub = getattr(srv, "HUB", None)
        except Exception:
            hub = None

    def get_jpeg(cam_id: int):
        """Freshest live frame for detect — prefer live JPEG, then attendance, then cache."""
        if hub is None:
            return None
        cam_id = int(cam_id)
        # 1) Live MJPEG frame (same as what the Floor UI shows).
        snap = getattr(hub, "snapshot", None)
        if callable(snap):
            jpeg = snap(cam_id)
            if jpeg and len(jpeg) > 8_000:
                return jpeg
        # 2) Short-age attendance grab (forces fresher sample when live is empty).
        att = getattr(hub, "attendance_jpeg", None)
        if callable(att):
            try:
                jpeg = att(cam_id)
                if jpeg:
                    return jpeg
            except TypeError:
                pass
        face = getattr(hub, "face_jpeg", None)
        if callable(face):
            return face(cam_id)
        if callable(snap):
            return snap(cam_id)
        return None

    return get_jpeg
