"""Shared types for restaurant table → camera aims."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PtzStep:
    """One continuous PTZ hold from Home (same command used on replay)."""

    cmd: str  # ptz_left / ptz_right / ptz_up / ptz_down / ptz_zoom_*
    sec: float

    def to_dict(self) -> dict[str, Any]:
        return {"cmd": self.cmd, "sec": round(float(self.sec), 3)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PtzStep | None:
        if not data:
            return None
        cmd = str(data.get("cmd") or "")
        sec = float(data.get("sec") or 0.0)
        if not cmd.startswith("ptz_") or sec < 0.05:
            return None
        return cls(cmd=cmd, sec=sec)


@dataclass
class TableAim:
    """Where the camera should look for this table.

    Prefer ONVIF hardware preset when available.
    For EZVIZ / relative: store ordered `moves` from a fixed Home corner and
    replay those exact holds (much more accurate than net pan/tilt alone).
    """

    preset_slot: int | None = None
    pan_sec: float = 0.0
    tilt_sec: float = 0.0
    zoom_sec: float = 0.0
    method: str = ""  # "onvif" | "relative" | ""
    saved: bool = False
    # Ordered steps recorded after Home (authoritative for relative recall)
    moves: list[PtzStep] = field(default_factory=list)
    # EZVIZ PTZ speed used while recording (must match on replay)
    speed: int = 5
    # EZVIZ 360 / tap-to-look normalized coordinates (0..1) — preferred when set
    pano_x: float | None = None
    pano_y: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["moves"] = [m.to_dict() if isinstance(m, PtzStep) else m for m in self.moves]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TableAim:
        if not data:
            return cls()
        slot = data.get("preset_slot")
        moves_raw = data.get("moves") if isinstance(data.get("moves"), list) else []
        moves: list[PtzStep] = []
        for item in moves_raw:
            if isinstance(item, dict):
                step = PtzStep.from_dict(item)
                if step is not None:
                    moves.append(step)
        px = data.get("pano_x")
        py = data.get("pano_y")
        aim = cls(
            preset_slot=int(slot) if slot is not None else None,
            pan_sec=float(data.get("pan_sec") or 0.0),
            tilt_sec=float(data.get("tilt_sec") or 0.0),
            zoom_sec=float(data.get("zoom_sec") or 0.0),
            method=str(data.get("method") or ""),
            saved=bool(data.get("saved")),
            moves=moves,
            speed=int(data.get("speed") or 5),
            pano_x=float(px) if px is not None else None,
            pano_y=float(py) if py is not None else None,
        )
        # Backward compat: older files without `saved`
        if not aim.saved and (
            (aim.preset_slot is not None and int(aim.preset_slot) > 0)
            or aim.has_relative
            or aim.has_panorama
        ):
            aim.saved = True
        # If only pan/tilt exist, synthesize moves for replay
        if not aim.moves and aim.has_net_offsets:
            aim.moves = aim.moves_from_net()
        return aim

    @property
    def has_panorama(self) -> bool:
        return self.pano_x is not None and self.pano_y is not None

    @property
    def has_net_offsets(self) -> bool:
        return abs(self.pan_sec) + abs(self.tilt_sec) + abs(self.zoom_sec) > 0.05

    @property
    def has_relative(self) -> bool:
        return bool(self.moves) or self.has_net_offsets

    @property
    def is_set(self) -> bool:
        if self.saved:
            return True
        if self.has_panorama:
            return True
        if self.preset_slot is not None and int(self.preset_slot) > 0:
            return True
        return self.has_relative

    def moves_from_net(self) -> list[PtzStep]:
        """Fallback: convert net pan/tilt/zoom into up to 3 axis steps."""
        out: list[PtzStep] = []
        if abs(self.pan_sec) > 0.05:
            out.append(
                PtzStep(
                    "ptz_right" if self.pan_sec > 0 else "ptz_left",
                    abs(self.pan_sec),
                )
            )
        if abs(self.tilt_sec) > 0.05:
            out.append(
                PtzStep(
                    "ptz_up" if self.tilt_sec > 0 else "ptz_down",
                    abs(self.tilt_sec),
                )
            )
        if abs(self.zoom_sec) > 0.05:
            out.append(
                PtzStep(
                    "ptz_zoom_in" if self.zoom_sec > 0 else "ptz_zoom_out",
                    abs(self.zoom_sec),
                )
            )
        return out

    def replay_moves(self) -> list[PtzStep]:
        return list(self.moves) if self.moves else self.moves_from_net()


@dataclass
class Table:
    id: str
    name: str
    camera_id: int | None = None
    # Floor-map position 0..1 (left/top relative)
    x: float = 0.5
    y: float = 0.5
    aim: TableAim = field(default_factory=TableAim)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "camera_id": self.camera_id,
            "x": self.x,
            "y": self.y,
            "aim": self.aim.to_dict(),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Table:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "Table"),
            camera_id=int(data["camera_id"]) if data.get("camera_id") is not None else None,
            x=float(data.get("x") if data.get("x") is not None else 0.5),
            y=float(data.get("y") if data.get("y") is not None else 0.5),
            aim=TableAim.from_dict(data.get("aim") if isinstance(data.get("aim"), dict) else None),
            notes=str(data.get("notes") or ""),
        )
