"""EZVIZ local PTZ helpers — ISAPI / ONVIF when the camera exposes them."""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

_ONVIF_PATHS = (
    "/onvif/ptz_service",
    "/onvif/PTZ",
    "/onvif/ptz",
    "/onvif/device_service",
    "/onvif/device",
)
_ISAPI_CONTINUOUS = (
    "/ISAPI/PTZCtrl/channels/1/continuous",
    "/ISAPI/PTZCtrl/channels/1/Continuous",
    "/ISAPI/PTZCtrl/channels/101/continuous",
    "/ISAPI/PTZCtrl/channels/101/Continuous",
)
_ISAPI_MOMENTARY = (
    "/ISAPI/PTZCtrl/channels/1/momentary",
    "/ISAPI/PTZCtrl/channels/1/Momentary",
    "/ISAPI/PTZCtrl/channels/101/momentary",
    "/ISAPI/PTZCtrl/channels/101/Momentary",
)
_DEFAULT_PORTS = (80, 8000)


def _password_variants(password: str) -> list[str]:
    """Raw verify-code plus EZVIZ LAN MD5 form used by some local services."""
    raw = (password or "").strip()
    out: list[str] = []
    for cand in (raw, raw.upper()):
        if cand and cand not in out:
            out.append(cand)
    if raw:
        try:
            hashed = hashlib.md5(raw.encode("utf-8")).hexdigest()[8:24].lower()
            if hashed not in out:
                out.append(hashed)
        except Exception:
            pass
    return out or [""]


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _digest_header(user: str, password: str, method: str, uri: str, challenge: str) -> str | None:
    fields: dict[str, str] = {}
    for key, val in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', challenge):
        fields[key.lower()] = val or ""
    realm = fields.get("realm") or ""
    nonce = fields.get("nonce") or ""
    qop = (fields.get("qop") or "").split(",")[0].strip()
    opaque = fields.get("opaque") or ""
    algo = (fields.get("algorithm") or "MD5").upper()
    if not realm or not nonce or "MD5" not in algo:
        return None
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode("utf-8")).hexdigest()
    nc = "00000001"
    cnonce = hashlib.md5(f"{nonce}{user}".encode("utf-8")).hexdigest()[:16]
    if qop:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode("utf-8")).hexdigest()
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
    parts = [
        f'username="{user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f'algorithm="{algo}"',
    ]
    if qop:
        parts.extend([f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'])
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def _http(
    url: str,
    data: bytes | None,
    user: str,
    password: str,
    content_type: str,
    method: str | None = None,
    timeout: float = 0.8,
) -> bytes:
    if method is None:
        method = "PUT" if data is not None else "GET"
    headers = {"Content-Type": content_type, "Connection": "close"}
    if data is not None:
        headers["Content-Length"] = str(len(data))
    headers["Authorization"] = _basic(user, password)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(url)) as resp:
            return resp.read() or b""
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        challenge = exc.headers.get("WWW-Authenticate") or ""
        uri = urlparse(url).path or "/"
        digest = _digest_header(user, password, method, uri, challenge)
        if not digest:
            raise
        headers["Authorization"] = digest
        req2 = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req2, timeout=timeout, context=_ssl_ctx(url)) as resp:
            return resp.read() or b""


def _ssl_ctx(url: str):
    if not url.lower().startswith("https://"):
        return None
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _soap_body(inner: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    ).encode("utf-8")


