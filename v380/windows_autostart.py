"""Windows scheduled tasks: keep Optiview API + recorder running after reboot."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from v380.paths import ROOT

BACKEND_TASK = "OptiviewBackend"
BACKEND_WATCH_TASK = "OptiviewBackendWatch"
# Legacy names from older installs
_LEGACY = ("V380StudioAutoRecord", "V380StudioAutoRecordWatch")


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


def ensure_autostart(enabled: bool = True) -> None:
    """Register ONLOGON + 5-minute watchdog so backend restarts after reboot."""
    if sys.platform != "win32":
        return
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
