"""Windows scheduled tasks: keep Optiview API + recorder running after reboot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from v380.paths import DATA_DIR, ROOT

BACKEND_TASK = "OptiviewBackend"
BACKEND_WATCH_TASK = "OptiviewBackendWatch"
# Legacy names from older installs
_LEGACY = ("V380StudioAutoRecord", "V380StudioAutoRecordWatch")
_PREF_FILE = DATA_DIR / "backend_autostart.json"


def _python_exe() -> str:
    """Prefer pythonw.exe so scheduled/startup launches have no console window."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def _backend_script() -> Path:
    return ROOT / "optiview_backend.py"


def backend_task_command() -> str:
    script = _backend_script()
    return f'"{_python_exe()}" "{script}"'


def _run_schtasks(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)


def autostart_preferred() -> bool:
    """User preference (default off until explicitly enabled in the app)."""
    try:
        if _PREF_FILE.is_file():
            raw = json.loads(_PREF_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "enabled" in raw:
                return bool(raw["enabled"])
    except Exception:
        pass
    return False


def set_autostart_preferred(enabled: bool) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _PREF_FILE.write_text(
            json.dumps({"enabled": bool(enabled)}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def autostart_status() -> dict:
    """What Windows currently has registered + the saved preference."""
    preferred = autostart_preferred()
    if sys.platform != "win32":
        return {
            "ok": True,
            "platform": sys.platform,
            "supported": False,
            "preferred": preferred,
            "enabled": False,
            "task": False,
            "startup_shortcut": False,
        }
    task = _task_exists(BACKEND_TASK) or _task_exists(BACKEND_WATCH_TASK)
    shortcut = _startup_vbs().is_file() or _startup_bat().is_file()
    return {
        "ok": True,
        "platform": "win32",
        "supported": True,
        "preferred": preferred,
        "enabled": bool(task or shortcut),
        "task": task,
        "startup_shortcut": shortcut,
    }


def ensure_autostart(enabled: bool | None = None) -> None:
    """Register or clear ONLOGON + 5-minute watchdog.

    If enabled is None, use the saved preference (default: off).
    """
    if sys.platform != "win32":
        return
    if enabled is None:
        enabled = autostart_preferred()
    set_autostart_preferred(bool(enabled))
    script = _backend_script()
    if not script.is_file():
        return
    try:
        if enabled:
            cmd = backend_task_command()
            # /IT = interactive token (needed for ONLOGON without admin elevation)
            _run_schtasks(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/TN",
                    BACKEND_TASK,
                    "/SC",
                    "ONLOGON",
                    "/RL",
                    "LIMITED",
                    "/IT",
                    "/TR",
                    cmd,
                ]
            )
            # Fallback: at user logon via Startup folder if schtasks rejected ONLOGON
            if not _task_exists(BACKEND_TASK):
                _install_startup_shortcut()
            _run_schtasks(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/TN",
                    BACKEND_WATCH_TASK,
                    "/SC",
                    "MINUTE",
                    "/MO",
                    "5",
                    "/RL",
                    "LIMITED",
                    "/TR",
                    cmd,
                ]
            )
        else:
            for name in (BACKEND_TASK, BACKEND_WATCH_TASK) + _LEGACY:
                _run_schtasks(["schtasks", "/Delete", "/F", "/TN", name])
            _remove_startup_shortcut()
    except Exception:
        pass


def _task_exists(name: str) -> bool:
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.returncode == 0
    except Exception:
        return False


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"


def _startup_bat() -> Path:
    return _startup_dir() / "OptiviewBackend.bat"


def _startup_vbs() -> Path:
    return _startup_dir() / "OptiviewBackend.vbs"


def _install_startup_shortcut() -> None:
    """Hidden logon fallback via VBS (no console flash). Remove legacy .bat if present."""
    try:
        _startup_dir().mkdir(parents=True, exist_ok=True)
        # VBScript: Run """exe"" ""arg""", 0, False  (0 = hidden window)
        py = _python_exe().replace('"', '""')
        script = str(_backend_script()).replace('"', '""')
        _startup_vbs().write_text(
            'Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.Run """{py}"" ""{script}""", 0, False\r\n',
            encoding="utf-8",
        )
        _startup_bat().unlink(missing_ok=True)
    except OSError:
        pass


def _remove_startup_shortcut() -> None:
    try:
        _startup_bat().unlink(missing_ok=True)
        _startup_vbs().unlink(missing_ok=True)
    except OSError:
        pass
