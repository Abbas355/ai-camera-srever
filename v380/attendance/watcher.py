"""Background entrance-camera poller. Isolated from LiveHub internals via jpeg callback."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from v380.attendance.service import get_service

JpegProvider = Callable[[int], bytes | None]


class AttendanceWatcher:
    _instance: AttendanceWatcher | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._get_jpeg: JpegProvider | None = None
        self._running = False
        self._last_tick: float | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._ticks = 0
        self._lock = threading.Lock()
        # After a successful check-in/out, briefly slow down to avoid spam.
        self._quiet_until: float = 0.0

    @classmethod
    def instance(cls) -> AttendanceWatcher:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = AttendanceWatcher()
            return cls._instance

    def configure(self, get_jpeg: JpegProvider) -> None:
        self._get_jpeg = get_jpeg

    def apply_settings(self, settings: dict[str, Any] | None = None) -> None:
        svc = get_service()
        settings = settings or svc.get_settings()
        if settings.get("enabled") and settings.get("entrance_camera_id") is not None:
            self.start()
        else:
            self.stop()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._get_jpeg is None:
                return {"ok": False, "error": "Watcher not wired to camera snapshots yet"}
            if self._running:
                return self.status()
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                name="attendance-watcher",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            self._running = False
            t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        with self._lock:
            self._thread = None
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running and not self._stop.is_set(),
            "ticks": self._ticks,
            "last_tick": self._last_tick,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "wired": self._get_jpeg is not None,
        }

    def _tick_once(self, svc: Any, cam_id: int) -> dict[str, Any] | None:
        if self._get_jpeg is None:
            self._last_error = "no jpeg provider"
            return None
        jpeg = self._get_jpeg(int(cam_id))
        if not jpeg:
            self._last_error = "no jpeg"
            return None
        result = svc.process_frame(jpeg, camera_id=int(cam_id))
        self._last_result = {
            "matched": result.get("matched"),
            "action": result.get("action"),
            "staff_name": (result.get("staff") or {}).get("name"),
            "score": result.get("score"),
            "faces": result.get("faces"),
            "threshold": result.get("threshold"),
            "already_checked_in": result.get("already_checked_in"),
            "message": result.get("message"),
            "error": result.get("error"),
        }
        self._last_error = result.get("error")
        self._ticks += 1
        self._last_tick = time.time()
        return result

    def _loop(self) -> None:
        svc = get_service()
        while not self._stop.is_set():
            settings = svc.get_settings()
            # Fast default — people cross the door in under a second.
            base_interval = float(settings.get("poll_interval_sec") or 0.5)
            base_interval = max(0.35, min(2.0, base_interval))
            if not settings.get("enabled"):
                self._last_error = "disabled"
                self._stop.wait(max(1.0, base_interval))
                continue
            cam_id = settings.get("entrance_camera_id")
            if cam_id is None or self._get_jpeg is None:
                self._last_error = "no entrance camera"
                self._stop.wait(max(1.0, base_interval))
                continue

            now = time.time()
            # After a successful punch, pause briefly so we don't flip check-out immediately.
            if now < self._quiet_until:
                self._stop.wait(min(0.5, self._quiet_until - now))
                continue

            t0 = time.time()
            try:
                result = self._tick_once(svc, int(cam_id))
                # Burst: face seen but not matched (blur / angle) — retry live frames quickly.
                # Skip burst when the last tick was already slow (model under load).
                elapsed_pre = time.time() - t0
                if (
                    elapsed_pre < 1.2
                    and result
                    and not result.get("matched")
                    and not result.get("error")
                    and int(result.get("faces") or 0) > 0
                ):
                    for _ in range(3):
                        if self._stop.is_set():
                            break
                        self._stop.wait(0.2)
                        burst = self._tick_once(svc, int(cam_id))
                        if burst and burst.get("matched"):
                            result = burst
                            break
                if result and result.get("matched") and result.get("action") in {
                    "check_in",
                    "check_out",
                }:
                    self._quiet_until = time.time() + 4.0
            except Exception as exc:
                self._last_error = str(exc)

            elapsed = time.time() - t0
            # Under heavy InsightFace load, back off so UI enroll/identify stay responsive.
            if elapsed > 2.0:
                wait = max(0.8, base_interval)
            elif elapsed > 1.0:
                wait = max(0.35, base_interval - elapsed * 0.3)
            else:
                wait = max(0.05, base_interval - elapsed)
            self._stop.wait(wait)
        with self._lock:
            self._running = False


def start_attendance_watcher(get_jpeg: JpegProvider) -> None:
    """Call once from server main — does not alter other subsystems."""
    w = AttendanceWatcher.instance()
    w.configure(get_jpeg)
    w.apply_settings()
