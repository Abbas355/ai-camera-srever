"""Background live tiles. Latest-frame slots + scaled decode for many cameras."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

from camera_store import Camera, CameraStore
from extras import get_relay_ip
from v380_client import LiveH264Decoder, V380SnapshotClient

OnState = Callable[[int, str], None]


def _decode_opts(count: int) -> dict:
    """Keep 1–3 tiles near live-view quality; scale down as the grid grows."""
    if count <= 1:
        return {"scale_width": 0, "jpeg_q": 5, "threads": None, "stream_quality": None}
    if count <= 3:
        return {"scale_width": 1280, "jpeg_q": 6, "threads": None, "stream_quality": None}
    if count <= 8:
        return {"scale_width": 960, "jpeg_q": 7, "threads": 1, "stream_quality": 0}
    if count <= 16:
        return {"scale_width": 640, "jpeg_q": 8, "threads": 1, "stream_quality": 0}
    return {"scale_width": 480, "jpeg_q": 8, "threads": 1, "stream_quality": 0}


class PreviewManager:
    def __init__(self, store: CameraStore, on_state: OnState, on_frame=None, max_cams: int = 64):
        self._store = store
        self._on_state = on_state
        self._on_frame = on_frame
        self._max = max_cams
        self._stop = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._latest: dict[int, bytes] = {}
        self._lock = threading.Lock()

    def start(self, cameras: list[Camera]) -> None:
        self.stop()
        self._stop = threading.Event()
        with self._lock:
            self._latest.clear()
        opts = _decode_opts(len(cameras))
        for i, cam in enumerate(cameras[: self._max]):
            t = threading.Thread(
                target=self._run_one,
                args=(cam, self._stop, i, opts),
                daemon=True,
            )
            self._threads[cam.id] = t
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._threads.clear()

    def take_latest(self, camera_id: int) -> bytes | None:
        with self._lock:
            return self._latest.pop(camera_id, None)

    def _publish(self, camera_id: int, jpeg: bytes) -> None:
        with self._lock:
            self._latest[camera_id] = jpeg

    def _run_one(self, cam: Camera, stop: threading.Event, index: int, opts: dict) -> None:
        if index:
            time.sleep(min(index * 0.12, 4.0))
        if stop.is_set():
            return
        self._on_state(cam.id, "Connecting…")
        quality = cam.quality if opts["stream_quality"] is None else opts["stream_quality"]
        while not stop.is_set():
            client = None
            decoder = None
            frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
            try:
                host = cam.ip
                if cam.source == "cloud":
                    host = get_relay_ip(int(cam.device_id))
                    if not host:
                        raise RuntimeError("No cloud relay")
                client = V380SnapshotClient(
                    host,
                    int(cam.device_id),
                    cam.username,
                    cam.password,
                    cam.port,
                    quality=quality,
                    source=cam.source,
                )
                client.connect()
                self._on_state(cam.id, "Live")
                self._store.set_status(cam.id, error="", seen=True)
                got_key = False
                for kind, is_iframe, payload in client.iter_video_frames(stop):
                    if kind != "video":
                        continue
                    if self._on_frame is not None:
                        try:
                            self._on_frame(cam, is_iframe, payload, client)
                        except Exception:
                            pass
                    if decoder is None:
                        decoder = LiveH264Decoder(
                            frames,
                            fmt=client.video_codec,
                            scale_width=opts["scale_width"],
                            jpeg_q=opts["jpeg_q"],
                            threads=opts["threads"],
                        )
                        self._store.set_status(
                            cam.id,
                            codec="hevc" if client.video_codec == "hevc" else "h264",
                            seen=True,
                        )
                    if is_iframe:
                        got_key = True
                    if not got_key:
                        continue
                    decoder.write_frame(bool(is_iframe), payload, client._sps, client._pps, client._vps)
                    while True:
                        try:
                            jpeg = frames.get_nowait()
                        except queue.Empty:
                            break
                        self._publish(cam.id, jpeg)
            except Exception as exc:
                if stop.is_set():
                    break
                self._on_state(cam.id, "Offline")
                self._store.set_status(cam.id, error=str(exc))
                time.sleep(2.5)
            finally:
                if decoder is not None:
                    decoder.close()
                if client is not None:
                    client.close()
        self._on_state(cam.id, "Stopped")
