"""Per-camera background recording.

Hourly:  recordings/<name>/<YYYY-MM-DD>/<HH>.h264 → .mp4
1-min:   recordings/<name>/<YYYY-MM-DD>/<HH>/<MM>.h264 → .mp4

The open chunk stays raw. Only a finished slot is remuxed.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REC_TZ_NAME = os.environ.get("V380_TZ", "Asia/Karachi")


def _tz():
    try:
        return ZoneInfo(REC_TZ_NAME)
    except Exception:
        return timezone(timedelta(hours=5))


def _now() -> datetime:
    return datetime.now(_tz())

from v380.client.extras import get_relay_ip, remux_annexb
from v380.client.v380_client import V380SnapshotClient
from v380.paths import DATA_DIR, REC_DIR, ROOT as APP_DIR, WORKER_SCRIPT
from v380.store.camera_store import Camera, CameraStore
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(10)}
_FOLDER_LOCK = threading.Lock()
_REMUX_Q: queue.Queue[tuple[Path, str] | None] = queue.Queue()
_REMUX_STARTED = False
_REMUX_WORKERS = 4

# Auto-delete: keep recent days, and always leave free disk space.
KEEP_DAYS = max(1, int(os.environ.get("V380_KEEP_DAYS", "7")))
MIN_FREE_GB = max(1.0, float(os.environ.get("V380_MIN_FREE_GB", "30")))
_CLEANUP_EVERY_SEC = 300.0
_REMUX_ORPHANS_EVERY_SEC = 90.0


def sanitize_folder(name: str, device_id: str) -> str:
    cleaned = _WIN_BAD.sub("_", (name or "").strip()).strip(" .")
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned.upper() in _RESERVED:
        cleaned = f"cam_{device_id}"
    return cleaned[:80]


def _read_marker(path: Path) -> str | None:
    marker = path / ".device_id"
    try:
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return None


def _write_marker(path: Path, device_id: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".device_id"
    if _read_marker(path) != device_id:
        marker.write_text(device_id, encoding="utf-8")


def _find_folder_by_id(root: Path, device_id: str) -> Path | None:
    try:
        for child in root.iterdir():
            if child.is_dir() and _read_marker(child) == device_id:
                return child
    except OSError:
        return None
    return None


def camera_folder(cam: Camera, rec_dir: Path | None = None) -> Path:
    """Stable folder for this device: reuse by .device_id, then name, then name_id."""
    root = rec_dir or REC_DIR
    with _FOLDER_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        existing = _find_folder_by_id(root, cam.device_id)
        base = sanitize_folder(cam.name, cam.device_id)
        preferred = root / base
        alt = root / f"{base}_{cam.device_id}"

        if existing is not None:
            if existing in (preferred, alt):
                return existing
            target = preferred if not preferred.exists() else alt
            if target != existing and not target.exists():
                try:
                    existing.rename(target)
                    _write_marker(target, cam.device_id)
                    return target
                except OSError:
                    return existing
            return existing

        path = preferred
        if path.exists() and (_read_marker(path) not in (None, cam.device_id) or path.is_file()):
            path = alt
        _write_marker(path, cam.device_id)
        if _read_marker(path) != cam.device_id:
            path = alt
            _write_marker(path, cam.device_id)
        return path


_VIDEO_SUFFIXES = {".mp4", ".ts", ".h264", ".h265", ".mkv"}


def day_has_media(day_dir: Path) -> bool:
    if not day_dir.is_dir():
        return False
    try:
        for path in day_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES:
                # Ignore HLS packaging junk next to recordings.
                if ".hls" in path.parts or path.name == "index.m3u8":
                    continue
                return True
    except OSError:
        return False
    return False


def resolve_recording_folder_for_day(cam: Camera, day: str, rec_dir: Path | None = None) -> str:
    """Pick the on-disk folder that actually has footage for this camera/day.

    Cameras can end up with renamed folders (name vs name_device_id). Playback
    must follow the folder that contains media, not only camera_folder().
    """
    root = rec_dir or REC_DIR
    day = (day or "").strip()
    candidates: list[Path] = []
    try:
        candidates.append(camera_folder(cam, root))
    except Exception:
        pass
    base = sanitize_folder(cam.name, cam.device_id)
    for name in (base, f"{base}_{cam.device_id}", str(cam.device_id or "").strip()):
        if name:
            candidates.append(root / name)
    try:
        for child in root.iterdir():
            if child.is_dir() and _read_marker(child) == cam.device_id:
                candidates.append(child)
    except OSError:
        pass

    seen: set[str] = set()
    with_media: list[Path] = []
    existing: list[Path] = []
    for path in candidates:
        try:
            key = str(path.resolve()) if path.exists() else str(path)
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_dir():
            continue
        existing.append(path)
        if day and day_has_media(path / day):
            with_media.append(path)

    if with_media:
        return with_media[0].name
    if existing:
        return existing[0].name
    return sanitize_folder(cam.name, cam.device_id)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HH_RE = re.compile(r"^\d{2}$")


def chunk_mode(value: str | None) -> str:
    # Default to 1-minute clips (SD-card style): fast scrub, smaller files, exact times.
    v = str(value or "minute").strip().lower()
    return "hour" if v == "hour" else "minute"


def _chunk_stamp(now: datetime, mode: str) -> tuple[str, str, str | None, datetime]:
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%H")
    if mode == "minute":
        minute = now.strftime("%M")
        until = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return day, hour, minute, until
    until = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return day, hour, None, until


def _raw_ext(fmt: str) -> str:
    return "h265" if fmt == "hevc" else "h264"


def _slot_stem(hour: str, minute: str | None) -> str:
    if minute is None:
        return f"{hour}-00-00"
    return f"{hour}-{minute}-00"


def _chunk_raw_path(folder: Path, day: str, hour: str, minute: str | None, fmt: str) -> Path:
    """Hourly: date/HH-00-00.h264. Minute: date/HH/HH-MM-00.h264."""
    folder.mkdir(parents=True, exist_ok=True)
    day_dir = folder / day
    day_dir.mkdir(parents=True, exist_ok=True)
    ext = _raw_ext(fmt)
    if minute is None:
        return day_dir / f"{_slot_stem(hour, None)}.{ext}"
    hour_dir = day_dir / hour
    hour_dir.mkdir(parents=True, exist_ok=True)
    return hour_dir / f"{_slot_stem(hour, minute)}.{ext}"


def is_current_slot(path: Path, now: datetime | None = None) -> bool:
    """True if this raw file is the still-open clock slot (must not be remuxed/deleted)."""
    now = now or _now()
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%H")
    minute = now.strftime("%M")
    # EZVIZ hour files are .ts; V380 uses annex-B .h264/.h265
    if path.suffix.lower() not in (".h264", ".h265", ".ts"):
        return False
    # Preview / snap leftovers are never the live slot.
    lower = path.name.lower()
    if lower.endswith(".snap.ts") or lower.startswith("seg_"):
        return False
    stem = path.stem
    parent = path.parent.name
    grand = path.parent.parent.name if path.parent.parent else ""
    if parent == day and stem == _slot_stem(hour, None):
        return True
    if parent == hour and grand == day and stem == _slot_stem(hour, minute):
        return True
    if _DATE_RE.match(parent) and _HH_RE.match(stem):
        return parent == day and stem == hour
    if _HH_RE.match(parent) and _DATE_RE.match(grand) and _HH_RE.match(stem):
        return grand == day and parent == hour and stem == minute
    return False


def should_leave_raw(path: Path, now: datetime | None = None) -> bool:
    if path.name.startswith("rec_"):
        return True
    # Never remux/delete the open clock slot while the recorder may still write it.
    now = now or _now()
    if path.suffix.lower() == ".ts":
        day = now.strftime("%Y-%m-%d")
        hour = now.strftime("%H")
        minute = now.strftime("%M")
        parent = path.parent.name
        # Hour layout: day/HH.ts
        if _DATE_RE.match(parent):
            slot = _hour_from_recording_name(path.name)
            if parent == day and slot == hour:
                return True
        # Minute layout: day/HH/MM.ts
        grand = path.parent.parent.name if path.parent.parent else ""
        if _DATE_RE.match(grand) and _HH_RE.match(parent) and _HH_RE.match(path.stem):
            if grand == day and parent == hour and path.stem == minute:
                return True
    return is_current_slot(path, now)


def _mp4_has_moov(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 64:
            return False
        with path.open("rb") as fh:
            head = fh.read(min(262144, size))
            if b"moov" in head:
                return True
            if size > 262144:
                fh.seek(max(0, size - 262144))
                return b"moov" in fh.read(262144)
    except OSError:
        return False
    return False


def _ezviz_finalize_ts(
    ts_path: Path,
    mp4_path: Path,
    *,
    cam_label: str,
    timeout: int = 120,
) -> None:
    """Remux a finished MPEG-TS clip to MP4.

    Keyframe-aligned segment recording should already start at an IDR. If a reconnect
    still leaves a late first keyframe, trim as a safety net (avoids ~10s seek jump).
    """
    try:
        if not ts_path.is_file() or ts_path.stat().st_size < 2048:
            ts_path.unlink(missing_ok=True)
            return
    except OSError:
        return
    if mp4_path.is_file() and _mp4_has_moov(mp4_path):
        # Already finalized — drop leftover .ts if present.
        try:
            if ts_path.stat().st_size <= mp4_path.stat().st_size * 1.1:
                ts_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp4_path.with_name(mp4_path.stem + ".tmp.mp4")
    try:
        from v380.record.keyframe_fix import (
            first_video_keyframe_sec,
            remux_starting_at,
        )

        kf = first_video_keyframe_sec(ts_path, timeout=min(90, timeout))
        # Only trim when clearly mid-GOP; keep near-full minute when already clean.
        start = float(kf) if kf is not None and kf > 0.45 else 0.0
        ok = remux_starting_at(ts_path, tmp, start_sec=start, timeout=timeout)
        if ok and _mp4_has_moov(tmp):
            tmp.replace(mp4_path)
            ts_path.unlink(missing_ok=True)
            for legacy_name in (
                f"{ts_path.stem}.partial.mp4",
                f"{mp4_path.stem.split('-')[0]}.mp4",
                f"{ts_path.stem}.mp4",
            ):
                legacy = mp4_path.with_name(legacy_name)
                if legacy != mp4_path and legacy.is_file():
                    try:
                        legacy.unlink(missing_ok=True)
                    except OSError:
                        pass
            if start > 0.45:
                _log(
                    _data_dir(),
                    f"ezviz finalize keyframe-trim {cam_label} "
                    f"{mp4_path.name} ss={start:.2f}s",
                )
            try:
                from v380.record.hls import ensure_hls_async

                ensure_hls_async(mp4_path)
            except Exception:
                pass
        else:
            tmp.unlink(missing_ok=True)
            _log(_data_dir(), f"ezviz finalize incomplete {cam_label} {ts_path.name}")
    except Exception as exc:
        _log(_data_dir(), f"ezviz finalize {cam_label}: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def rename_legacy_names(root: Path | None = None) -> int:
    """Turn old 27.mp4 / 14.mp4 names into 14-27-00.mp4 / 14-00-00.mp4."""
    root = root or REC_DIR
    if not root.exists():
        return 0
    clock = _now()
    done = 0
    for path in list(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".mp4", ".h264", ".h265"):
            continue
        stem = path.stem
        if not _HH_RE.match(stem):
            continue
        if path.suffix.lower() != ".mp4" and should_leave_raw(path, clock):
            continue
        # Never promote incomplete MP4s (missing moov) — they stay broken under new names
        if path.suffix.lower() == ".mp4" and not _mp4_has_moov(path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        parent = path.parent.name
        if _HH_RE.match(parent):
            new_name = f"{parent}-{stem}-00{path.suffix.lower()}"
        elif _DATE_RE.match(parent):
            new_name = f"{stem}-00-00{path.suffix.lower()}"
        else:
            continue
        dest = path.with_name(new_name)
        if dest.exists():
            continue
        try:
            path.rename(dest)
            done += 1
        except OSError:
            pass
    return done


_OPEN_RAW: set[str] = set()
_OPEN_LOCK = threading.Lock()


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _mark_open(path: Path | None, open_: bool) -> None:
    if path is None:
        return
    keys = {_path_key(path), str(path)}
    with _OPEN_LOCK:
        if open_:
            _OPEN_RAW.update(keys)
        else:
            _OPEN_RAW.difference_update(keys)


def _parse_ymd(text: str) -> date | None:
    if not _DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _date_folder_of(path: Path, root: Path) -> date | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    for part in rel.parts:
        parsed = _parse_ymd(part)
        if parsed is not None:
            return parsed
    return None


def _hour_from_recording_name(name: str) -> str | None:
    base = name
    lower = name.lower()
    for suf in (".partial.mp4", ".tmp.mp4", ".snap.ts", ".mp4", ".ts", ".h264", ".h265"):
        if lower.endswith(suf):
            base = name[: -len(suf)]
            break
    m = re.match(r"^(\d{2})(?:-\d{2}-\d{2})?$", base)
    return m.group(1) if m else None


def _is_media_file(path: Path) -> bool:
    lower = path.name.lower()
    if lower.endswith((".partial.mp4", ".tmp.mp4", ".snap.ts")):
        return True
    return path.suffix.lower() in {".mp4", ".ts", ".h264", ".h265"}


def _is_protected_media(path: Path, root: Path, now: datetime) -> bool:
    """Never delete the open hour, markers, or files the recorder still holds."""
    if path.name == ".device_id" or not path.is_file():
        return True
    with _OPEN_LOCK:
        if str(path) in _OPEN_RAW or _path_key(path) in _OPEN_RAW:
            return True
    if should_leave_raw(path, now):
        return True
    day = _date_folder_of(path, root)
    if day != now.date():
        return False
    hour = _hour_from_recording_name(path.name)
    if hour != now.strftime("%H"):
        return False
    # Current clock hour under today's folder — leave it alone.
    return True


def _free_bytes(path: Path) -> int | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _unlink_quiet(path: Path) -> int:
    try:
        size = path.stat().st_size if path.is_file() else 0
        path.unlink(missing_ok=True)
        # Drop matching HLS folder so scrub cache does not orphan on delete.
        if path.suffix.lower() in {".mp4", ".ts", ".h264", ".h265"}:
            hls = path.with_name(path.stem + ".hls")
            if hls.is_dir():
                import shutil

                try:
                    for child in hls.rglob("*"):
                        if child.is_file():
                            size += child.stat().st_size
                    shutil.rmtree(hls, ignore_errors=True)
                except OSError:
                    pass
        return size
    except OSError:
        return 0


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        folder = Path(dirpath)
        if folder == root:
            continue
        try:
            next(folder.iterdir())
        except StopIteration:
            try:
                folder.rmdir()
            except OSError:
                pass
        except OSError:
            pass


def _iter_recording_media(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        for path in root.rglob("*"):
            if path.is_file() and _is_media_file(path):
                out.append(path)
    except OSError:
        pass
    return out


def cleanup_recordings(
    rec_dir: Path | None = None,
    *,
    keep_days: int | None = None,
    min_free_gb: float | None = None,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Delete old recordings (keep_days) and free disk until min_free_gb is available.

    Never removes the current open hour or in-use recorder files.
    """
    root = rec_dir or REC_DIR
    clock = now or _now()
    days = KEEP_DAYS if keep_days is None else max(1, int(keep_days))
    floor_gb = MIN_FREE_GB if min_free_gb is None else max(1.0, float(min_free_gb))
    floor = int(floor_gb * (1024**3))
    removed = 0
    freed = 0
    cache = cache_dir if cache_dir is not None else (DATA_DIR / "clip_cache")

    # 1) Age retention — whole calendar days older than keep_days
    for path in list(_iter_recording_media(root)):
        if _is_protected_media(path, root, clock):
            continue
        day = _date_folder_of(path, root)
        if day is None:
            continue
        age = (clock.date() - day).days
        if age < days:
            continue
        freed += _unlink_quiet(path)
        removed += 1
    _prune_empty_dirs(root)

    # Age-clean regenerable clip cache too
    if cache.is_dir():
        for path in list(cache.glob("*")):
            if not path.is_file():
                continue
            try:
                age_sec = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age_sec >= days * 86400:
                freed += _unlink_quiet(path)
                removed += 1

    # 2) Free-space floor — oldest finished media first
    free = _free_bytes(root)
    if free is not None and free < floor:
        candidates: list[tuple[date, str, float, Path]] = []
        for path in _iter_recording_media(root):
            if _is_protected_media(path, root, clock):
                continue
            day = _date_folder_of(path, root) or date(1970, 1, 1)
            hour = _hour_from_recording_name(path.name) or "99"
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((day, hour, mtime, path))
        if cache.is_dir():
            for path in cache.glob("*"):
                if not path.is_file():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                candidates.append((date(1970, 1, 1), "00", mtime, path))
        candidates.sort(key=lambda row: (row[0], row[1], row[2]))
        for _day, _hour, _mtime, path in candidates:
            free = _free_bytes(root)
            if free is None or free >= floor:
                break
            try:
                under_rec = root in path.parents or path == root
                under_cache = cache in path.parents or path.parent == cache
            except Exception:
                continue
            if under_rec:
                if _is_protected_media(path, root, clock):
                    continue
            elif not under_cache:
                continue
            freed += _unlink_quiet(path)
            removed += 1
        _prune_empty_dirs(root)

    free_after = _free_bytes(root)
    return {
        "removed": removed,
        "freed_bytes": freed,
        "free_bytes": free_after,
        "keep_days": days,
        "min_free_gb": floor_gb,
    }


