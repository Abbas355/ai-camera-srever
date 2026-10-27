"""Public read-only attendance API for external software (API-key auth).

Base: /api/v1/attendance
Auth: Authorization: Bearer opa_…  or  X-Api-Key: opa_…
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from v380.integrations.api_keys import SCOPE_ATTENDANCE_READ, ApiKeyStore
from v380.attendance.service import get_service


PUBLIC_BASE = "/api/v1/attendance"
MANAGE_BASE = "/api/integrations/attendance"


def extract_api_key(handler: Any) -> str | None:
    hdr = handler.headers.get("Authorization") or ""
    if hdr.lower().startswith("bearer "):
        tok = hdr[7:].strip()
        if tok.startswith("opa_"):
            return tok
    x = (handler.headers.get("X-Api-Key") or "").strip()
    if x.startswith("opa_"):
        return x
    # Query fallback for simple browser tests (optional).
    try:
        qs = parse_qs(urlparse(handler.path).query or "")
        q = (qs.get("api_key") or [""])[0].strip()
        if q.startswith("opa_"):
            return q
    except Exception:
        pass
    return None


def need_api_key(handler: Any) -> dict[str, Any] | None:
    raw = extract_api_key(handler)
    rec = ApiKeyStore.instance().authenticate(raw, scope=SCOPE_ATTENDANCE_READ)
    if not rec:
        handler.send_json(
            {"ok": False, "error": "invalid or missing API key"},
            401,
        )
        return None
    return rec


def try_handle_public(handler: Any, method: str, path: str) -> bool:
    """Handle /api/v1/attendance/* — API key only, no staff login."""
    if not path.startswith(PUBLIC_BASE):
        return False
    if method != "GET":
        handler.send_json({"ok": False, "error": "method not allowed"}, 405)
        return True
    if not need_api_key(handler):
        return True

    svc = get_service()
    # /api/v1/attendance or /api/v1/attendance/
    rest = path[len(PUBLIC_BASE) :].strip("/")

    if rest in {"", "ping"}:
        handler.send_json(
            {
                "ok": True,
                "service": "optiview-attendance",
                "version": "v1",
                "scope": SCOPE_ATTENDANCE_READ,
            }
        )
        return True

    if rest == "status":
        st = svc.status()
        # Strip internal noise for external consumers.
        handler.send_json(
            {
                "ok": True,
                "face_engine": st.get("face_engine"),
                "watcher": {
                    "running": (st.get("watcher") or {}).get("running"),
                    "ticks": (st.get("watcher") or {}).get("ticks"),
                },
                "settings": {
                    "enabled": (st.get("settings") or {}).get("enabled"),
                    "match_threshold": (st.get("settings") or {}).get("match_threshold"),
                },
                "summary": st.get("summary"),
            }
        )
        return True

    if rest == "staff":
        staff = svc.list_staff()
        # Never expose embeddings / absolute photo paths to external apps.
        safe = [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "role": s.get("role"),
                "active": s.get("active"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "photo_url": f"{PUBLIC_BASE}/staff/{s.get('id')}/photo",
            }
            for s in staff
            if s.get("active") is not False
        ]
        handler.send_json({"ok": True, "staff": safe})
        return True

    if rest == "events":
        qs = parse_qs(urlparse(handler.path).query)
        day = (qs.get("day") or qs.get("date") or [None])[0]
        try:
            limit = int((qs.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        handler.send_json(
            {
                "ok": True,
                "day": day,
                "sessions": svc.list_sessions(day=day),
                "events": svc.list_events(day=day, limit=limit),
                "summary": svc.store.summary_today() if not day else None,
            }
        )
        return True

    if rest.startswith("staff/"):
        parts = rest.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            sid = int(parts[1])
            if len(parts) == 3 and parts[2] == "photo":
                from pathlib import Path

                photo = svc.store.photo_path_for(sid)
                if photo is None:
                    handler.send_json({"ok": False, "error": "no photo"}, 404)
                    return True
                data = Path(photo).read_bytes()
                handler.send_response(200)
                handler.cors()
                handler.send_header("Content-Type", "image/jpeg")
                handler.send_header("Content-Length", str(len(data)))
                handler.send_header("Cache-Control", "private, max-age=60")
                handler.end_headers()
                handler.wfile.write(data)
                return True
            if len(parts) == 2:
                qs = parse_qs(urlparse(handler.path).query)
                try:
                    days = int((qs.get("days") or ["30"])[0])
                except ValueError:
                    days = 30
                detail = svc.staff_detail(sid, days=days)
                if detail is None:
                    handler.send_json({"ok": False, "error": "missing"}, 404)
                    return True
                # Drop absolute photo_path from nested staff.
                staff = dict(detail.get("staff") or {})
                staff.pop("photo_path", None)
                staff["photo_url"] = f"{PUBLIC_BASE}/staff/{sid}/photo"
                handler.send_json({**detail, "staff": staff})
                return True

    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def try_handle_manage(handler: Any, method: str, path: str) -> bool:
    """Staff-login management of attendance API keys + docs meta."""
    if not path.startswith(MANAGE_BASE):
        return False
    store = ApiKeyStore.instance()

    if path == f"{MANAGE_BASE}/docs" and method == "GET":
        import os
        import socket

        port = int(os.environ.get("V380_PORT", "8080"))
        ips: list[str] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ips.append(sock.getsockname()[0])
            sock.close()
        except OSError:
            pass
        if not ips:
            ips = ["127.0.0.1"]
        primary = ips[0]
        base = f"http://{primary}:{port}{PUBLIC_BASE}"
        handler.send_json(
            {
                "ok": True,
                "base_url": base,
                "auth_header": "Authorization: Bearer opa_YOUR_KEY",
                "alt_header": "X-Api-Key: opa_YOUR_KEY",
                "note": "External sites call these endpoints with the API key. Do not use staff login tokens.",
                "endpoints": [
                    {"name": "Ping (verify key)", "method": "GET", "path": "/ping"},
                    {"name": "Status", "method": "GET", "path": "/status"},
                    {"name": "Staff list", "method": "GET", "path": "/staff"},
                    {
                        "name": "Events & sessions",
                        "method": "GET",
                        "path": "/events?day=YYYY-MM-DD&limit=200",
                    },
                    {
                        "name": "Staff detail",
                        "method": "GET",
                        "path": "/staff/{id}?days=30",
                    },
                ],
                "ips": ips,
                "port": port,
            }
        )
        return True

    if path == f"{MANAGE_BASE}/keys":
        if method == "GET":
            handler.send_json({"ok": True, "keys": store.list_keys()})
            return True
        if method == "POST":
            body = handler.read_json() or {}
            name = str(body.get("name") or "Integration").strip()
            out = store.create(name)
            handler.send_json(out, 201)
            return True
        handler.send_json({"ok": False, "error": "method not allowed"}, 405)
        return True

    if path.startswith(f"{MANAGE_BASE}/keys/"):
        mid = path[len(f"{MANAGE_BASE}/keys/") :].strip("/")
        parts = mid.split("/")
        try:
            kid = int(parts[0])
        except ValueError:
            handler.send_json({"ok": False, "error": "bad id"}, 400)
            return True
        if len(parts) == 1 and method == "DELETE":
            ok = store.revoke(kid)
            handler.send_json({"ok": ok, "revoked": ok}, 200 if ok else 404)
            return True
        if len(parts) == 2 and parts[1] == "rotate" and method == "POST":
            out = store.rotate(kid)
            if out is None:
                handler.send_json({"ok": False, "error": "not found"}, 404)
                return True
            handler.send_json(out)
            return True

    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True
