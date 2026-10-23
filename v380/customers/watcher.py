"""Poll cameras for customer faces. Isolated; uses snapshot callback only."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from v380.customers.service import get_service

JpegProvider = Callable[[int], bytes | None]
CameraIdsProvider = Callable[[], list[int]]


class CustomerWatcher:
    _instance: CustomerWatcher | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._get_jpeg: JpegProvider | None = None
        self._list_cameras: CameraIdsProvider | None = None
        self._running = False
        self._ticks = 0
        self._last_tick: float | None = None
        self._last_error: str | None = None
        self._last_actions: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> CustomerWatcher:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = CustomerWatcher()
            return cls._instance

    def configure(self, get_jpeg: JpegProvider, list_cameras: CameraIdsProvider) -> None:
        self._get_jpeg = get_jpeg
        self._list_cameras = list_cameras

    def apply_settings(self, settings: dict[str, Any] | None = None) -> None:
        settings = settings or get_service().get_settings()
        if settings.get("enabled"):
            self.start()
        else:
            self.stop()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._get_jpeg is None or self._list_cameras is None:
                return {"ok": False, "error": "Customer watcher not wired yet"}
            if self._running:
                return self.status()
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                name="customer-watcher",
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
            "last_error": self._last_error,
            "last_actions": self._last_actions[-5:],
            "wired": self._get_jpeg is not None,
        }

    def _camera_ids(self) -> list[int]:
        settings = get_service().get_settings()
        configured = settings.get("camera_ids") or []
        if configured:
            return [int(x) for x in configured]
        if self._list_cameras is None:
            return []
        try:
            return [int(x) for x in self._list_cameras()]
        except Exception:
            return []

    def _loop(self) -> None:
        svc = get_service()
        while not self._stop.is_set():
            settings = svc.get_settings()
            interval = float(settings.get("poll_interval_sec") or 3.0)
            if not settings.get("enabled"):
                self._last_error = "disabled"
                self._stop.wait(interval)
                continue
            if self._get_jpeg is None:
                self._last_error = "no jpeg provider"
                self._stop.wait(interval)
                continue
            try:
                svc.close_stale()
                actions: list[dict[str, Any]] = []
                for cam_id in self._camera_ids():
                    jpeg = self._get_jpeg(int(cam_id))
                    if not jpeg:
                        continue
                    part = svc.process_camera_jpeg(int(cam_id), jpeg)
                    for row in part:
                        if row.get("matched"):
                            actions.append(
                                {
                                    "camera_id": cam_id,
                                    "customer": (row.get("customer") or {}).get("name"),
                                    "action": row.get("action"),
                                }
                            )
                        elif row.get("pending"):
                            actions.append(
                                {
                                    "camera_id": cam_id,
                                    "action": "pending",
                                    "area": row.get("area"),
                                    "pitch": row.get("pitch"),
                                }
                            )
                        elif row.get("skipped"):
                            actions.append(
                                {
                                    "camera_id": cam_id,
                                    "action": f"skip:{row.get('skipped')}",
                                    "score": row.get("score"),
                                    "area": row.get("area"),
                                }
                            )
                self._last_actions = actions
                self._ticks += 1
                self._last_tick = time.time()
                self._last_error = None
                # Periodically collapse duplicate Guests created from angle changes.
                if self._ticks % 25 == 0:
                    try:
                        svc.merge_near_duplicates()
                    except Exception:
                        pass
            except Exception as exc:
                self._last_error = str(exc)
            self._stop.wait(interval)
        with self._lock:
            self._running = False


def start_customer_watcher(get_jpeg: JpegProvider, list_cameras: CameraIdsProvider) -> None:
    w = CustomerWatcher.instance()
    w.configure(get_jpeg, list_cameras)
    w.apply_settings()
