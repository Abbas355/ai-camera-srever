"""ONVIF helpers for V380-style cameras (often :8899)."""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET

_PATHS = ("/onvif/ptz_service", "/onvif/ptz", "/")


def _auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _soap(ip: str, port: int, action: str, body: str, username: str, password: str, timeout: float = 4.0) -> bytes:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
        "<s:Body>"
        f"{body}"
        "</s:Body></s:Envelope>"
    )
    data = envelope.encode("utf-8")
    headers = {
        "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
        "Content-Length": str(len(data)),
    }
    if username:
        headers["Authorization"] = _auth_header(username, password or "")
    last_exc: Exception | None = None
    for path in _PATHS:
        url = f"http://{ip}:{port}{path}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read() or b""
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return b""


def continuous_move(
    ip: str,
    username: str,
    password: str,
    x: float,
    y: float,
    port: int = 8899,
    zoom: float = 0.0,
) -> bool:
    x = max(-1.0, min(1.0, float(x)))
    y = max(-1.0, min(1.0, float(y)))
    z = max(-1.0, min(1.0, float(zoom)))
    body = (
        "<tptz:ContinuousMove>"
        "<tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>"
        "<tptz:Velocity>"
        f'<tt:PanTilt x="{x}" y="{y}"/>'
        f'<tt:Zoom x="{z}"/>'
        "</tptz:Velocity>"
        "</tptz:ContinuousMove>"
    )
    try:
        _soap(ip, port, "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove", body, username, password)
        return True
    except Exception:
        return False


def stop_move(ip: str, username: str, password: str, port: int = 8899) -> bool:
    body = (
        "<tptz:Stop>"
        "<tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>"
        "<tptz:PanTilt>true</tptz:PanTilt>"
        "<tptz:Zoom>true</tptz:Zoom>"
        "</tptz:Stop>"
    )
    try:
        _soap(ip, port, "http://www.onvif.org/ver20/ptz/wsdl/Stop", body, username, password)
        return True
    except Exception:
        return False


def set_preset(
    ip: str,
    username: str,
    password: str,
    slot: int,
    name: str = "",
    port: int = 8899,
) -> bool:
    slot = max(1, min(6, int(slot)))
    token = str(slot)
    label = (name or f"Preset {slot}").replace("<", "").replace(">", "")[:32]
    body = (
        "<tptz:SetPreset>"
        "<tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>"
        f"<tptz:PresetName>{label}</tptz:PresetName>"
        f"<tptz:PresetToken>{token}</tptz:PresetToken>"
        "</tptz:SetPreset>"
    )
    try:
        _soap(ip, port, "http://www.onvif.org/ver20/ptz/wsdl/SetPreset", body, username, password)
        return True
    except Exception:
        body2 = (
            "<tptz:SetPreset>"
            "<tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>"
            f"<tptz:PresetName>{label}</tptz:PresetName>"
            "</tptz:SetPreset>"
        )
        try:
            _soap(ip, port, "http://www.onvif.org/ver20/ptz/wsdl/SetPreset", body2, username, password)
            return True
        except Exception:
            return False


def goto_preset(ip: str, username: str, password: str, slot: int, port: int = 8899) -> bool:
    slot = max(1, min(6, int(slot)))
    body = (
        "<tptz:GotoPreset>"
        "<tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>"
        f"<tptz:PresetToken>{slot}</tptz:PresetToken>"
        "</tptz:GotoPreset>"
    )
    try:
        _soap(ip, port, "http://www.onvif.org/ver20/ptz/wsdl/GotoPreset", body, username, password, timeout=8)
        return True
    except Exception:
        return False


def list_preset_tokens(ip: str, username: str, password: str, port: int = 8899) -> list[str]:
    body = (
        "<tptz:GetPresets>"
        "<tptz:ProfileToken>PROFILE_000</tptz:ProfileToken>"
        "</tptz:GetPresets>"
    )
    try:
        raw = _soap(ip, port, "http://www.onvif.org/ver20/ptz/wsdl/GetPresets", body, username, password)
    except Exception:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: list[str] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag in ("token", "PresetToken") and el.text:
            out.append(el.text.strip())
    return out