def _scheme_ports(ports: tuple[int, ...]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for port in ports:
        out.append(("http", port))
    seen: set[tuple[str, int]] = set()
    uniq: list[tuple[str, int]] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        uniq.append(item)
    return uniq


def _cmd_to_velocity(name: str) -> tuple[float, float, float]:
    table = {
        "ptz_left": (-0.5, 0.0, 0.0),
        "ptz_right": (0.5, 0.0, 0.0),
        "ptz_up": (0.0, 0.5, 0.0),
        "ptz_down": (0.0, -0.5, 0.0),
        "ptz_zoom_in": (0.0, 0.0, 0.4),
        "ptz_zoom_out": (0.0, 0.0, -0.4),
        "ptz_stop": (0.0, 0.0, 0.0),
    }
    return table.get(name, (0.0, 0.0, 0.0))


def _profile_token(scheme: str, ip: str, port: int, user: str, password: str) -> str:
    inner = "<trt:GetProfiles/>"
    for path in ("/onvif/media_service", "/onvif/media", "/onvif/device_service", "/onvif/device"):
        try:
            url = f"{scheme}://{ip}:{port}{path}"
            raw = _http(
                url,
                _soap_body(inner),
                user,
                password,
                'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/media/wsdl/GetProfiles"',
                method="POST",
            )
            root = ET.fromstring(raw)
            for el in root.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "Profiles":
                    tok = el.attrib.get("token")
                    if tok:
                        return tok
                if tag == "token" and el.text:
                    return el.text.strip()
        except Exception:
            continue
    return "Profile_1"


def _onvif_move(scheme: str, ip: str, port: int, user: str, password: str, name: str) -> bool:
    token = _profile_token(scheme, ip, port, user, password)
    if name == "ptz_stop":
        inner = (
            "<tptz:Stop>"
            f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt>"
            "<tptz:Zoom>true</tptz:Zoom>"
            "</tptz:Stop>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/Stop"
    else:
        x, y, z = _cmd_to_velocity(name)
        inner = (
            "<tptz:ContinuousMove>"
            f"<tptz:ProfileToken>{token}</tptz:ProfileToken>"
            "<tptz:Velocity>"
            f'<tt:PanTilt x="{x}" y="{y}"/>'
            f'<tt:Zoom x="{z}"/>'
            "</tptz:Velocity>"
            "</tptz:ContinuousMove>"
        )
        action = "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove"
    for path in _ONVIF_PATHS:
        try:
            url = f"{scheme}://{ip}:{port}{path}"
            _http(
                url,
                _soap_body(inner),
                user,
                password,
                f'application/soap+xml; charset=utf-8; action="{action}"',
                method="POST",
            )
            return True
        except Exception:
            continue
    return False


def _isapi_move(scheme: str, ip: str, port: int, user: str, password: str, name: str) -> bool:
    x, y, z = _cmd_to_velocity(name)
    pan = int(x * 60)
    tilt = int(y * 60)
    zoom = int(z * 60)
    continuous = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<PTZData><pan>{pan}</pan><tilt>{tilt}</tilt><zoom>{zoom}</zoom></PTZData>"
    ).encode("utf-8")
    momentary = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<PTZData><pan>{pan}</pan><tilt>{tilt}</tilt><zoom>{zoom}</zoom>"
        "<Momentary><duration>400</duration></Momentary></PTZData>"
    ).encode("utf-8")
    for path in _ISAPI_CONTINUOUS:
        try:
            _http(f"{scheme}://{ip}:{port}{path}", continuous, user, password, "application/xml")
            return True
        except Exception:
            continue
    if name != "ptz_stop":
        for path in _ISAPI_MOMENTARY:
            try:
                _http(f"{scheme}://{ip}:{port}{path}", momentary, user, password, "application/xml")
                return True
            except Exception:
                continue
    return False


def send_ptz(
    ip: str,
    username: str,
    password: str,
    name: str,
    *,
    ports: tuple[int, ...] = _DEFAULT_PORTS,
) -> bool:
    """Send a PTZ command. Returns False if this camera has no local PTZ API."""
    ip = (ip or "").strip()
    user = (username or "admin").strip() or "admin"
    if not ip or name not in {
        "ptz_left",
        "ptz_right",
        "ptz_up",
        "ptz_down",
        "ptz_zoom_in",
        "ptz_zoom_out",
        "ptz_stop",
    }:
        return False
    for pwd in _password_variants(password):
        for scheme, port in _scheme_ports(ports):
            try:
                if _isapi_move(scheme, ip, port, user, pwd, name):
                    return True
            except Exception:
                pass
            try:
                if _onvif_move(scheme, ip, port, user, pwd, name):
                    return True
            except Exception:
                pass
    return False
