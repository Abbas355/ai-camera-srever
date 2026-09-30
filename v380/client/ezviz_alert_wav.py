"""Play alert.wav on an EZVIZ camera speaker (cloud / Open Platform)."""

from __future__ import annotations

import json
import os
import tempfile
import wave
from pathlib import Path

import requests

from v380.client.extras import ALERT_WAV, _load_alert_pcm
from v380.client.ezviz_session import resolve_serial, silence_all, sound_alarm
from v380.paths import DATA_DIR

OPEN_CREDS = DATA_DIR / "ezviz_open_api.json"


def load_open_creds() -> tuple[str, str] | None:
    key = (os.environ.get("EZVIZ_APP_KEY") or "").strip()
    secret = (os.environ.get("EZVIZ_APP_SECRET") or "").strip()
    if key and secret:
        return key, secret
    if OPEN_CREDS.is_file():
        try:
            data = json.loads(OPEN_CREDS.read_text(encoding="utf-8"))
            key = str(data.get("app_key") or "").strip()
            secret = str(data.get("app_secret") or "").strip()
            if key and secret:
                return key, secret
        except Exception:
            return None
    return None


def save_open_creds(app_key: str, app_secret: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OPEN_CREDS.write_text(
        json.dumps({"app_key": app_key.strip(), "app_secret": app_secret.strip()}, indent=2),
        encoding="utf-8",
    )


def _open_token(app_key: str, app_secret: str) -> tuple[str, str]:
    # International Open API host (works for most non-CN accounts)
    for host in ("https://open.ezvizlife.com", "https://open.ys7.com"):
        try:
            r = requests.post(
                f"{host}/api/lapp/token/get",
                data={"appKey": app_key, "appSecret": app_secret},
                timeout=20,
            )
            data = r.json()
            token = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("accessToken")
            if token:
                return str(token), host
        except Exception:
            continue
    raise RuntimeError("Open Platform token failed — check AppKey / AppSecret")


def _wav_for_upload(src: Path) -> Path:
    """EZVIZ upload prefers short mono 8 kHz WAV."""
    pcm = _load_alert_pcm()
    if not pcm:
        if src.is_file():
            return src
        raise RuntimeError(f"Cannot read {src}")
    # Cap ~8 seconds to keep upload small
    max_bytes = 8000 * 2 * 8
    pcm = pcm[:max_bytes]
    out = Path(tempfile.gettempdir()) / "ezviz_studio_alert_8k.wav"
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(pcm)
    return out


def play_wav_open_platform(serial: str, wav_path: Path | None = None) -> str:
    creds = load_open_creds()
    if not creds:
        raise RuntimeError("no_open_creds")
    app_key, app_secret = creds
    token, host = _open_token(app_key, app_secret)
    path = _wav_for_upload(wav_path or ALERT_WAV)

    with path.open("rb") as fh:
        up = requests.post(
            f"{host}/api/lapp/voice/upload",
            data={"accessToken": token, "deviceSerial": serial, "voiceName": "StudioAlert"},
            files={"file": ("alert.wav", fh, "audio/wav")},
            timeout=60,
        )
    up_json = up.json()
    if str((up_json.get("code") if isinstance(up_json, dict) else "") or "") not in ("200", "0"):
        # some regions nest under meta
        meta = (up_json.get("meta") if isinstance(up_json, dict) else None) or {}
        if str(meta.get("code") or "") != "200":
            raise RuntimeError(f"Voice upload failed: {up_json}")
    file_url = ((up_json.get("data") or {}) if isinstance(up_json, dict) else {}).get("fileUrl") or (
        (up_json.get("data") or {}) if isinstance(up_json, dict) else {}
    ).get("url")
    if not file_url:
        raise RuntimeError(f"Upload OK but no fileUrl: {up_json}")

    send = requests.post(
        f"{host}/api/lapp/voice/send",
        data={
            "accessToken": token,
            "deviceSerial": serial,
            "fileUrl": file_url,
            "channelNo": "1",
        },
        timeout=30,
    )
    send_json = send.json()
    code = str(send_json.get("code") or ((send_json.get("meta") or {}).get("code")) or "")
    if code not in ("200", "0"):
        raise RuntimeError(f"Voice send failed: {send_json}")
    return "Playing alert.wav on camera speaker (Open Platform)"


def play_alert_on_camera(ip: str, wav_path: Path | None = None) -> str:
    """
    Play audio/alert.wav from the CAMERA speaker.
    Prefer Open Platform custom voice broadcast; otherwise use built-in siren
    (EZVIZ does not allow RTSP push of arbitrary WAV on H6C).
    """
    serial = resolve_serial(ip)
    path = wav_path or ALERT_WAV
    if not path.is_file():
        raise RuntimeError(f"Missing {path}")

    try:
        return play_wav_open_platform(serial, path)
    except RuntimeError as exc:
        if "no_open_creds" not in str(exc) and "Open Platform token" not in str(exc):
            # upload/send failed — fall through to siren with note
            pass
        elif "no_open_creds" in str(exc):
            pass
        else:
            # token failed — still try siren so sound comes from camera
            pass

    # Fallback: built-in camera alarm (real speaker, not custom WAV)
    sound_alarm(ip, True)
    return (
        "Camera siren ON (built-in). "
        "This H6C cannot play a custom WAV over RTSP. "
        "To play audio/alert.wav on the camera: add EZVIZ Open Platform "
        "AppKey/AppSecret in data/ezviz_open_api.json, or upload the file "
        "in the EZVIZ phone app as a custom alarm voice."
    )


def stop_alert_on_camera(ip: str) -> str:
    try:
        return silence_all(ip)
    except Exception:
        try:
            sound_alarm(ip, False)
        except Exception:
            pass
        return "Stop sent"
