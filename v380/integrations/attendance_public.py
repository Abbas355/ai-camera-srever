"""Public read-only attendance API for external software (API-key auth).

Base: /api/v1/attendance
Auth: Authorization: Bearer opa_…  or  X-Api-Key: opa_…

Staff-login preview (same JSON shapes, no API key):
  GET /api/integrations/attendance/preview/{ping|status|staff|events}
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


def public_ping_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "optiview-attendance",
        "version": "v1",
        "scope": SCOPE_ATTENDANCE_READ,
    }


def public_status_payload(svc: Any | None = None) -> dict[str, Any]:
    svc = svc or get_service()
    st = svc.status()
    return {
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


def public_staff_payload(svc: Any | None = None) -> dict[str, Any]:
    svc = svc or get_service()
    staff = svc.list_staff()
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
    return {"ok": True, "staff": safe}


def public_events_payload(
    *,
    day: str | None = None,
    limit: int = 200,
    svc: Any | None = None,
) -> dict[str, Any]:
    svc = svc or get_service()
    limit = max(1, min(1000, int(limit)))
    return {
        "ok": True,
        "day": day,
        "sessions": svc.list_sessions(day=day),
        "events": svc.list_events(day=day, limit=limit),
        "summary": svc.store.summary_today() if not day else None,
    }


def _parse_ymd(raw: str | None):
    from datetime import datetime

    key = (raw or "").strip()[:10]
    if not key:
        return None
    try:
        return datetime.strptime(key, "%Y-%m-%d").date()
    except ValueError:
        return None


def _session_day_key(session: dict[str, Any]) -> str:
    day = str(session.get("day") or "").strip()
    if len(day) >= 10:
        return day[:10]
    cin = str(session.get("check_in") or "").strip()
    return cin[:10] if len(cin) >= 10 else ""


def public_staff_detail_payload(
    staff_id: int,
    *,
    day: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    svc: Any | None = None,
) -> dict[str, Any] | None:
    """Staff profile + history.

    Date filters (optional):
      • ``day=YYYY-MM-DD`` — one calendar day
      • ``from=YYYY-MM-DD&to=YYYY-MM-DD`` — inclusive range
        (aliases: ``date_from`` / ``date_to``)

    When a day/range is set, response also includes:
      ``from``, ``to``, ``sessions_in_range``, ``calendar_in_range``
      (and for a single day: ``day``, ``sessions_on_day``, ``day_status``).
    """
    from datetime import datetime

    svc = svc or get_service()

    single = _parse_ymd(day)
    start = _parse_ymd(date_from)
    end = _parse_ymd(date_to)
    if single is not None and start is None and end is None:
        start = end = single
    if start is not None and end is None:
        end = start
    if end is not None and start is None:
        start = end
    if start is not None and end is not None and start > end:
        start, end = end, start

    # Lookback for staff_detail — derived from the filter, not a client ``days`` param.
    today = datetime.now().date()
    if start is not None and end is not None:
        need = max((today - start).days + 1, (end - start).days + 1, 1)
        days = max(1, min(120, int(need)))
    else:
        days = 30

    detail = svc.staff_detail(int(staff_id), days=days)
    if detail is None:
        return None
    staff = dict(detail.get("staff") or {})
    staff.pop("photo_path", None)
    staff["photo_url"] = f"{PUBLIC_BASE}/staff/{int(staff_id)}/photo"
    out: dict[str, Any] = {**detail, "staff": staff}

    if start is not None and end is not None:
        from_s, to_s = start.isoformat(), end.isoformat()
        sessions = list(detail.get("sessions") or [])
        in_range = [
            s
            for s in sessions
            if from_s <= _session_day_key(s) <= to_s
        ]
        cal = list(detail.get("calendar") or [])
        cal_in_range = [
            c
            for c in cal
            if from_s <= str(c.get("day") or "")[:10] <= to_s
        ]
        out["from"] = from_s
        out["to"] = to_s
        out["sessions_in_range"] = in_range
        out["calendar_in_range"] = cal_in_range
        if from_s == to_s:
            out["day"] = from_s
            out["sessions_on_day"] = in_range
            out["day_status"] = next(
                (c for c in cal_in_range if str(c.get("day") or "")[:10] == from_s),
                {
                    "day": from_s,
                    "present": bool(in_range),
                    "status": (
                        "working"
                        if any(s.get("status") == "working" for s in in_range)
                        else ("present" if in_range else "absent")
                    ),
                    "session_count": len(in_range),
                },
            )
    return out


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
        handler.send_json(public_ping_payload())
        return True

    if rest == "status":
        handler.send_json(public_status_payload(svc))
        return True

    if rest == "staff":
        handler.send_json(public_staff_payload(svc))
        return True

    if rest == "events":
        qs = parse_qs(urlparse(handler.path).query)
        day = (qs.get("day") or qs.get("date") or [None])[0]
        try:
            limit = int((qs.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        handler.send_json(public_events_payload(day=day, limit=limit, svc=svc))
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
                day = (qs.get("day") or qs.get("date") or [None])[0]
                date_from = (qs.get("from") or qs.get("date_from") or [None])[0]
                date_to = (qs.get("to") or qs.get("date_to") or [None])[0]
                detail = public_staff_detail_payload(
                    sid,
                    day=day,
                    date_from=date_from,
                    date_to=date_to,
                    svc=svc,
                )
                if detail is None:
                    handler.send_json({"ok": False, "error": "missing"}, 404)
                    return True
                handler.send_json(detail)
                return True

    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def _handle_preview(handler: Any, method: str, path: str) -> bool:
    """Staff-login sample of public JSON (for in-app Test buttons)."""
    prefix = f"{MANAGE_BASE}/preview"
    if not path.startswith(prefix):
        return False
    if method != "GET":
        handler.send_json({"ok": False, "error": "method not allowed"}, 405)
        return True

    rest = path[len(prefix) :].strip("/")
    qs = parse_qs(urlparse(handler.path).query or "")

    if rest in {"", "ping"}:
        handler.send_json(
            {
                **public_ping_payload(),
                "_preview": True,
                "_note": "Same shape as GET /api/v1/attendance/ping (API key required there).",
            }
        )
        return True

    if rest == "status":
        handler.send_json(
            {
                **public_status_payload(),
                "_preview": True,
                "_note": "Same shape as GET /api/v1/attendance/status",
            }
        )
        return True

    if rest == "staff":
        handler.send_json(
            {
                **public_staff_payload(),
                "_preview": True,
                "_note": "Same shape as GET /api/v1/attendance/staff",
            }
        )
        return True

    if rest == "events":
        day = (qs.get("day") or qs.get("date") or [None])[0]
        try:
            limit = int((qs.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        handler.send_json(
            {
                **public_events_payload(day=day, limit=limit),
                "_preview": True,
                "_note": "Same shape as GET /api/v1/attendance/events",
            }
        )
        return True

    if rest.startswith("staff/") and rest.count("/") == 1:
        sid_s = rest.split("/", 1)[1]
        if sid_s.isdigit():
            day = (qs.get("day") or qs.get("date") or [None])[0]
            date_from = (qs.get("from") or qs.get("date_from") or [None])[0]
            date_to = (qs.get("to") or qs.get("date_to") or [None])[0]
            detail = public_staff_detail_payload(
                int(sid_s),
                day=day,
                date_from=date_from,
                date_to=date_to,
            )
            if detail is None:
                handler.send_json({"ok": False, "error": "missing"}, 404)
                return True
            note = f"Same shape as GET /api/v1/attendance/staff/{sid_s}"
            if date_from or date_to:
                note += f"?from={date_from or ''}&to={date_to or ''}"
            elif day:
                note += f"?day={day}"
            handler.send_json(
                {
                    **detail,
                    "_preview": True,
                    "_note": note,
                }
            )
            return True

    handler.send_json({"ok": False, "error": "not found"}, 404)
    return True


def try_handle_manage(handler: Any, method: str, path: str) -> bool:
    """Staff-login management of attendance API keys + docs meta."""
    if not path.startswith(MANAGE_BASE):
        return False
    store = ApiKeyStore.instance()

    if path.startswith(f"{MANAGE_BASE}/preview"):
        return _handle_preview(handler, method, path)

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
                "note": (
                    "External sites call these endpoints with the API key. "
                    "Do not use staff login tokens. Use Test below to see live sample JSON."
                ),
                "endpoints": [
                    {
                        "name": "Ping (verify key)",
                        "method": "GET",
                        "path": "/ping",
                        "preview": "/api/integrations/attendance/preview/ping",
                    },
                    {
                        "name": "Status",
                        "method": "GET",
                        "path": "/status",
                        "preview": "/api/integrations/attendance/preview/status",
                    },
                    {
                        "name": "Staff list",
                        "method": "GET",
                        "path": "/staff",
                        "preview": "/api/integrations/attendance/preview/staff",
                    },
                    {
                        "name": "Events & sessions",
                        "method": "GET",
                        "path": "/events?day=YYYY-MM-DD&limit=200",
                        "preview": "/api/integrations/attendance/preview/events?limit=200",
                    },
                    {
                        "name": "Staff detail (one day)",
                        "method": "GET",
                        "path": "/staff/{id}?day=YYYY-MM-DD",
                        "preview": "/api/integrations/attendance/preview/staff/{id}?day=YYYY-MM-DD",
                    },
                    {
                        "name": "Staff detail (date range)",
                        "method": "GET",
                        "path": "/staff/{id}?from=YYYY-MM-DD&to=YYYY-MM-DD",
                        "preview": "/api/integrations/attendance/preview/staff/{id}?from=YYYY-MM-DD&to=YYYY-MM-DD",
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
