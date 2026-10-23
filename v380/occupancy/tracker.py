"""Multi-object tracker for Floor occupancy (ByteTrack-style IDs).

Works with sparse JPEG polls (~2–4s): IoU + center distance association.
Optionally uses Ultralytics ByteTrack when model.track() is available.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    aa = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    ba = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = aa + ba - inter
    return float(inter / union) if union > 1e-12 else 0.0


@dataclass
class _Track:
    track_id: int
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    hits: int = 1
    misses: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) * 0.5

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) * 0.5


class IoUTracker:
    """Greedy IoU tracker — stable IDs across sparse frames."""

    def __init__(
        self,
        *,
        iou_thresh: float = 0.25,
        max_misses: int = 4,
        max_center_frac: float = 0.35,
    ) -> None:
        self.iou_thresh = float(iou_thresh)
        self.max_misses = int(max_misses)
        self.max_center_frac = float(max_center_frac)
        self._tracks: dict[str, list[_Track]] = {
            "person": [],
            "chair": [],
            "table": [],
        }
        self._next_id = 1
        self._lock = threading.Lock()

    def update(
        self,
        boxes: list[Any],
        *,
        kind: str,
        frame_w: float = 1.0,
        frame_h: float = 1.0,
    ) -> list[Any]:
        """Assign track_id on each BoxDet-like object; return same list."""
        kind = kind if kind in self._tracks else "person"
        now = time.time()
        with self._lock:
            tracks = self._tracks[kind]
            for t in tracks:
                t.misses += 1

            used_tracks: set[int] = set()
            # Score pairs (iou, -dist, box_i, track_i)
            pairs: list[tuple[float, float, int, int]] = []
            diag = max(1.0, (float(frame_w) ** 2 + float(frame_h) ** 2) ** 0.5)
            for bi, b in enumerate(boxes):
                br = (float(b.x1), float(b.y1), float(b.x2), float(b.y2))
                bcx = (br[0] + br[2]) * 0.5
                bcy = (br[1] + br[3]) * 0.5
                for ti, t in enumerate(tracks):
                    if ti in used_tracks:
                        continue
                    iou = _iou(br, t.rect)
                    dist = ((bcx - t.cx) ** 2 + (bcy - t.cy) ** 2) ** 0.5 / diag
                    if iou < self.iou_thresh and dist > self.max_center_frac:
                        continue
                    score = iou + max(0.0, 0.35 - dist)
                    if score < 0.12:
                        continue
                    pairs.append((score, -dist, bi, ti))

            pairs.sort(key=lambda x: (x[0], x[1]), reverse=True)
            assigned_boxes: set[int] = set()
            for _score, _nd, bi, ti in pairs:
                if bi in assigned_boxes or ti in used_tracks:
                    continue
                assigned_boxes.add(bi)
                used_tracks.add(ti)
                b = boxes[bi]
                t = tracks[ti]
                t.x1, t.y1, t.x2, t.y2 = float(b.x1), float(b.y1), float(b.x2), float(b.y2)
                t.conf = float(getattr(b, "conf", 0.0) or 0.0)
                t.hits += 1
                t.misses = 0
                t.last_seen = now
                b.track_id = int(t.track_id)

            for bi, b in enumerate(boxes):
                if bi in assigned_boxes:
                    continue
                tid = self._next_id
                self._next_id += 1
                tracks.append(
                    _Track(
                        track_id=tid,
                        kind=kind,
                        x1=float(b.x1),
                        y1=float(b.y1),
                        x2=float(b.x2),
                        y2=float(b.y2),
                        conf=float(getattr(b, "conf", 0.0) or 0.0),
                        last_seen=now,
                    )
                )
                b.track_id = tid

            # Drop stale tracks
            self._tracks[kind] = [t for t in tracks if t.misses <= self.max_misses]
        return boxes

    def reset(self) -> None:
        with self._lock:
            for k in self._tracks:
                self._tracks[k] = []
            self._next_id = 1


class CameraTrackHub:
    """One IoUTracker per floor camera."""

    _instance: CameraTrackHub | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._by_cam: dict[int, IoUTracker] = {}
        self._inner = threading.Lock()

    @classmethod
    def instance(cls) -> CameraTrackHub:
        with cls._lock:
            if cls._instance is None:
                cls._instance = CameraTrackHub()
            return cls._instance

    def tracker(self, camera_id: int | None) -> IoUTracker:
        cam = int(camera_id or 0)
        with self._inner:
            if cam not in self._by_cam:
                self._by_cam[cam] = IoUTracker()
            return self._by_cam[cam]


@dataclass
class TemporalTableState:
    """Require consecutive occupied evidence before flipping a table filled."""

    filled_streak: int = 0
    empty_streak: int = 0
    last_status: str = "empty"  # filled|empty|uncertain
    last_person_track: int | None = None


class TemporalOccupancy:
    """Smooth flicker: occupied only after N consecutive positive frames."""

    def __init__(self, *, occupy_frames: int = 2, free_frames: int = 2) -> None:
        self.occupy_frames = max(1, int(occupy_frames))
        self.free_frames = max(1, int(free_frames))
        self._by_key: dict[str, TemporalTableState] = {}
        self._lock = threading.Lock()

    def update(
        self,
        *,
        key: str,
        candidate_filled: bool,
        person_track_id: int | None = None,
    ) -> str:
        """Return 'filled' | 'empty' | 'uncertain'."""
        with self._lock:
            st = self._by_key.get(key) or TemporalTableState()
            if candidate_filled:
                st.filled_streak += 1
                st.empty_streak = 0
                if person_track_id is not None:
                    st.last_person_track = int(person_track_id)
                if st.filled_streak >= self.occupy_frames:
                    st.last_status = "filled"
                elif st.last_status != "filled":
                    st.last_status = "uncertain"
            else:
                st.empty_streak += 1
                st.filled_streak = 0
                if st.empty_streak >= self.free_frames:
                    st.last_status = "empty"
                    st.last_person_track = None
                elif st.last_status == "filled":
                    # Hold occupied briefly during gaps.
                    st.last_status = "filled"
            self._by_key[key] = st
            return st.last_status


class CameraTemporalHub:
    _instance: CameraTemporalHub | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._by_cam: dict[int, TemporalOccupancy] = {}
        self._inner = threading.Lock()

    @classmethod
    def instance(cls) -> CameraTemporalHub:
        with cls._lock:
            if cls._instance is None:
                cls._instance = CameraTemporalHub()
            return cls._instance

    def temporal(self, camera_id: int | None) -> TemporalOccupancy:
        cam = int(camera_id or 0)
        with self._inner:
            if cam not in self._by_cam:
                self._by_cam[cam] = TemporalOccupancy(occupy_frames=2, free_frames=2)
            return self._by_cam[cam]
