"""HTTP routes for /api/occupancy/lab/* — Detect Lab (experimental)."""

from __future__ import annotations

from typing import Any

from v380.occupancy.lab.service import get_lab_service


def try_handle(handler: Any, method: str, path: str) -> bool:
    if not path.startswith("/api/occupancy/lab"):
        return False
    svc = get_lab_service()
    if method == "GET":
        return _get(handler, path, svc)
    if method == "POST":
        return _post(handler, path, svc)
    if method == "DELETE":
        return _delete(handler, path, svc)
    handler.send_json({"ok": False, "error": "method not allowed"}, 405)
    return True


def _get(handler: Any, path: str, svc: Any) -> bool:
    if path in {"/api/occupancy/lab", "/api/occupancy/lab/status"}:
        handler.send_json(svc.status())
        return True
    if path == "/api/occupancy/lab/models":
        handler.send_json({"ok": True, "models": svc.status()["models"]})
        return True
    if path == "/api/occupancy/lab/configs":
        handler.send_json({"ok": True, "configs": svc.store.list_configs()})
        return True
    if path.startswith("/api/occupancy/lab/configs/"):
        cid = path.rsplit("/", 1)[-1]
        doc = svc.store.get_config(cid)
        if not doc:
            handler.send_json({"ok": False, "error": "missing"}, 404)
            return True
        handler.send_json({"ok": True, "config": doc})
        return True
    if path == "/api/occupancy/lab/results":
        handler.send_json({"ok": True, "results": svc.store.list_results()})
        return True
    if path.startswith("/api/occupancy/lab/results/") and path.endswith("/csv"):
        rid = path[len("/api/occupancy/lab/results/") : -len("/csv")].strip("/")
        csv_text, err = svc.export_csv(rid)
        if err:
            handler.send_json({"ok": False, "error": err}, 404)
            return True
        handler.send_json({"ok": True, "csv": csv_text, "result_id": rid})
        return True
    if path.startswith("/api/occupancy/lab/results/"):
        rid = path.rsplit("/", 1)[-1]
        doc = svc.store.get_result(rid)
        if not doc:
            handler.send_json({"ok": False, "error": "missing"}, 404)
            return True
        handler.send_json({"ok": True, "result": doc})
        return True
    if path == "/api/occupancy/lab/ground-truth":
        handler.send_json({"ok": True, "ground_truth": svc.store.list_ground_truth()})
        return True
    if path.startswith("/api/occupancy/lab/ground-truth/"):
        gid = path.rsplit("/", 1)[-1]
        doc = svc.store.get_ground_truth(gid)
        if not doc:
            handler.send_json({"ok": False, "error": "missing"}, 404)
            return True
        handler.send_json({"ok": True, "ground_truth": doc})
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _post(handler: Any, path: str, svc: Any) -> bool:
    body: dict[str, Any] = {}
    try:
        body = handler.read_json() or {}
    except Exception:
        body = {}

    if path == "/api/occupancy/lab/detect":
        err = _maybe_inject_camera_jpeg(handler, body)
        if err:
            handler.send_json({"ok": False, "error": err}, 400)
            return True
        out = svc.detect(body)
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/occupancy/lab/compare":
        err = _maybe_inject_camera_jpeg(handler, body)
        if err:
            handler.send_json({"ok": False, "error": err}, 400)
            return True
        out = svc.compare(body)
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/occupancy/lab/evaluate":
        out = svc.evaluate(body)
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/occupancy/lab/occupancy-experiment":
        out = svc.occupancy_experiment(body)
        handler.send_json(out, 200 if out.get("ok") else 400)
        return True
    if path == "/api/occupancy/lab/configs":
        name = str(body.get("name") or "")
        settings = body.get("settings") or body
        doc = svc.store.save_config(name, settings if "settings" in body else body)
        handler.send_json({"ok": True, "config": doc})
        return True
    if path == "/api/occupancy/lab/results":
        name = str(body.get("name") or "")
        payload = {k: v for k, v in body.items() if k != "name"}
        doc = svc.store.save_result(name, payload)
        handler.send_json({"ok": True, "result": doc})
        return True
    if path == "/api/occupancy/lab/ground-truth":
        name = str(body.get("name") or "")
        anns = body.get("annotations") or body.get("boxes") or []
        if not isinstance(anns, list):
            handler.send_json({"ok": False, "error": "annotations must be a list"}, 400)
            return True
        doc = svc.store.save_ground_truth(name, anns, meta=body.get("meta"))
        handler.send_json({"ok": True, "ground_truth": doc})
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _maybe_inject_camera_jpeg(handler: Any, body: dict[str, Any]) -> str | None:
    """If source=camera (or camera_id set without image), pull a live JPEG into body."""
    import base64

    source = str(body.get("source") or "").strip().lower()
    has_img = bool(body.get("image_base64") or body.get("photo_base64"))
    cam_raw = body.get("camera_id")
    want_cam = source in {"camera", "live", "cam"} or (
        cam_raw is not None and not has_img
    )
    if not want_cam:
        return None
    if cam_raw is None:
        # Fall back to Floor occupancy camera.
        try:
            from v380.occupancy.service import get_service

            cam_raw = get_service().get_settings().get("camera_id")
        except Exception:
            cam_raw = None
    if cam_raw is None:
        return "Set camera_id or Floor camera for live detect"
    try:
        cam_id = int(cam_raw)
    except (TypeError, ValueError):
        return "Invalid camera_id"
    jpeg = _grab_camera_jpeg(handler, cam_id)
    if not jpeg:
        return f"No live snapshot from camera {cam_id}"
    body["image_base64"] = base64.b64encode(jpeg).decode("ascii")
    body["source"] = "camera"
    body["camera_id"] = cam_id
    return None


def _grab_camera_jpeg(handler: Any, cam_id: int) -> bytes | None:
    hub = getattr(handler, "hub", None)
    if hub is None:
        try:
            from v380.api.http import server as srv

            hub = getattr(srv, "HUB", None)
        except Exception:
            hub = None
    if hub is None:
        return None
    cam_id = int(cam_id)
    for name in ("snapshot", "attendance_jpeg", "face_jpeg"):
        fn = getattr(hub, name, None)
        if not callable(fn):
            continue
        try:
            jpeg = fn(cam_id)
        except TypeError:
            continue
        except Exception:
            continue
        if jpeg and len(jpeg) > 1000:
            return jpeg
    return None


def _delete(handler: Any, path: str, svc: Any) -> bool:
    if path.startswith("/api/occupancy/lab/configs/"):
        cid = path.rsplit("/", 1)[-1]
        ok = svc.store.delete_config(cid)
        handler.send_json({"ok": ok}, 200 if ok else 404)
        return True
    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True