def storage_summary(rec_dir: Path | None = None) -> dict:
    """Days of footage on disk + free space for the Playback status chip."""
    root = rec_dir or REC_DIR
    days: set[date] = set()
    if root.is_dir():
        try:
            for path in root.rglob("*"):
                if not path.is_dir():
                    continue
                parsed = _parse_ymd(path.name)
                if parsed is None:
                    continue
                try:
                    if any(p.is_file() and _is_media_file(p) for p in path.iterdir()):
                        days.add(parsed)
                    else:
                        # Hour subfolders under the day still count
                        for child in path.iterdir():
                            if child.is_dir() and any(
                                p.is_file() and _is_media_file(p) for p in child.rglob("*")
                            ):
                                days.add(parsed)
                                break
                except OSError:
                    continue
        except OSError:
            pass
    free = _free_bytes(root)
    free_gb = round((free or 0) / (1024**3), 1) if free is not None else None
    return {
        "days_on_disk": len(days),
        "oldest": min(days).isoformat() if days else None,
        "newest": max(days).isoformat() if days else None,
        "free_bytes": free,
        "free_gb": free_gb,
        "keep_days": KEEP_DAYS,
        "min_free_gb": MIN_FREE_GB,
    }


def remux_orphans(rec_dir: Path | None, now: datetime | None = None) -> int:
    """Remux finished leftover raw files. Never touch the current slot or rec_*."""
    root = rec_dir or REC_DIR
    if not root.exists():
        return 0
    rename_legacy_names(root)
    clock = now or _now()
    done = 0
    for ext, fmt in ((".h264", "h264"), (".h265", "hevc")):
        for path in root.rglob(f"*{ext}"):
            if should_leave_raw(path, clock):
                continue
            with _OPEN_LOCK:
                busy = str(path) in _OPEN_RAW or _path_key(path) in _OPEN_RAW
            if busy:
                continue
            try:
                if path.stat().st_size <= 0:
                    path.unlink(missing_ok=True)
                    continue
                out = remux_annexb(path, fmt)
                if out is not None and out.suffix == ".mp4":
                    done += 1
                    try:
                        from v380.record.hls import ensure_hls_async

                        ensure_hls_async(out)
                    except Exception:
                        pass
            except OSError:
                pass
    # EZVIZ MPEG-TS leftovers → playable MP4 (past hours only; never touch open hour).
    from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

    exe = _ffmpeg_exe()
    if exe:
        for path in list(root.rglob("*.ts")):
            name_l = path.name.lower()
            if name_l.endswith(".snap.ts") or name_l.startswith("seg_"):
                continue
            if any(part.endswith(".hls") for part in path.parts):
                continue
            if should_leave_raw(path, clock):
                continue
            with _OPEN_LOCK:
                busy = str(path) in _OPEN_RAW or _path_key(path) in _OPEN_RAW
            if busy:
                continue
            try:
                if path.stat().st_size < 2048:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            parent = path.parent.name
            stem = path.stem
            if _HH_RE.match(parent) and _HH_RE.match(stem):
                mp4_path = path.with_name(f"{parent}-{stem}-00.mp4")
            elif _DATE_RE.match(parent) and _HH_RE.match(stem):
                mp4_path = path.with_name(f"{stem}-00-00.mp4")
            else:
                continue  # unknown layout — leave alone
            if mp4_path.is_file() and _mp4_has_moov(mp4_path):
                # Finished mp4 already present — drop leftover raw (safe for past hours).
                try:
                    if path.stat().st_size <= mp4_path.stat().st_size * 1.05:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            tmp = mp4_path.with_name(mp4_path.stem + ".tmp.mp4")
            try:
                size_hint = int(path.stat().st_size)
            except OSError:
                size_hint = 0
            # Small / broken leftovers must not block the recorder for 30 minutes.
            remux_timeout = 120 if size_hint < 50_000_000 else 1800
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "timeout": remux_timeout,
                "check": False,
            }
            kwargs.update(_win_hide_kwargs())
            try:
                proc = subprocess.run(
                    [
                        exe,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-analyzeduration",
                        "50M",
                        "-probesize",
                        "50M",
                        "-i",
                        str(path),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        "-f",
                        "mp4",
                        str(tmp),
                    ],
                    **kwargs,
                )
                if tmp.is_file() and tmp.stat().st_size > 1024 and _mp4_has_moov(tmp):
                    tmp.replace(mp4_path)
                    path.unlink(missing_ok=True)
                    # Drop stale mid-hour preview now that the hour is finished.
                    try:
                        path.with_name(f"{stem}.partial.mp4").unlink(missing_ok=True)
                    except OSError:
                        pass
                    done += 1
                    try:
                        from v380.record.hls import ensure_hls_async

                        ensure_hls_async(mp4_path)
                    except Exception:
                        pass
                else:
                    tmp.unlink(missing_ok=True)
                    err = (proc.stderr or b"")[-400:].decode("utf-8", "replace")
                    if err.strip():
                        _log(_data_dir(), f"remux orphan failed {path.name}: {err.strip()}")
                    # Unrecoverable TS (e.g. dimensions not set) — stop retrying every 90s.
                    if "dimensions not set" in err or "Invalid argument" in err:
                        try:
                            path.rename(path.with_suffix(path.suffix + ".bad"))
                        except OSError:
                            pass
            except Exception as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                _log(_data_dir(), f"remux orphan error {path.name}: {exc}")
    return done


