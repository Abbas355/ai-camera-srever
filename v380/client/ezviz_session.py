"""EZVIZ cloud session — same API the phone app uses for PTZ, light, siren."""

from __future__ import annotations

import contextvars
import json
import threading
from contextlib import contextmanager
from pathlib import Path

from v380.paths import DATA_DIR
from v380.store.secretbox import decrypt, encrypt, load_or_create_key

TOKEN_FILE = DATA_DIR / "ezviz_token.json"
CREDS_FILE = DATA_DIR / "ezviz_account.bin"
KEY_PATH = DATA_DIR / "master.key"

_lock = threading.Lock()
_client = None
_clients: dict[str, object] = {}
_scope: contextvars.ContextVar[str] = contextvars.ContextVar("ezviz_account", default="")
_serial_by_ip: dict[str, str] = {}


def _key() -> bytes:
    return load_or_create_key(KEY_PATH)


def save_account(account: str, password: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    blob = encrypt(_key(), json.dumps({"account": account, "password": password}))
    CREDS_FILE.write_bytes(blob)


def load_account() -> tuple[str, str] | None:
    if not CREDS_FILE.is_file():
        return None
    try:
        data = json.loads(decrypt(_key(), CREDS_FILE.read_bytes()))
        acc = str(data.get("account") or "").strip()
        pw = str(data.get("password") or "")
        if acc and pw:
            return acc, pw
    except Exception:
        return None
    return None


def _save_token(token: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    slim = {
        "session_id": token.get("session_id"),
        "rf_session_id": token.get("rf_session_id"),
        "username": token.get("username"),
        "api_url": token.get("api_url") or "apiisgp.ezvizlife.com",
    }
    TOKEN_FILE.write_text(json.dumps(slim), encoding="utf-8")


def _load_token() -> dict | None:
    if not TOKEN_FILE.is_file():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        if data.get("session_id") and data.get("rf_session_id"):
            return data
    except Exception:
        return None
    return None


def login(account: str, password: str, sms_code: str | None = None) -> dict:
    from pyezviz import EzvizClient

    global _client
    acc = (account or "").strip()
    if not acc or not password:
        raise RuntimeError("EZVIZ account email/phone and password are required")
    sms = int(sms_code) if str(sms_code or "").strip().isdigit() else None
    # Prefer last-known region (e.g. apiisgp); EU is only a bootstrap host.
    prev = _load_token() or {}
    api = str(prev.get("api_url") or "apiisgp.ezvizlife.com")
    client = EzvizClient(acc, password, url=api, timeout=20)
    token = client.login(sms)
    save_account(acc, password)
    _save_token(token)
    with _lock:
        _clients[acc.lower()] = client
        _client = client
        _serial_by_ip.clear()
    return token


def _open_saved(username: str, password: str, api_url: str, token_json: str):
    from pyezviz import EzvizClient

    token = None
    if token_json:
        try:
            parsed = json.loads(token_json)
            if isinstance(parsed, dict) and parsed.get("session_id"):
                token = parsed
        except Exception:
            token = None
    api = (api_url or (token or {}).get("api_url") or "apiisgp.ezvizlife.com").strip()
    if token:
        try:
            client = EzvizClient(username, password, url=api, timeout=20, token=token)
            client.login()
            return client
        except Exception:
            pass
    client = EzvizClient(username, password, url=api, timeout=20)
    client.login()
    return client


def _client_for(username: str):
    key = (username or "").strip().lower()
    if not key:
        raise RuntimeError("Sign in with the EZVIZ app account for this camera")
    with _lock:
        cached = _clients.get(key)
    if cached is not None:
        return cached
    from v380.store.ezviz_accounts import EzvizAccountStore

    rec = None
    for row in EzvizAccountStore().list_full():
        if str(row.get("username") or "").strip().lower() == key:
            rec = row
            break
    if rec is None:
        raise RuntimeError("Sign in with the EZVIZ app account for this camera")
    client = _open_saved(
        str(rec["username"]),
        str(rec["password"]),
        str(rec.get("api_url") or ""),
        str(rec.get("token_json") or ""),
    )
    token = getattr(client, "_token", None)
    if isinstance(token, dict):
        try:
            EzvizAccountStore().upsert(
                str(rec["username"]),
                str(rec["password"]),
                token_json=json.dumps(token),
                api_url=str(token.get("api_url") or rec.get("api_url") or ""),
            )
        except Exception:
            pass
    with _lock:
        _clients[key] = client
    return client


@contextmanager
def using_account(username: str):
    """Use this EZVIZ account for cloud calls on the current request only."""
    handle = _scope.set((username or "").strip())
    try:
        yield
    finally:
        _scope.reset(handle)


def get_client():
    global _client
    scoped = _scope.get().strip()
    if scoped:
        return _client_for(scoped)
    with _lock:
        if _client is not None:
            return _client
    token = _load_token()
    acc = load_account()
    from pyezviz import EzvizClient

    if token:
        try:
            client = EzvizClient(
                acc[0] if acc else None,
                acc[1] if acc else None,
                url=str(token.get("api_url") or "apiisgp.ezvizlife.com"),
                timeout=20,
                token=token,
            )
            client.login()
            _save_token(client._token)
            with _lock:
                _client = client
            return client
        except Exception:
            _client = None
    if acc:
        api = str((token or {}).get("api_url") or "apiisgp.ezvizlife.com")
        client = EzvizClient(acc[0], acc[1], url=api, timeout=20)
        token = client.login()
        _save_token(token)
        with _lock:
            _client = client
        return client
    raise RuntimeError("Sign in with your EZVIZ app email and password to use PTZ / light / alert")


def has_session() -> bool:
    return _load_token() is not None or load_account() is not None


def _norm_mac(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch in "0123456789abcdef")


def _is_ipv4(value: str) -> bool:
    parts = (value or "").strip().split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if not all(0 <= n <= 255 for n in nums):
        return False
    a, _b, _c, d = nums
    # Drop netmasks / broadcast / loopback — not camera addresses.
    if a in (0, 127, 255) or d in (0, 255):
        return False
    return True


def _is_private_ip(value: str) -> bool:
    if not _is_ipv4(value):
        return False
    a, b, _c, _d = [int(p) for p in value.split(".")]
    return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    elif obj is not None:
        yield str(obj)


def _device_ips(dev: dict) -> set[str]:
    ips: set[str] = set()
    for raw in _walk(dev):
        text = raw.strip()
        if _is_ipv4(text):
            ips.add(text)
            continue
        # "ip: 192.168.1.10" or "rtsp://admin:xx@192.168.1.10:554/..."
        for token in text.replace("/", " ").replace("@", " ").replace(":", " ").split():
            if _is_ipv4(token):
                ips.add(token)
    return ips


def _device_mac(dev: dict) -> str:
    best = ""
    for raw in _walk(dev):
        mac = _norm_mac(raw)
        if len(mac) == 12:
            best = mac
            break
    return best


def _device_name(dev: dict) -> str:
    info = dev.get("deviceInfos") if isinstance(dev.get("deviceInfos"), dict) else {}
    if not info:
        info = dev.get("DEVICE_INFO") if isinstance(dev.get("DEVICE_INFO"), dict) else {}
    return str(info.get("name") or info.get("deviceName") or dev.get("name") or "").strip()


def _looks_like_serial(value: str) -> bool:
    text = (value or "").strip()
    if len(text) < 8 or len(text) > 16:
        return False
    if text.lower().startswith("ezviz_"):
        return False
    return text.replace("-", "").isalnum()


def resolve_serial(ip: str, serial: str = "", mac: str = "", name: str = "") -> str:
    """Map the *open* camera to its EZVIZ serial. Never fall back to another camera."""
    ip = (ip or "").strip()
    serial_hint = (serial or "").strip()
    if serial_hint.lower().startswith("ezviz_"):
        serial_hint = ""
    name_hint = (name or "").strip().lower()
    mac_hint = _norm_mac(mac)

    def _remember(found: str) -> str:
        if ip:
            _serial_by_ip[f"{_scope.get().strip().lower()}|{ip}"] = found
        return found

    # A saved serial is this camera. Listing every device on the account
    # adds a cloud round trip to every button press.
    if serial_hint and _looks_like_serial(serial_hint):
        return _remember(serial_hint)
    cached = _serial_by_ip.get(f"{_scope.get().strip().lower()}|{ip}", "") if ip else ""
    if cached and _looks_like_serial(cached):
        return cached

    client = get_client()
    devices = client.get_device_infos()
    if not isinstance(devices, dict) or not devices:
        try:
            devices = client.get_device_infos(force_api_call=True)  # type: ignore[call-arg]
        except TypeError:
            devices = client.get_device_infos()
    if not isinstance(devices, dict) or not devices:
        raise RuntimeError("Could not list EZVIZ cameras on this account")

    keys = {str(k): str(k) for k in devices}
    keys_l = {str(k).lower(): str(k) for k in devices}

    if serial_hint and serial_hint in keys:
        return _remember(keys[serial_hint])
    if serial_hint and serial_hint.lower() in keys_l:
        return _remember(keys_l[serial_hint.lower()])
    if ip and ip in keys:
        return _remember(keys[ip])

    ip_hits: list[str] = []
    octet_hits: list[str] = []
    mac_hits: list[str] = []
    name_hits: list[str] = []
    name_part_hits: list[str] = []
    last = ip.rsplit(".", 1)[-1] if _is_ipv4(ip) else ""
    for ser, dev in devices.items():
        if not isinstance(dev, dict):
            continue
        ser_s = str(ser)
        dips = _device_ips(dev)
        if ip and ip in dips:
            ip_hits.append(ser_s)
        if last and any(_is_private_ip(x) and x.rsplit(".", 1)[-1] == last for x in dips):
            octet_hits.append(ser_s)
        if mac_hint and _device_mac(dev) == mac_hint:
            mac_hits.append(ser_s)
        dname = _device_name(dev).lower()
        if name_hint and dname and dname == name_hint:
            name_hits.append(ser_s)
        elif name_hint and dname and (name_hint in dname or dname in name_hint) and min(len(name_hint), len(dname)) >= 3:
            name_part_hits.append(ser_s)

    for hits in (ip_hits, mac_hits, name_hits, octet_hits, name_part_hits):
        uniq = list(dict.fromkeys(hits))
        if len(uniq) == 1:
            return _remember(uniq[0])

    cached = _serial_by_ip.get(f"{_scope.get().strip().lower()}|{ip}", "") if ip else ""
    if cached and cached in keys:
        return cached

    raise RuntimeError(
        "Could not match this open camera on the EZVIZ account — "
        "will not control a different camera"
    )


def match_open_camera(ip: str = "", serial: str = "", mac: str = "", name: str = "") -> str | None:
    """Return serial if this camera is on the *current* cloud session, else None."""
    try:
        return resolve_serial(ip, serial=serial, mac=mac, name=name)
    except Exception:
        return None


_PTZ_ALIASES = {
    "UP": "UP",
    "DOWN": "DOWN",
    "LEFT": "LEFT",
    "RIGHT": "RIGHT",
    "ZOOMIN": "ZOOMIN",
    "ZOOM_IN": "ZOOMIN",
    "ZOOM-IN": "ZOOMIN",
    "ZOOMOUT": "ZOOMOUT",
    "ZOOM_OUT": "ZOOMOUT",
    "ZOOM-OUT": "ZOOMOUT",
}


def ptz_direction(name: str) -> str | None:
    """Map app command (ptz_up / ptz_zoom_in) to EZVIZ cloud direction."""
    raw = (name or "").strip().upper().replace("PTZ_", "")
    return _PTZ_ALIASES.get(raw)


def ptz(ip: str, direction: str, action: str, speed: int = 5, serial: str = "", mac: str = "", name: str = "") -> bool:
    serial = resolve_serial(ip, serial=serial, mac=mac, name=name)
    client = get_client()
    cmd = ptz_direction(direction) or str(direction).upper()
    client.ptz_control(cmd, serial, action.upper(), speed)
    return True


def _friendly_api_error(exc: BaseException) -> str:
    text = str(exc)
    if "2009" in text:
        return "Camera busy / too many commands — wait 2s and try again"
    if "2003" in text:
        return "Camera offline on EZVIZ cloud — open the phone app, wait until it shows Online, then retry"
    if "2004" in text or "DEVICE_EXCEPTION" in text:
        return "Camera rejected command (asleep, offline, or feature unsupported) — try Sleep OFF first"
    if "1100" in text or "Region" in text:
        return "EZVIZ region mismatch — sign in again"
    # strip non-ascii for Windows consoles / UI
    return text.encode("ascii", "replace").decode("ascii")[:180]


def set_switch(ip: str, switch_type: int, enable: bool) -> bool:
    serial = resolve_serial(ip)
    client = get_client()
    try:
        client.switch_status(serial, int(switch_type), 1 if enable else 0)
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return True


def set_status_led(ip: str, enable: bool) -> bool:
    """H6C status LED is controlled via IndicatorLight config (switch type 3 is flaky)."""
    serial = resolve_serial(ip)
    client = get_client()
    val = f'{{"enable":{1 if enable else 0},"mode":1}}'
    try:
        client.set_device_config_by_key(serial, val, "IndicatorLight")
    except Exception:
        # fallback to classic LIGHT switch
        set_switch(ip, 3, enable)  # DeviceSwitchType.LIGHT
        return True
    try:
        set_switch(ip, 3, enable)
    except Exception:
        pass
    return True


def set_night_vision(ip: str, mode: int) -> bool:
    serial = resolve_serial(ip)
    client = get_client()
    try:
        client.set_night_vision_mode(serial, int(mode))
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return True


def sound_alarm(ip: str, enable: bool) -> bool:
    """Active defense siren. EZVIZ API: enable=2 start, enable=1 stop (NOT 1/0)."""
    serial = resolve_serial(ip)
    client = get_client()
    code = 2 if enable else 1
    try:
        client.sound_alarm(serial, code)
    except Exception as exc:
        # Some firmwares also want strobe with siren
        if enable:
            try:
                from pyezviz.constants import DeviceSwitchType

                client.switch_status(serial, DeviceSwitchType.LIGHT_FLICKER.value, 1)
                client.sound_alarm(serial, 2)
                return True
            except Exception as exc2:
                raise RuntimeError(_friendly_api_error(exc2)) from exc2
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return True


def enable_mic(ip: str, enable: bool) -> bool:
    from pyezviz.constants import DeviceSwitchType

    return set_switch(ip, DeviceSwitchType.SOUND.value, enable)


def set_defence(ip: str, enable: bool) -> bool:
    """Arm/disarm. Avoid pyezviz set_camera_defence — its 504 retry passes
    max_retries as channel_no and can spin forever."""
    serial = resolve_serial(ip)
    client = get_client()
    url = (
        f"https://{client._token['api_url']}/v3/devices/"
        f"{serial}/0/changeDefenceStatusReq"
    )
    try:
        req = client._session.put(
            url,
            timeout=getattr(client, "_timeout", 20),
            data={"type": "Global", "status": 1 if enable else 0, "actor": "V"},
        )
        req.raise_for_status()
        data = req.json()
        code = (data.get("meta") or {}).get("code")
        if code == 200:
            return True
        if code == 504:
            # CAS fallback used by pyezviz for stubborn devices
            client.set_camera_defence_old(serial, 1 if enable else 0)
            return True
        raise RuntimeError(_friendly_api_error(RuntimeError(str(data))))
    except Exception as exc:
        try:
            client.set_camera_defence_old(serial, 1 if enable else 0)
            return True
        except Exception as exc2:
            raise RuntimeError(_friendly_api_error(exc2)) from exc


def set_do_not_disturb(ip: str, enable: bool) -> bool:
    serial = resolve_serial(ip)
    client = get_client()
    # pyezviz concatenates channelno into URL — must be str, not int
    try:
        client.do_not_disturb(serial, 1 if enable else 0, channelno="0")
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return True


def set_alarm_sound(ip: str, mode: int) -> bool:
    """0=soft, 1=intense, 2=silent."""
    serial = resolve_serial(ip)
    client = get_client()
    try:
        client.alarm_sound(serial, int(mode), 1)
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return True


def set_detection_sensibility(ip: str, level: int) -> bool:
    """1–6 typical; type 0 = motion."""
    serial = resolve_serial(ip)
    client = get_client()
    try:
        client.detection_sensibility(serial, int(level), 0)
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return True


def set_tracking(ip: str, enable: bool) -> bool:
    """Try the tracking switch types this H6C exposes."""
    from pyezviz.constants import DeviceSwitchType

    errors: list[str] = []
    for kind in (
        DeviceSwitchType.MOBILE_TRACKING,
        DeviceSwitchType.TRACKING,
        DeviceSwitchType.FEATURE_TRACKING,
    ):
        try:
            set_switch(ip, kind.value, enable)
            return True
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(errors[-1] if errors else "Tracking not supported")


def get_feature_states(ip: str, serial: str = "", mac: str = "", name: str = "") -> dict:
    """Read ON/OFF (and modes) from cloud for the Studio toggle panel."""
    from pyezviz.constants import DeviceSwitchType

    serial = resolve_serial(ip, serial=serial, mac=mac, name=name)
    client = get_client()
    try:
        devices = client.get_device_infos(force_update=True)  # type: ignore[call-arg]
    except TypeError:
        devices = client.get_device_infos()
    if not isinstance(devices, dict) or serial not in devices:
        devices = client.get_device_infos()
    dev = devices.get(serial) if isinstance(devices, dict) else None
    if not isinstance(dev, dict):
        raise RuntimeError("Camera not found on EZVIZ account")

    switches: dict[int, bool] = {}
    for item in dev.get("SWITCH") or []:
        if isinstance(item, dict) and "type" in item:
            switches[int(item["type"])] = bool(item.get("enable"))

    status = dev.get("STATUS") if isinstance(dev.get("STATUS"), dict) else {}
    opt = status.get("optionals") if isinstance(status.get("optionals"), dict) else {}
    info = dev.get("deviceInfos") if isinstance(dev.get("deviceInfos"), dict) else {}
    led_opt = opt.get("IndicatorLight") if isinstance(opt.get("IndicatorLight"), dict) else {}
    nv = opt.get("NightVision_Model") if isinstance(opt.get("NightVision_Model"), dict) else {}
    vol = opt.get("CustomVoice_Volume") if isinstance(opt.get("CustomVoice_Volume"), dict) else {}
    icr = opt.get("device_ICR_DSS") if isinstance(opt.get("device_ICR_DSS"), dict) else {}

    def sw(kind: DeviceSwitchType, default: bool = False) -> bool:
        return bool(switches.get(kind.value, default))

    online_raw = info.get("status", status.get("globalStatus"))
    online = online_raw in (1, "1", True)

    try:
        speaker_vol = int(vol.get("volume", 80))
    except (TypeError, ValueError):
        speaker_vol = 80
    try:
        mic_vol = int(vol.get("microphone_volume", 50))
    except (TypeError, ValueError):
        mic_vol = 50
    speaker_vol = max(0, min(100, speaker_vol))
    mic_vol = max(0, min(100, mic_vol))

    # IR: day mode (2) = off; night mode (1) = on; auto (0) falls back to switch 10
    try:
        icr_mode = int(icr.get("mode", 0))
    except (TypeError, ValueError):
        icr_mode = 0
    if icr_mode == 2:
        ir_on = False
    elif icr_mode == 1:
        ir_on = True
    else:
        ir_on = sw(DeviceSwitchType.INFRARED_LIGHT, True)

    return {
        "online": online,
        "serial": serial,
        "privacy": sw(DeviceSwitchType.PRIVACY),
        "sleep": sw(DeviceSwitchType.SLEEP),
        "track": sw(DeviceSwitchType.MOBILE_TRACKING) or sw(DeviceSwitchType.TRACKING),
        "cruise": sw(DeviceSwitchType.CRUISE_TRACKING),
        "led": bool(led_opt.get("enable")) or sw(DeviceSwitchType.LIGHT),
        "ir": ir_on,
        "ir_mode": icr_mode,  # 0 auto, 1 night, 2 day
        "flood": sw(DeviceSwitchType.ALARM_LIGHT),
        "flicker": sw(DeviceSwitchType.LIGHT_FLICKER),
        "mic": sw(DeviceSwitchType.SOUND),
        "logo": sw(DeviceSwitchType.LOGO),
        "human": sw(DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION),
        "allday": sw(DeviceSwitchType.ALL_DAY_VIDEO),
        "alarm_tone": sw(DeviceSwitchType.ALARM_TONE),
        "feature_track": sw(DeviceSwitchType.FEATURE_TRACKING),
        "night_vision": int(nv.get("graphicType", 2) or 2),
        "alarm_sound": int(status.get("alarmSoundMode", 0) or 0),
        "speaker_volume": speaker_vol,
        "mic_volume": mic_vol,
        "siren": False,
    }


def set_audio_volumes(ip: str, speaker: int | None = None, microphone: int | None = None) -> str:
    """Set camera speaker (alarm/talk) and microphone levels 0–100."""
    st = get_feature_states(ip)
    sp = int(st.get("speaker_volume", 80) if speaker is None else speaker)
    mic = int(st.get("mic_volume", 50) if microphone is None else microphone)
    sp = max(0, min(100, sp))
    mic = max(0, min(100, mic))
    serial = resolve_serial(ip)
    client = get_client()
    val = f'{{"volume":{sp},"microphone_volume":{mic}}}'
    try:
        client.set_device_config_by_key(serial, val, "CustomVoice_Volume")
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
    return f"Volumes set — speaker {sp}% · mic {mic}%"


def silence_all(ip: str) -> str:
    """Stop siren / flicker / flood / alarm tone — emergency quiet."""
    from pyezviz.constants import DeviceSwitchType

    errors: list[str] = []
    try:
        sound_alarm(ip, False)
    except Exception as exc:
        errors.append(f"siren:{_friendly_api_error(exc)}")
    for kind, label in (
        (DeviceSwitchType.LIGHT_FLICKER, "flicker"),
        (DeviceSwitchType.ALARM_LIGHT, "flood"),
        (DeviceSwitchType.ALARM_TONE, "alarm_tone"),
    ):
        try:
            set_switch(ip, kind.value, False)
        except Exception as exc:
            errors.append(f"{label}:{_friendly_api_error(exc)}")
    try:
        set_alarm_sound(ip, 2)
    except Exception as exc:
        errors.append(f"sound:{_friendly_api_error(exc)}")
    if errors and all("offline" in e.lower() or "2003" in e for e in errors):
        raise RuntimeError(
            "Camera offline on EZVIZ cloud — plug it in, wait until Online in the phone app, then press Silence again"
        )
    if errors:
        return "Silence sent (some steps failed: " + "; ".join(errors[:2]) + ")"
    return "All alarms silenced (siren / flicker / flood / tones)"


def camera_status(ip: str) -> dict:
    from pyezviz.camera import EzvizCamera

    serial = resolve_serial(ip)
    client = get_client()
    cam = EzvizCamera(client, serial)
    return cam.status()


def run_action(ip: str, name: str, serial: str = "", mac: str = "", name_hint: str = "") -> str:
    """Run a named Studio control. Returns a short status label."""
    from pyezviz.constants import DeviceSwitchType, NightVisionMode

    ip = (ip or "").strip()
    if not ip:
        raise RuntimeError("Camera IP missing")
    # Bind this IP to the open camera before nested helpers call resolve_serial(ip).
    resolve_serial(ip, serial=serial, mac=mac, name=name_hint)

    def sw(kind: DeviceSwitchType, on: bool, label: str) -> str:
        set_switch(ip, kind.value, on)
        return label

    def light(on: bool) -> str:
        # Flood / white light + status LED attempts
        set_status_led(ip, on)
        try:
            set_switch(ip, DeviceSwitchType.ALARM_LIGHT.value, on)
        except Exception:
            pass
        return "Light ON" if on else "Light OFF"

    def led(on: bool) -> str:
        set_status_led(ip, on)
        return "Status LED ON" if on else "Status LED OFF"

    def track(on: bool) -> str:
        set_tracking(ip, on)
        return "Smart tracking ON" if on else "Smart tracking OFF"

    def ir(on: bool) -> str:
        """H6C IR LEDs follow day/night mode (device_ICR_DSS), not only switch 10.

        mode 0 = auto, 1 = force night (IR on), 2 = force day (IR off).
        """
        serial = resolve_serial(ip)
        client = get_client()
        # Force day/night so LEDs actually change even in a bright room.
        mode = 1 if on else 2
        val = (
            f'{{"mode":{mode},"sensitivity":2,'
            f'"autoTimeBegin":"00:00","autoTimeEnd":"00:00"}}'
        )
        try:
            client.set_device_config_by_key(serial, val, "device_ICR_DSS")
        except Exception as exc:
            raise RuntimeError(_friendly_api_error(exc)) from exc
        try:
            set_switch(ip, DeviceSwitchType.INFRARED_LIGHT.value, on)
        except Exception:
            pass
        if on:
            try:
                set_night_vision(ip, NightVisionMode.NIGHT_VISION_B_W.value)
            except Exception:
                pass
            return "IR ON — forced night / B&W (LEDs should light; pink on phone camera)"
        try:
            set_night_vision(ip, NightVisionMode.NIGHT_VISION_COLOUR.value)
        except Exception:
            pass
        return "IR OFF — forced day mode (IR LEDs off)"

    def ir_auto() -> str:
        serial = resolve_serial(ip)
        client = get_client()
        val = '{"mode":0,"sensitivity":2,"autoTimeBegin":"00:00","autoTimeEnd":"00:00"}'
        try:
            client.set_device_config_by_key(serial, val, "device_ICR_DSS")
        except Exception as exc:
            raise RuntimeError(_friendly_api_error(exc)) from exc
        try:
            set_switch(ip, DeviceSwitchType.INFRARED_LIGHT.value, True)
            set_night_vision(ip, NightVisionMode.NIGHT_VISION_SMART.value)
        except Exception:
            pass
        return "IR AUTO — camera chooses day/night"

    def flicker(on: bool) -> str:
        # H6C strobe needs the flood/alarm light armed with the flicker switch.
        if on:
            try:
                set_switch(ip, DeviceSwitchType.ALARM_LIGHT.value, True)
            except Exception:
                pass
            set_switch(ip, DeviceSwitchType.LIGHT_FLICKER.value, True)
            return "Strobe ON — flood light + flicker"
        set_switch(ip, DeviceSwitchType.LIGHT_FLICKER.value, False)
        try:
            set_switch(ip, DeviceSwitchType.ALARM_LIGHT.value, False)
        except Exception:
            pass
        return "Strobe OFF"

    table = {
        "privacy_on": lambda: sw(DeviceSwitchType.PRIVACY, True, "Privacy ON — lens covered"),
        "privacy_off": lambda: sw(DeviceSwitchType.PRIVACY, False, "Privacy OFF"),
        "sleep_on": lambda: sw(DeviceSwitchType.SLEEP, True, "Sleep ON"),
        "sleep_off": lambda: sw(DeviceSwitchType.SLEEP, False, "Sleep OFF"),
        "track_on": lambda: track(True),
        "track_off": lambda: track(False),
        "cruise_on": lambda: sw(DeviceSwitchType.CRUISE_TRACKING, True, "Cruise ON"),
        "cruise_off": lambda: sw(DeviceSwitchType.CRUISE_TRACKING, False, "Cruise OFF"),
        "led_on": lambda: led(True),
        "led_off": lambda: led(False),
        "ir_on": lambda: ir(True),
        "ir_off": lambda: ir(False),
        "ir_auto": lambda: ir_auto(),
        "mic_on": lambda: sw(DeviceSwitchType.SOUND, True, "Camera mic ON"),
        "mic_off": lambda: sw(DeviceSwitchType.SOUND, False, "Camera mic OFF"),
        "logo_on": lambda: sw(DeviceSwitchType.LOGO, True, "OSD logo ON"),
        "logo_off": lambda: sw(DeviceSwitchType.LOGO, False, "OSD logo OFF"),
        "flood_on": lambda: sw(DeviceSwitchType.ALARM_LIGHT, True, "Flood / alarm light ON"),
        "flood_off": lambda: sw(DeviceSwitchType.ALARM_LIGHT, False, "Flood / alarm light OFF"),
        "flicker_on": lambda: flicker(True),
        "flicker_off": lambda: flicker(False),
        "human_on": lambda: sw(DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION, True, "Human detection ON"),
        "human_off": lambda: sw(DeviceSwitchType.HUMAN_INTELLIGENT_DETECTION, False, "Human detection OFF"),
        "allday_on": lambda: sw(DeviceSwitchType.ALL_DAY_VIDEO, True, "All-day recording ON"),
        "allday_off": lambda: sw(DeviceSwitchType.ALL_DAY_VIDEO, False, "All-day recording OFF"),
        "alarm_tone_on": lambda: sw(DeviceSwitchType.ALARM_TONE, True, "Alarm tone ON"),
        "alarm_tone_off": lambda: sw(DeviceSwitchType.ALARM_TONE, False, "Alarm tone OFF"),
        "cruise_track_on": lambda: sw(DeviceSwitchType.CRUISE_TRACKING, True, "Cruise tracking ON"),
        "cruise_track_off": lambda: sw(DeviceSwitchType.CRUISE_TRACKING, False, "Cruise tracking OFF"),
        "feature_track_on": lambda: sw(DeviceSwitchType.FEATURE_TRACKING, True, "Feature tracking ON"),
        "feature_track_off": lambda: sw(DeviceSwitchType.FEATURE_TRACKING, False, "Feature tracking OFF"),
        "arm": lambda: (set_defence(ip, True) or True) and "Armed — motion alerts ON",
        "disarm": lambda: (set_defence(ip, False) or True) and "Disarmed — motion alerts OFF",
        "dnd_on": lambda: (set_do_not_disturb(ip, True) or True) and "Do not disturb ON",
        "dnd_off": lambda: (set_do_not_disturb(ip, False) or True) and "Do not disturb OFF",
        "siren_on": lambda: (sound_alarm(ip, True) or True) and "Siren ON",
        "siren_off": lambda: (sound_alarm(ip, False) or True) and "Siren OFF",
        "sound_soft": lambda: (set_alarm_sound(ip, 0) or True) and "Alarm sound: soft",
        "sound_loud": lambda: (set_alarm_sound(ip, 1) or True) and "Alarm sound: intense",
        "sound_mute": lambda: (set_alarm_sound(ip, 2) or True) and "Alarm sound: silent",
        "sens_low": lambda: (set_detection_sensibility(ip, 1) or True) and "Detection: low",
        "sens_mid": lambda: (set_detection_sensibility(ip, 3) or True) and "Detection: medium",
        "sens_high": lambda: (set_detection_sensibility(ip, 6) or True) and "Detection: high",
        "nv_color": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_COLOUR.value) or True)
        and "Night vision: colour",
        "nv_bw": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_B_W.value) or True)
        and "Night vision: B&W",
        "nv_smart": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_SMART.value) or True)
        and "Night vision: smart",
        # aliases used by existing V380-style buttons
        "light_on": lambda: light(True),
        "light_off": lambda: light(False),
        "light_auto": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_SMART.value) or True)
        and "Night vision: smart",
        "image_color": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_COLOUR.value) or True)
        and "Night vision: colour",
        "image_bw": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_B_W.value) or True)
        and "Night vision: B&W",
        "image_auto": lambda: (set_night_vision(ip, NightVisionMode.NIGHT_VISION_SMART.value) or True)
        and "Night vision: smart",
        "image_flip": lambda: sw(DeviceSwitchType.INFRARED_LIGHT, True, "IR ON"),
        "alert_on": lambda: (sound_alarm(ip, True) or True) and "Siren / alert ON",
        "alert_off": lambda: (sound_alarm(ip, False) or True) and "Siren / alert OFF",
        "silence_all": lambda: silence_all(ip),
    }
    fn = table.get(name)
    if not fn:
        raise RuntimeError(f"Unknown EZVIZ action: {name}")
    try:
        return str(fn())
    except Exception as exc:
        raise RuntimeError(_friendly_api_error(exc)) from exc
