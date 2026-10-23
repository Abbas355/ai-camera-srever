"""Save / recall table aims via ONVIF presets (and relative move recipes)."""

from __future__ import annotations

from typing import Callable

from v380.client import onvif_ptz
from v380.tables.store import TableStore
from v380.tables.types import PtzStep, Table, TableAim

# Keep ONVIF attempts short — EZVIZ often has no local PTZ service.
_ONVIF_PORTS = (8899, 80, 8000)


def _try_ports(
    fn: Callable[..., bool],
    ip: str,
    username: str,
    password: str,
    *args,
    ports: tuple[int, ...] = _ONVIF_PORTS,
) -> tuple[bool, int | None]:
    ip = (ip or "").strip()
    if not ip:
        return False, None
    for port in ports:
        try:
            if fn(ip, username, password, *args, port=port):
                return True, int(port)
        except Exception:
            continue
    return False, None


def save_onvif_preset(
    *,
    ip: str,
    username: str,
    password: str,
    slot: int,
    name: str = "",
) -> bool:
    slot = max(1, min(64, int(slot)))
    ok, _ = _try_ports(onvif_ptz.set_preset, ip, username, password, slot, name or f"Table {slot}")
    return ok


def goto_onvif_preset(
    *,
    ip: str,
    username: str,
    password: str,
    slot: int,
) -> bool:
    slot = max(1, min(64, int(slot)))
    ok, _ = _try_ports(onvif_ptz.goto_preset, ip, username, password, slot)
    return ok


def _net_from_moves(moves: list[PtzStep]) -> tuple[float, float, float]:
    pan = tilt = zoom = 0.0
    for m in moves:
        s = float(m.sec)
        if m.cmd == "ptz_right":
            pan += s
        elif m.cmd == "ptz_left":
            pan -= s
        elif m.cmd == "ptz_up":
            tilt += s
        elif m.cmd == "ptz_down":
            tilt -= s
        elif m.cmd == "ptz_zoom_in":
            zoom += s
        elif m.cmd == "ptz_zoom_out":
            zoom -= s
    return pan, tilt, zoom


def save_table_aim(
    store: TableStore,
    table: Table,
    *,
    ip: str,
    username: str,
    password: str,
    pan_sec: float = 0.0,
    tilt_sec: float = 0.0,
    zoom_sec: float = 0.0,
    moves: list[PtzStep] | list[tuple[str, float]] | None = None,
    speed: int = 5,
    try_onvif: bool = True,
) -> tuple[Table | None, str]:
    """Save current camera aim for a table.

    Prefer an ordered `moves` list recorded after Home — that is what Go replays.
    """
    if table.camera_id is None:
        return None, "Assign a camera to this table first"

    step_list: list[PtzStep] = []
    for item in moves or []:
        if isinstance(item, PtzStep):
            if item.sec >= 0.05:
                step_list.append(item)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            cmd, sec = str(item[0]), float(item[1])
            if cmd.startswith("ptz_") and sec >= 0.05:
                step_list.append(PtzStep(cmd=cmd, sec=sec))

    if step_list:
        pan_sec, tilt_sec, zoom_sec = _net_from_moves(step_list)

    has_rel = bool(step_list) or abs(float(pan_sec)) + abs(float(tilt_sec)) + abs(float(zoom_sec)) > 0.05
    if not has_rel and not try_onvif:
        return (
            None,
            "Aim not saved — click Home, aim at the table with the joystick, then Save aim.",
        )

    onvif_ok = False
    slot = table.aim.preset_slot
    if try_onvif:
        if slot is None:
            slot = store.next_preset_slot(int(table.camera_id), exclude_table_id=table.id)
        onvif_ok = save_onvif_preset(
            ip=ip,
            username=username,
            password=password,
            slot=int(slot),
            name=table.name,
        )

    if not onvif_ok and not has_rel:
        return (
            None,
            "Aim not saved — click Home, aim with the joystick (watch Track change), then Save aim.",
        )

    if not step_list and has_rel:
        step_list = TableAim(
            pan_sec=float(pan_sec),
            tilt_sec=float(tilt_sec),
            zoom_sec=float(zoom_sec),
        ).moves_from_net()

    aim = TableAim(
        preset_slot=int(slot) if onvif_ok and slot is not None else None,
        pan_sec=float(pan_sec),
        tilt_sec=float(tilt_sec),
        zoom_sec=float(zoom_sec),
        method="onvif" if onvif_ok else "relative",
        saved=True,
        moves=step_list,
        speed=max(1, min(7, int(speed))),
    )

    saved = store.set_aim(table.id, aim)
    if saved is None:
        return None, "Table not found"
    if onvif_ok:
        return saved, f"Saved {table.name} (preset #{slot})"
    n = len(step_list)
    return saved, (
        f"Saved {table.name}  ({n} move{'s' if n != 1 else ''} from Home — "
        f"pan {pan_sec:+.1f}s tilt {tilt_sec:+.1f}s)"
    )


def save_table_panorama_point(
    store: TableStore,
    table: Table,
    *,
    x: float,
    y: float,
) -> tuple[Table | None, str]:
    """Save a 360 tap coordinate (0..1) as the table aim — preferred for EZVIZ."""
    if table.camera_id is None:
        return None, "Assign a camera to this table first"
    x = max(0.0, min(1.0, float(x)))
    y = max(0.0, min(1.0, float(y)))
    aim = TableAim(
        preset_slot=table.aim.preset_slot,
        pan_sec=table.aim.pan_sec,
        tilt_sec=table.aim.tilt_sec,
        zoom_sec=table.aim.zoom_sec,
        method="panorama",
        saved=True,
        moves=list(table.aim.moves),
        speed=table.aim.speed,
        pano_x=x,
        pano_y=y,
    )
    saved = store.set_aim(table.id, aim)
    if saved is None:
        return None, "Table not found"
    return saved, f"Saved {table.name} at 360 point ({x:.2f}, {y:.2f})"


def goto_table_aim(
    table: Table,
    *,
    ip: str,
    username: str,
    password: str,
) -> tuple[bool, str]:
    """Move camera to a saved table aim.

    Preference: panorama coordinates → ONVIF preset → relative (Live View).
    """
    if not table.aim.is_set:
        return False, f"{table.name} has no saved camera aim yet"

    # 1) EZVIZ 360 / tap-to-look coordinates (best)
    if table.aim.has_panorama:
        try:
            from v380.client.ezviz_panorama import look_at

            ok, msg = look_at(ip, float(table.aim.pano_x), float(table.aim.pano_y))
            if ok:
                return True, f"Moving to {table.name} (360 point)"
            return False, msg
        except Exception as exc:
            return False, str(exc)

    slot = table.aim.preset_slot
    if slot is None:
        if table.aim.has_relative:
            return False, f"{table.name} uses relative aim — open Live View to recall"
        return False, f"{table.name} has no saved camera aim yet"
    ok = goto_onvif_preset(ip=ip, username=username, password=password, slot=int(slot))
    if ok:
        return True, f"Moving to {table.name}"
    if table.aim.has_relative:
        return False, f"{table.name} ONVIF failed — open Live View to recall relative aim"
    return False, f"Could not move to {table.name} — check camera PTZ / ONVIF"
