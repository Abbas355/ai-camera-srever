"""Poll floor camera for table occupancy. Isolated via jpeg callback."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from v380.occupancy.service import get_service

JpegProvider = Callable[[int], bytes | None]


class OccupancyWatcher:
    _instance: OccupancyWatcher | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._get_jpeg: JpegProvider | None = None
        self._running = False
        self._ticks = 0
        self._last_tick: float | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> OccupancyWatcher:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = OccupancyWatcher()
            return cls._instance

    def configure(self, get_jpeg: JpegProvider) -> None:
        self._get_jpeg = get_jpeg

    def apply_settings(self, settings: dict[str, Any] | None = None) -> None:
        """Manual Floor detect only — never auto-start YOLO/face polling.

        Tracking timers can be turned on later via /watcher/start; until then
        the loop stays stopped so boxes are not continuously redrawn.
        """
        _ = settings or get_service().get_settings()
        self.stop()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._get_jpeg is None:
                return {"ok": False, "error": "Occupancy watcher not wired yet"}
            if self._running:
                return self.status()
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="occupancy-watcher", daemon=True
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

    def _loop(self) -> None:
        svc = get_service()
        while not self._stop.is_set():
            settings = svc.get_settings()
            interval = float(settings.get("poll_interval_sec") or 2.5)
            if not settings.get("enabled"):
                self._last_error = "disabled"
                self._stop.wait(interval)
                continue
            cam_id = settings.get("camera_id")
            if cam_id is None or self._get_jpeg is None:
                self._last_error = "no camera"
                self._stop.wait(interval)
                continue
            try:
                jpeg = self._get_jpeg(int(cam_id))
                if jpeg:
                    result = svc.process_frame(jpeg, camera_id=int(cam_id))
                    self._last_result = {
                        "mode": result.get("mode"),
                        "faces": result.get("faces"),
                        "persons": result.get("persons"),
                        "tables": result.get("tables"),
                        "changes": result.get("changes"),
                        "states": result.get("states"),
                        "error": result.get("error"),
                    }
                    self._last_error = result.get("error")
                else:
                    self._last_error = "no jpeg"
                self._ticks += 1
                self._last_tick = time.time()
            except Exception as exc:
                self._last_error = str(exc)
            self._stop.wait(interval)
        with self._lock:
            self._running = False


def start_occupancy_watcher(get_jpeg: JpegProvider) -> None:
    """Wire JPEG provider only — do not start the poll loop (manual detect)."""
    w = OccupancyWatcher.instance()
    w.configure(get_jpeg)
    w.stop()