def _ensure_remux_workers() -> None:
    global _REMUX_STARTED
    if _REMUX_STARTED:
        return
    _REMUX_STARTED = True
    for _ in range(_REMUX_WORKERS):
        threading.Thread(target=_remux_worker, daemon=True).start()


def _remux_worker() -> None:
    while True:
        item = _REMUX_Q.get()
        try:
            if item is None:
                return
            path, fmt = item
            for _ in range(5):
                try:
                    out = remux_annexb(path, fmt)
                    if out is not None and out.suffix == ".mp4":
                        try:
                            from v380.record.hls import ensure_hls_async

                            ensure_hls_async(out)
                        except Exception:
                            pass
                        break
                    if out is None or not path.exists():
                        break
                except Exception:
                    pass
                time.sleep(1.0)
        except Exception:
            pass
        finally:
            _REMUX_Q.task_done()


def _schedule_remux(path: Path, fmt: str) -> Path:
    _ensure_remux_workers()
    _REMUX_Q.put((path, fmt))
    return path.with_suffix(".mp4")


def wait_remux(timeout: float = 180.0) -> None:
    done = threading.Event()

    def _join() -> None:
        _REMUX_Q.join()
        done.set()

    threading.Thread(target=_join, daemon=True).start()
    done.wait(timeout)


