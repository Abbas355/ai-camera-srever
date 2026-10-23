"""Process entry: HTTP API + record worker (Flutter is the client UI)."""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path


def _api_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _who_listens(port: int) -> str:
    """Best-effort Windows pid/name for whatever owns the port."""
    if sys.platform != "win32":
        return ""
    try:
        import subprocess

        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        needle = f":{port} "
        for line in out.splitlines():
            if "LISTENING" not in line.upper():
                continue
            if needle not in line and f":{port}\t" not in line:
                continue
            parts = line.split()
            if not parts:
                continue
            pid = parts[-1]
            name = ""
            try:
                name = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    text=True,
                    errors="ignore",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).strip().split(",")[0].strip('"')
            except Exception:
                pass
            return f"pid={pid}" + (f" ({name})" if name else "")
    except Exception:
        return ""
    return ""


class _Tee:
    """Write the same text to several streams (console + log file)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self) -> None:
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


def _write_status(data_dir: Path, **fields) -> None:
    """Small JSON so Flutter / humans can see why the backend exited or stuck."""
    try:
        path = data_dir / "backend_status.json"
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            **fields,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _setup_logs() -> Path:
    """Always keep a server.log; show the same lines in the console when present."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    from v380.paths import DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "server.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n===== Optiview backend start {stamp} pid={os.getpid()} =====\n")
    log_file.flush()

    # pythonw has no console — only the log file.
    if sys.stdout is None and sys.stderr is None:
        sys.stdout = log_file
        sys.stderr = log_file
    else:
        out = sys.stdout
        err = sys.stderr
        if out is None:
            out = log_file
        if err is None:
            err = log_file
        sys.stdout = _Tee(out, log_file)
        sys.stderr = _Tee(err, log_file)

    return log_path


def main() -> None:
    log_path = _setup_logs()
    from v380.paths import DATA_DIR

    port = int(os.environ.get("V380_PORT") or 8080)

    print(f"[optiview] log file: {log_path}")
    print(f"[optiview] status file: {DATA_DIR / 'backend_status.json'}")
    print(f"[optiview] port: {port}")
    _write_status(DATA_DIR, state="starting", port=port, log=str(log_path))

    try:
        from v380.record.auto_record import ensure_worker

        ensure_worker()
    except Exception as exc:
        print(f"[optiview] record worker: {exc}", file=sys.stderr)

    if _api_up(port):
        who = _who_listens(port)
        msg = (
            f"API already running on port {port}"
            + (f" — {who}" if who else "")
            + ". Not starting a second copy."
        )
        print(f"[optiview] {msg}")
        print("[optiview] Use the Flutter app against that server.")
        print(
            "[optiview] To restart: stop OptiviewBackend / pythonw / seeme backend "
            "holding the port, then run again."
        )
        _write_status(
            DATA_DIR,
            state="already_running",
            port=port,
            log=str(log_path),
            detail=msg,
            owner=who or None,
        )
        return

    print("[optiview] starting HTTP API…")
    _write_status(DATA_DIR, state="listening", port=port, log=str(log_path))
    try:
        from v380.api.http.server import main as studio_main

        studio_main()
    except OSError as exc:
        print(f"[optiview] HTTP bind failed: {exc}", file=sys.stderr)
        _write_status(
            DATA_DIR,
            state="bind_failed",
            port=port,
            log=str(log_path),
            error=str(exc),
        )
        raise
    except Exception as exc:
        print(f"[optiview] HTTP server crashed: {exc}", file=sys.stderr)
        _write_status(
            DATA_DIR,
            state="crashed",
            port=port,
            log=str(log_path),
            error=str(exc),
        )
        raise
    finally:
        _write_status(DATA_DIR, state="stopped", port=port, log=str(log_path))


__all__ = ["main"]