class _ChunkWriter:
    """Writes one stable slot file. Splits on the first I-frame after the clock."""

    # While an hour file is still open, refresh a playable .partial.mp4 so users can
    # watch earlier minutes without waiting for the hour to finish.
    PARTIAL_EVERY_SEC = 60
    PARTIAL_FIRST_SEC = 45

    def __init__(self, cam: Camera, rec_dir: Path | None = None, remux=None):
        self._cam = cam
        self._rec_dir = rec_dir
        self._remux = remux or _schedule_remux
        self._mode = chunk_mode(getattr(cam, "record_chunk", "minute"))
        self._fh = None
        self._raw: Path | None = None
        self._fmt = "h264"
        self._until = _now()
        self._bytes = 0
        self._opened_at = 0.0
        self._last_partial = 0.0

    def feed(self, now: datetime, fmt: str, is_iframe: bool, annexb: bytes) -> None:
        if not annexb:
            return
        want = "hevc" if fmt == "hevc" else "h264"
        if self._fh is None:
            if not is_iframe:
                return
            self._open(now, want)
            self._write(annexb)
            return
        rotate = is_iframe and (now >= self._until or want != self._fmt)
        if rotate:
            self.close()
            self._open(now, want)
        self._write(annexb)
        self._maybe_partial()

    def _open(self, now: datetime, fmt: str) -> None:
        day, hour, minute, until = _chunk_stamp(now, self._mode)
        raw = _chunk_raw_path(camera_folder(self._cam, self._rec_dir), day, hour, minute, fmt)
        mode = "ab" if raw.exists() else "wb"
        self._raw = raw
        self._fh = raw.open(mode)
        self._bytes = raw.stat().st_size
        self._fmt = fmt
        self._until = until
        self._opened_at = time.time()
        self._last_partial = 0.0
        self._last_flush = time.time()
        _mark_open(raw, True)

    def _write(self, annexb: bytes) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(annexb)
            self._bytes += len(annexb)
            # Flush occasionally — every-frame flush stalls live decode on the same disk.
            now = time.time()
            if now - getattr(self, "_last_flush", 0) >= 1.0 or self._bytes % (2 * 1024 * 1024) < len(annexb):
                self._fh.flush()
                self._last_flush = now
        except OSError:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            _mark_open(self._raw, False)

    def _maybe_partial(self) -> None:
        if self._mode != "hour" or self._raw is None or self._bytes < 50_000:
            return
        now_ts = time.time()
        age = now_ts - self._opened_at
        due_first = self._last_partial <= 0 and age >= self.PARTIAL_FIRST_SEC
        due_next = self._last_partial > 0 and (now_ts - self._last_partial) >= self.PARTIAL_EVERY_SEC
        if not (due_first or due_next):
            return
        self._last_partial = now_ts
        try:
            if self._fh is not None:
                self._fh.flush()
                os.fsync(self._fh.fileno())
        except OSError:
            pass
        path = self._raw
        fmt = self._fmt

        def work() -> None:
            try:
                from v380.client.extras import remux_snapshot

                remux_snapshot(path, fmt)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="partial-remux").start()

    def close(self) -> Path | None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        path = self._raw
        self._raw = None
        wrote = self._bytes
        self._bytes = 0
        _mark_open(path, False)
        if path is None:
            return None
        # Drop stale partial when the real hour remux finishes.
        try:
            path.with_name(path.stem + ".partial.mp4").unlink(missing_ok=True)
        except OSError:
            pass
        if wrote <= 0:
            path.unlink(missing_ok=True)
            return None
        return self._remux(path, self._fmt)


# Older tests / imports
_HourWriter = _ChunkWriter


class AutoRecordManager:
    def __init__(self, rec_dir: Path | None = None):
        self._rec_dir = rec_dir
        self._stop = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._flags: dict[int, threading.Event] = {}
        self._alive: dict[int, bool] = {}
        # off | starting | recording | reconnecting | error
        self._state: dict[int, str] = {}
        self._last_byte_at: dict[int, float] = {}
        self._spec: dict[int, tuple] = {}
        self._lock = threading.Lock()

    def _set_cam_state(self, cam_id: int, state: str, *, writing: bool | None = None) -> None:
        self._state[cam_id] = state
        if writing is not None:
            self._alive[cam_id] = bool(writing)
        else:
            self._alive[cam_id] = state == "recording"

    def sync(self, cameras: list[Camera]) -> None:
        wanted = {c.id: c for c in cameras if c.auto_record and c.id}
        with self._lock:
            for cam_id, flag in list(self._flags.items()):
                if cam_id not in wanted:
                    flag.set()
                    self._spec.pop(cam_id, None)
                    self._set_cam_state(cam_id, "off", writing=False)
            for cam in wanted.values():
                spec = (
                    cam.ip,
                    cam.port,
                    cam.username,
                    cam.password,
                    cam.quality,
                    cam.source,
                    chunk_mode(getattr(cam, "record_chunk", "minute")),
                    cam.name,
                )
                alive = cam.id in self._threads and self._threads[cam.id].is_alive()
                if alive and self._spec.get(cam.id) == spec:
                    continue
                old = self._flags.get(cam.id)
                if old is not None:
                    old.set()
                flag = threading.Event()
                self._flags[cam.id] = flag
                self._spec[cam.id] = spec
                self._set_cam_state(cam.id, "starting", writing=False)
                t = threading.Thread(target=self._run, args=(cam, flag), daemon=True, name=f"auto-rec-{cam.id}")
                self._threads[cam.id] = t
                t.start()

    def is_recording(self, camera_id: int) -> bool:
        # True only when the file grew recently — not just "ffmpeg was spawned".
        if not bool(self._alive.get(camera_id)):
            return False
        last = float(self._last_byte_at.get(camera_id) or 0)
        return bool(last) and (time.time() - last) <= 12.0

    def snapshot(self) -> dict[int, bool]:
        ids = set(self._state) | set(self._alive)
        return {cam_id: self.is_recording(cam_id) for cam_id in ids}

    def states(self) -> dict[int, str]:
        out: dict[int, str] = {}
        now = time.time()
        for cam_id, state in self._state.items():
            if state == "recording":
                last = float(self._last_byte_at.get(cam_id) or 0)
                if not last or (now - last) > 12.0:
                    out[cam_id] = "reconnecting"
                    continue
            out[cam_id] = state
        return out

    def stop_all(self) -> None:
        self._stop.set()
        with self._lock:
            for flag in self._flags.values():
                flag.set()
            threads = list(self._threads.values())
        for t in threads:
            t.join(timeout=8)
        wait_remux(120)
        with self._lock:
            self._flags.clear()
            self._threads.clear()
            self._spec.clear()
            self._alive.clear()
            self._state.clear()
            self._last_byte_at.clear()

    def _run(self, cam: Camera, flag: threading.Event) -> None:
        if getattr(cam, "is_ezviz", False) or str(getattr(cam, "brand", "") or "").lower() == "ezviz":
            self._run_ezviz(cam, flag)
            return
        writer = _ChunkWriter(cam, self._rec_dir)
        self._set_cam_state(cam.id, "starting", writing=False)
        try:
            while not self._stop.is_set() and not flag.is_set():
                client = None
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
                        quality=cam.quality,
                        source=cam.source,
                    )
                    client.connect()
                    need_key = True
                    for kind, is_iframe, payload in client.iter_video_frames(flag):
                        if self._stop.is_set() or flag.is_set():
                            break
                        if kind != "video":
                            continue
                        if need_key and not is_iframe:
                            continue
                        need_key = False
                        rec = client.h264_for_decode(payload) if is_iframe else payload
                        writer.feed(_now(), client.video_codec, bool(is_iframe), rec)
                        self._last_byte_at[cam.id] = time.time()
                        self._set_cam_state(cam.id, "recording", writing=True)
                except Exception as exc:
                    self._set_cam_state(cam.id, "reconnecting", writing=False)
                    _log(_data_dir(), f"record {cam.device_id}: {type(exc).__name__}: {exc}")
                    if self._stop.is_set() or flag.is_set():
                        break
                    time.sleep(1.0)
                finally:
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass
        finally:
            writer.close()
            with self._lock:
                if self._flags.get(cam.id) is flag:
                    self._set_cam_state(cam.id, "off", writing=False)
                    self._flags.pop(cam.id, None)
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)

    def _run_ezviz(self, cam: Camera, flag: threading.Event) -> None:
        """EZVIZ: FFmpeg copy RTSP → MPEG-TS (kill-safe), then finalize to MP4."""
        from v380.client.ezviz_rtsp import resolve_rtsp_url
        from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

        self._set_cam_state(cam.id, "starting", writing=False)
        exe = _ffmpeg_exe()
        url = resolve_rtsp_url(cam)
        if not exe or not url:
            _log(_data_dir(), f"ezviz record {cam.device_id}: missing ffmpeg or RTSP URL")
            with self._lock:
                if self._flags.get(cam.id) is flag:
                    self._set_cam_state(cam.id, "error", writing=False)
                    self._flags.pop(cam.id, None)
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)
            return
        mode = chunk_mode(getattr(cam, "record_chunk", "minute"))
        # Minute mode: keyframe-aligned segment muxer (full ~60s clips that start at t=0).
        if mode == "minute":
            try:
                self._run_ezviz_minute_segments(cam, flag, exe, url)
            finally:
                with self._lock:
                    if self._flags.get(cam.id) is flag:
                        self._set_cam_state(cam.id, "off", writing=False)
                        self._flags.pop(cam.id, None)
                    if self._threads.get(cam.id) is threading.current_thread():
                        self._threads.pop(cam.id, None)
            return

        def _finalize(ts_path: Path, mp4_path: Path) -> None:
            """Remux kill-safe .ts into a playable .mp4 with moov atom."""
            _ezviz_finalize_ts(ts_path, mp4_path, cam_label=cam.device_id, timeout=1800)

        def _schedule_ts_partial(ts_path: Path) -> None:
            def work() -> None:
                try:
                    from v380.client.extras import remux_ts_snapshot

                    remux_ts_snapshot(ts_path)
                except Exception:
                    pass

            threading.Thread(target=work, daemon=True, name="ezviz-partial").start()

        try:
            while not self._stop.is_set() and not flag.is_set():
                now = _now()
                day, hour, minute, until = _chunk_stamp(now, mode)
                folder = camera_folder(cam, self._rec_dir)
                stem_dir = folder / day
                ts_path = stem_dir / f"{hour}.ts"
                mp4_path = stem_dir / f"{hour}-00-00.mp4"
                stem_dir.mkdir(parents=True, exist_ok=True)
                kwargs = {
                    "stdin": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                kwargs.update(_win_hide_kwargs())
                last_partial = 0.0
                partial_first = 45.0
                partial_every = 60.0
                last_reconnect_log = 0.0
                # Stay inside this clock slot: RTSP drops must reconnect fast and APPEND.
                while not self._stop.is_set() and not flag.is_set() and _now() < until:
                    proc = None
                    out_fh = None
                    opened_at = time.time()
                    try:
                        ts_bytes = 0
                        try:
                            if ts_path.is_file():
                                ts_bytes = int(ts_path.stat().st_size)
                        except OSError:
                            ts_bytes = 0
                        append_ts = ts_bytes >= 2048
                        if (
                            append_ts
                            and mp4_path.is_file()
                            and _mp4_has_moov(mp4_path)
                            and _now() < until
                        ):
                            try:
                                mp4_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                        cmd = [
                            exe,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-rtsp_transport",
                            "tcp",
                            "-analyzeduration",
                            "3M",
                            "-probesize",
                            "3M",
                            "-fflags",
                            "+genpts",
                            "-i",
                            url,
                            "-c",
                            "copy",
                            "-f",
                            "mpegts",
                        ]
                        if append_ts:
                            out_fh = ts_path.open("ab")
                            cmd.extend(["pipe:1"])
                            proc = subprocess.Popen(cmd, stdout=out_fh, **kwargs)
                        else:
                            cmd.extend(["-y", str(ts_path)])
                            proc = subprocess.Popen(
                                cmd,
                                stdout=subprocess.DEVNULL,
                                **kwargs,
                            )
                        _mark_open(ts_path, True)
                        self._set_cam_state(cam.id, "starting", writing=False)
                        last_size = ts_bytes
                        while not self._stop.is_set() and not flag.is_set() and _now() < until:
                            if proc.poll() is not None:
                                break
                            try:
                                sz = int(ts_path.stat().st_size) if ts_path.is_file() else 0
                            except OSError:
                                sz = 0
                            if sz > last_size + 2048:
                                last_size = sz
                                self._last_byte_at[cam.id] = time.time()
                                self._set_cam_state(cam.id, "recording", writing=True)
                            now_ts = time.time()
                            age = now_ts - opened_at
                            due = (last_partial <= 0 and age >= partial_first) or (
                                last_partial > 0
                                and (now_ts - last_partial) >= partial_every
                            )
                            if due and sz >= 50_000:
                                last_partial = now_ts
                                _schedule_ts_partial(ts_path)
                            time.sleep(0.5)
                    except Exception as exc:
                        self._set_cam_state(cam.id, "error", writing=False)
                        _log(
                            _data_dir(),
                            f"ezviz record {cam.device_id}: {type(exc).__name__}: {exc}",
                        )
                    finally:
                        if proc is not None and proc.poll() is None:
                            try:
                                proc.terminate()
                                proc.wait(timeout=5)
                            except Exception:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                        if out_fh is not None:
                            try:
                                out_fh.close()
                            except Exception:
                                pass
                        self._set_cam_state(cam.id, "reconnecting", writing=False)

                    if self._stop.is_set() or flag.is_set() or _now() >= until:
                        break
                    self._set_cam_state(cam.id, "reconnecting", writing=False)
                    now_ts = time.time()
                    if now_ts - last_reconnect_log >= 15.0:
                        last_reconnect_log = now_ts
                        _log(
                            _data_dir(),
                            f"ezviz reconnect {cam.device_id} slot={hour}:00 "
                            f"— keeping {ts_path.name}",
                        )
                    time.sleep(1.5)

                _mark_open(ts_path, False)
                stopping = self._stop.is_set() or flag.is_set()
                if _now() >= until or stopping:
                    ts_f, mp4_f = ts_path, mp4_path

                    def _bg_finalize(ts=ts_f, mp4=mp4_f) -> None:
                        try:
                            _finalize(ts, mp4)
                        except Exception as exc:
                            _log(_data_dir(), f"ezviz bg finalize {cam.device_id}: {exc}")

                    threading.Thread(
                        target=_bg_finalize, daemon=True, name=f"finalize-{hour}"
                    ).start()
        finally:
            with self._lock:
                if self._flags.get(cam.id) is flag:
                    self._set_cam_state(cam.id, "off", writing=False)
                    self._flags.pop(cam.id, None)
                if self._threads.get(cam.id) is threading.current_thread():
                    self._threads.pop(cam.id, None)

    def _run_ezviz_minute_segments(
        self, cam: Camera, flag: threading.Event, exe: str, url: str
    ) -> None:
        """Minute clips via ffmpeg segment muxer — split on keyframes at clock time.

        Each new ``MM.ts`` starts on an IDR with PTS reset to 0, so playback begins
        at t=0 without trimming ~10s of mid-GOP junk. Segments may run slightly past
        the minute while waiting for the next keyframe (~60–70s).
        """
        from v380.client.v380_client import _win_hide_kwargs

        folder = camera_folder(cam, self._rec_dir)
        folder.mkdir(parents=True, exist_ok=True)
        # strftime pattern → recordings/<cam>/%Y-%m-%d/%H/%M.ts
        pattern = (folder / "%Y-%m-%d" / "%H" / "%M.ts").as_posix()
        size_hist: dict[str, tuple[int, float]] = {}
        scheduled: set[str] = set()
        last_live: Path | None = None
        last_reconnect_log = 0.0

        def _ensure_dirs(now: datetime) -> None:
            for hours_ahead in range(0, 3):
                t = now + timedelta(hours=hours_ahead)
                (folder / t.strftime("%Y-%m-%d") / t.strftime("%H")).mkdir(
                    parents=True, exist_ok=True
                )

        def _live_ts(now: datetime) -> Path:
            return folder / now.strftime("%Y-%m-%d") / now.strftime("%H") / f"{now.strftime('%M')}.ts"

        def _schedule_finalize(ts_path: Path) -> None:
            key = _path_key(ts_path)
            if key in scheduled:
                return
            if not ts_path.is_file():
                return
            try:
                if ts_path.stat().st_size < 2048:
                    return
            except OSError:
                return
            hour = ts_path.parent.name
            minute = ts_path.stem
            if not (_HH_RE.match(hour) and _HH_RE.match(minute)):
                return
            mp4_path = ts_path.with_name(f"{hour}-{minute}-00.mp4")
            scheduled.add(key)

            def work(ts=ts_path, mp4=mp4_path, sk=key) -> None:
                try:
                    _ezviz_finalize_ts(ts, mp4, cam_label=cam.device_id, timeout=120)
                except Exception as exc:
                    _log(_data_dir(), f"ezviz segment finalize {cam.device_id}: {exc}")
                finally:
                    scheduled.discard(sk)

            threading.Thread(
                target=work, daemon=True, name=f"finalize-{hour}-{minute}"
            ).start()

        def _poll_segments(now: datetime) -> None:
            """Track the .ts that is still growing (may overrun the clock waiting for IDR)."""
            nonlocal last_live
            now_ts = time.time()
            growing: Path | None = None
            candidates: list[Path] = []

            for day_name in {
                now.strftime("%Y-%m-%d"),
                (now - timedelta(days=1)).strftime("%Y-%m-%d"),
            }:
                day_dir = folder / day_name
                if not day_dir.is_dir():
                    continue
                for hour_dir in day_dir.iterdir():
                    if not hour_dir.is_dir() or not _HH_RE.match(hour_dir.name):
                        continue
                    for ts_path in hour_dir.glob("??.ts"):
                        if _HH_RE.match(ts_path.stem):
                            candidates.append(ts_path)

            for ts_path in candidates:
                try:
                    sz = int(ts_path.stat().st_size)
                except OSError:
                    continue
                if sz < 2048:
                    continue
                key = _path_key(ts_path)
                prev = size_hist.get(key)
                if prev is None or sz > prev[0]:
                    size_hist[key] = (sz, now_ts)
                    if prev is not None and sz > prev[0] + 1024:
                        growing = ts_path
                    continue
                # Unchanged size — if stable for a few seconds, segment is closed.
                size_hist[key] = (sz, prev[1] if prev else now_ts)
                if now_ts - size_hist[key][1] >= 2.5:
                    _schedule_finalize(ts_path)

            if growing is not None:
                if last_live is not None and last_live != growing:
                    _mark_open(last_live, False)
                last_live = growing
                _mark_open(growing, True)
                self._last_byte_at[cam.id] = time.time()
                self._set_cam_state(cam.id, "recording", writing=True)
            elif last_live is not None:
                # No growth this tick — keep open mark on last writer until stable finalize.
                _mark_open(last_live, True)

        while not self._stop.is_set() and not flag.is_set():
            now = _now()
            _ensure_dirs(now)
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            kwargs.update(_win_hide_kwargs())
            # segment_atclocktime + default break_non_keyframes=0 → wait for IDR after
            # the clock boundary, then open the next MM.ts with reset timestamps.
            cmd = [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-analyzeduration",
                "3M",
                "-probesize",
                "3M",
                "-fflags",
                "+genpts",
                "-i",
                url,
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                "60",
                "-segment_atclocktime",
                "1",
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                pattern,
            ]
            proc = None
            try:
                self._set_cam_state(cam.id, "starting", writing=False)
                proc = subprocess.Popen(cmd, **kwargs)
                _log(
                    _data_dir(),
                    f"ezviz segment record {cam.device_id}: keyframe-aligned minutes",
                )
                while not self._stop.is_set() and not flag.is_set():
                    if proc.poll() is not None:
                        break
                    _ensure_dirs(_now())
                    _poll_segments(_now())
                    time.sleep(0.5)
            except Exception as exc:
                self._set_cam_state(cam.id, "error", writing=False)
                _log(
                    _data_dir(),
                    f"ezviz segment record {cam.device_id}: {type(exc).__name__}: {exc}",
                )
            finally:
                if last_live is not None:
                    _mark_open(last_live, False)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                # Finalize whatever is left after stop/crash.
                try:
                    live = _live_ts(_now())
                    if live.is_file():
                        _schedule_finalize(live)
                    # Also sweep any other leftover .ts in today's hour.
                    hour_dir = live.parent
                    if hour_dir.is_dir():
                        for ts_path in hour_dir.glob("??.ts"):
                            _schedule_finalize(ts_path)
                except Exception:
                    pass

            if self._stop.is_set() or flag.is_set():
                break
            self._set_cam_state(cam.id, "reconnecting", writing=False)
            now_ts = time.time()
            if now_ts - last_reconnect_log >= 15.0:
                last_reconnect_log = now_ts
                _log(
                    _data_dir(),
                    f"ezviz segment reconnect {cam.device_id}",
                )
            time.sleep(0.8)


TASK_NAME = "V380StudioAutoRecord"
WATCH_TASK_NAME = "V380StudioAutoRecordWatch"
_MUTEX_HANDLE = None
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_HEARTBEAT_MAX_AGE = 15.0
_IDLE_EXIT_SEC = 120.0


def _data_dir(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("V380_DATA_DIR")
    return Path(env) if env else DATA_DIR


def _rec_dir(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("V380_REC_DIR")
    return Path(env) if env else REC_DIR


def _pid_path(data_dir: Path) -> Path:
    return data_dir / "record_worker.pid"


def _status_path(data_dir: Path) -> Path:
    return data_dir / "record_worker.json"


def _python_exe() -> str:
    return sys.executable


def _worker_script() -> Path:
    return WORKER_SCRIPT


def _log(data_dir: Path, message: str) -> None:
    path = data_dir / "record_worker.log"
    line = f"{_now().isoformat(timespec='seconds')} {message}\n"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.write_text(line, encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint(32768)
        query = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if query is not None and query(handle, 0, buf, ctypes.byref(size)):
            name = buf.value.lower()
            return "python" in Path(name).name
        return True
    finally:
        kernel32.CloseHandle(handle)


def read_worker_status(data_dir: Path | None = None) -> dict:
    path = _status_path(_data_dir(data_dir))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_worker_status(data_dir: Path, payload: dict) -> None:
    path = _status_path(data_dir)
    tmp = path.with_name(f"record_worker.{os.getpid()}.tmp")
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload)
    tmp.write_text(text, encoding="utf-8")
    last_err: OSError | None = None
    for _ in range(25):
        try:
            os.replace(str(tmp), str(path))
            return
        except OSError as exc:
            last_err = exc
            time.sleep(0.04)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        last_err = exc
    tmp.unlink(missing_ok=True)
    if last_err is not None:
        raise last_err


def _lease_path(data_dir: Path) -> Path:
    return data_dir / "studio.lease"


def touch_studio_lease(data_dir: Path | None = None) -> None:
    root = _data_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    _lease_path(root).write_text(json.dumps({"pid": os.getpid(), "heartbeat": time.time()}), encoding="utf-8")


def clear_studio_lease(data_dir: Path | None = None) -> None:
    _lease_path(_data_dir(data_dir)).unlink(missing_ok=True)


def studio_lease_fresh(data_dir: Path | None = None, max_age: float = 6.0) -> bool:
    path = _lease_path(_data_dir(data_dir))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        beat = float(raw.get("heartbeat") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return bool(beat) and (time.time() - beat) <= max_age


def is_worker_alive(data_dir: Path | None = None) -> bool:
    root = _data_dir(data_dir)
    status = read_worker_status(root)
    try:
        beat = float(status.get("heartbeat") or 0)
    except (TypeError, ValueError):
        beat = 0.0
    if beat and (time.time() - beat) <= _HEARTBEAT_MAX_AGE:
        return True
    try:
        pid = int(_pid_path(root).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = int(status.get("pid") or 0)
    return _pid_running(pid)


def _start_worker_process(data_dir: Path | None = None, rec_dir: Path | None = None) -> None:
    root = _data_dir(data_dir)
    rec = _rec_dir(rec_dir)
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["V380_DATA_DIR"] = str(root)
    env["V380_REC_DIR"] = str(rec)
    args = [_python_exe(), str(_worker_script())]
    kwargs: dict = {
        "cwd": str(APP_DIR),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        detached = 0x00000008
        flag_sets = (
            _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB | detached,
            _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB | _CREATE_NO_WINDOW,
            _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            _CREATE_NEW_PROCESS_GROUP,
        )
        last_err: OSError | None = None
        for flags in flag_sets:
            try:
                subprocess.Popen(args, creationflags=flags, **kwargs)
                return
            except OSError as exc:
                last_err = exc
        try:
            subprocess.Popen(
                f'start "" /B "{args[0]}" "{args[1]}"',
                shell=True,
                cwd=str(APP_DIR),
                env=env,
                creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError as exc:
            last_err = exc
        if last_err is not None:
            raise last_err
        return
    subprocess.Popen(args, start_new_session=True, **kwargs)


def ensure_worker(data_dir: Path | None = None, rec_dir: Path | None = None) -> bool:
    root = _data_dir(data_dir)
    if is_worker_alive(root):
        return True
    # systemd optiview-record owns the worker — API must not spawn a second one.
    if str(os.environ.get("V380_EXTERNAL_RECORD") or "").strip() in ("1", "true", "yes"):
        return is_worker_alive(root)
    _start_worker_process(root, rec_dir)
    for _ in range(25):
        time.sleep(0.1)
        if is_worker_alive(root):
            return True
    return is_worker_alive(root)


def logon_task_command() -> str:
    return f'"{_python_exe()}" "{_worker_script()}"'


def _run_schtasks(args: list[str]) -> None:
    subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)


def _sync_logon_task(enabled: bool) -> None:
    """Keep Optiview backend (API + recorder) auto-starting on Windows logon.

    Recording on/off no longer removes the task — the API stays warm after reboot.
    """
    if sys.platform != "win32":
        return
    try:
        from v380.windows_autostart import ensure_autostart

        # Respect user preference (UI toggle) — do not force-enable on record.
        ensure_autostart(None)
        if not enabled:
            # Legacy worker-only tasks are superseded by OptiviewBackend.
            _run_schtasks(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
            _run_schtasks(["schtasks", "/Delete", "/F", "/TN", WATCH_TASK_NAME])
    except Exception:
        pass


def _acquire_worker_mutex() -> bool:
    """Only one record worker may run (Windows mutex / Linux flock)."""
    global _MUTEX_HANDLE
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, True, "Local\\V380StudioAutoRecord")
        if not handle:
            return False
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    # Linux/macOS: exclusive lock file so API+systemd cannot double-record.
    import fcntl

    root = _data_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "record_worker.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    # Keep fd open for process lifetime (do not close).
    _MUTEX_HANDLE = fd
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
    except OSError:
        pass
    return True


def run_worker_forever(data_dir: Path | None = None, rec_dir: Path | None = None) -> None:
    root = _data_dir(data_dir)
    rec = _rec_dir(rec_dir)
    root.mkdir(parents=True, exist_ok=True)
    if not _acquire_worker_mutex():
        return
    _pid_path(root).write_text(str(os.getpid()), encoding="utf-8")
    _log(root, "worker started")
    # Start cameras first — never block recording on leftover remux / cleanup.
    store = CameraStore(root)
    mgr = AutoRecordManager(rec)

    def _bg_startup_housekeeping() -> None:
        try:
            remux_orphans(rec)
        except Exception as exc:
            _log(root, f"startup remux failed: {exc}")
        try:
            stats = cleanup_recordings(rec)
            if stats.get("removed"):
                _log(root, f"startup retention cleanup removed={stats['removed']}")
        except Exception as exc:
            _log(root, f"startup retention cleanup failed: {exc}")

    threading.Thread(
        target=_bg_startup_housekeeping, daemon=True, name="startup-remux"
    ).start()
    idle_since: float | None = None
    last_cleanup = 0.0
    last_remux = 0.0
    try:
        while True:
            try:
                cameras = store.list()
            except Exception as exc:
                _log(root, f"db read failed: {exc}")
                time.sleep(1.5)
                continue
            enabled = [c for c in cameras if c.auto_record]
            try:
                mgr.sync(cameras)
            except Exception as exc:
                _log(root, f"sync failed: {exc}")
            # Periodically finish leftover hour .ts → .mp4 (after reconnects / crashes).
            if time.time() - last_remux >= _REMUX_ORPHANS_EVERY_SEC:
                try:
                    n = remux_orphans(rec)
                    last_remux = time.time()
                    if n:
                        _log(root, f"orphan remux finished={n}")
                except Exception as exc:
                    last_remux = time.time()
                    _log(root, f"orphan remux failed: {exc}")
            if time.time() - last_cleanup >= _CLEANUP_EVERY_SEC:
                try:
                    stats = cleanup_recordings(rec)
                    last_cleanup = time.time()
                    if stats.get("removed"):
                        free_gb = (stats.get("free_bytes") or 0) / (1024**3)
                        _log(
                            root,
                            f"retention cleanup removed={stats['removed']} "
                            f"freed_mb={stats['freed_bytes'] // (1024 * 1024)} "
                            f"free_gb={free_gb:.1f} keep_days={stats['keep_days']} "
                            f"min_free_gb={stats['min_free_gb']}",
                        )
                except Exception as exc:
                    last_cleanup = time.time()
                    _log(root, f"retention cleanup failed: {exc}")
            try:
                write_worker_status(
                    root,
                    {
                        "pid": os.getpid(),
                        "heartbeat": time.time(),
                        "enabled": [c.id for c in enabled],
                        # True only while HDD bytes grew in the last ~12s.
                        "recording": {str(k): v for k, v in mgr.snapshot().items()},
                        # off | starting | recording | reconnecting | error
                        "states": {str(k): v for k, v in mgr.states().items()},
                        "keep_days": KEEP_DAYS,
                        "min_free_gb": MIN_FREE_GB,
                    },
                )
            except Exception as exc:
                _log(root, f"status write failed: {exc}")
            if enabled:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since >= _IDLE_EXIT_SEC:
                    _log(root, "idle exit — no cameras enabled")
                    break
            time.sleep(1.0)
    except KeyboardInterrupt:
        _log(root, "worker interrupted")
    except Exception as exc:
        _log(root, f"worker crash: {type(exc).__name__}: {exc}")
    finally:
        mgr.stop_all()
        store.close()
        try:
            _pid_path(root).unlink(missing_ok=True)
            _status_path(root).unlink(missing_ok=True)
        except OSError:
            pass
        _log(root, "worker stopped")


class RecordSupervisor:
    """GUI handle only. The detached worker is the single recorder."""

    def __init__(self, data_dir: Path | None = None, rec_dir: Path | None = None, remux=None):
        self._data_dir = _data_dir(data_dir)
        self._rec_dir = rec_dir
        self._enabled: set[int] = set()
        self._watch_at = 0.0
        self._status: dict = {}
        self._status_at = 0.0
        self._status_mtime = -1.0

    def _kick_worker(self) -> None:
        data_dir = self._data_dir
        rec_dir = self._rec_dir
        enabled = bool(self._enabled)

        def work() -> None:
            _sync_logon_task(enabled)
            if enabled:
                ensure_worker(data_dir, rec_dir)

        threading.Thread(target=work, daemon=True, name="rec-supervisor").start()

    def sync(self, cameras: list[Camera]) -> None:
        self._enabled = {c.id for c in cameras if c.auto_record}
        self._status_at = 0.0
        self._kick_worker()

    def watch(self) -> None:
        if not self._enabled:
            return
        now = time.time()
        if now - self._watch_at < 5.0:
            return
        self._watch_at = now
        if not is_worker_alive(self._data_dir):
            self._kick_worker()

    def on_video(self, cam: Camera, is_iframe: bool, payload: bytes, client) -> None:
        return None

    def _status_fresh(self) -> dict:
        now = time.time()
        path = _status_path(self._data_dir)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if now - self._status_at >= 0.25 or mtime != self._status_mtime:
            self._status = read_worker_status(self._data_dir)
            self._status_at = now
            self._status_mtime = mtime
        return self._status

    def service_running(self) -> bool:
        status = self._status_fresh()
        try:
            beat = float(status.get("heartbeat") or 0)
        except (TypeError, ValueError):
            return False
        return bool(beat) and (time.time() - beat) <= _HEARTBEAT_MAX_AGE

    def is_recording(self, camera_id: int) -> bool:
        if not self.service_running():
            return False
        rec = self._status_fresh().get("recording") or {}
        if isinstance(rec, dict):
            return bool(rec.get(str(camera_id)) or rec.get(camera_id))
        return False

    def cam_state(self, camera_id: int) -> str:
        states = self._status_fresh().get("states") or {}
        if isinstance(states, dict):
            raw = states.get(str(camera_id)) or states.get(camera_id)
            if raw:
                return str(raw)
        if self.is_recording(camera_id):
            return "recording"
        if self.service_running():
            return "starting"
        return "off"

    def camera_status(self, camera_id: int, enabled: bool) -> tuple[str, str]:
        """Human status for one camera: (text, level off|ok|wait|down)."""
        if not enabled:
            return "Service: off", "off"
        if not self.service_running():
            return "Service: not running", "down"
        state = self.cam_state(camera_id)
        if state == "recording":
            return "Service: recording (HDD writing)", "ok"
        if state == "reconnecting":
            return "Service: reconnecting (stream down)", "wait"
        if state == "error":
            return "Service: error", "down"
        if state == "starting":
            return "Service: starting…", "wait"
        return "Service: waiting…", "wait"

    def service_summary(self, cameras: list[Camera]) -> str:
        wanted = [c for c in cameras if c.auto_record]
        if not wanted:
            return "Record service: off"
        if not self.service_running():
            return "Record service: not running  ·  waiting to start"
        writing = sum(1 for c in wanted if self.is_recording(c.id))
        reconnecting = sum(1 for c in wanted if self.cam_state(c.id) == "reconnecting")
        if reconnecting:
            return (
                f"Record service: running  ·  {writing}/{len(wanted)} saving"
                f"  ·  {reconnecting} reconnecting"
            )
        return f"Record service: running  ·  {writing}/{len(wanted)} camera(s) saving"

    def release_to_worker(self) -> None:
        if self._enabled:
            self._kick_worker()

    def stop_all(self) -> None:
        return None
