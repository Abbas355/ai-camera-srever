"""Playback screen: pick camera and date, then hours and minutes — not a folder tree."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from v380.paths import DATA_DIR, REC_DIR
from v380.record.auto_record import rename_legacy_names
from v380.ui.profile import open_profile
from v380.ui.theme import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    FONT_HEAD,
    FONT_SMALL,
    MUTED,
    TEXT,
    TILE,
    ghost_button,
    primary_button,
    status_bar,
)
from v380.client.v380_client import _ffmpeg_exe, _win_hide_kwargs

CACHE_DIR = DATA_DIR / "clip_cache"
VIDEO_EXT = {".mp4", ".h264", ".h265"}
_HH = re.compile(r"^\d{2}$")
SERVER_FILE = DATA_DIR / "server.txt"


def _play_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], start_new_session=True)


def _base_stem(path) -> str:
    stem = str(getattr(path, "stem", "") or "")
    if stem.endswith(".partial"):
        return stem[: -len(".partial")]
    return stem


def _is_partial(path) -> bool:
    name = str(getattr(path, "name", "") or "").lower()
    return name.endswith(".partial.mp4")


def _play_rank(path) -> int:
    """Lower = better for playback. Finished mp4 > mid-hour partial > raw."""
    if _is_partial(path):
        return 1
    suf = str(getattr(path, "suffix", "") or "").lower()
    if suf == ".mp4":
        return 0
    return 2


def _clip_slot_key(path) -> str:
    stem = _base_stem(path)
    parent = str(getattr(getattr(path, "parent", None), "name", "") or "")
    if _HH.match(parent) and (_HH.match(stem) or stem.isdigit()):
        return f"{parent}/{stem}"
    return stem


def _dedupe_clips(clips: list) -> list:
    best: dict[str, object] = {}
    order: list[str] = []
    for path in clips:
        key = _clip_slot_key(path)
        if key not in best:
            best[key] = path
            order.append(key)
            continue
        if _play_rank(path) < _play_rank(best[key]):
            best[key] = path
    return [best[k] for k in order]


def _to_mp4(src: Path) -> Path:
    if src.suffix.lower() == ".mp4":
        return src
    # Open hourly files must stay raw — never remux to <hour>.mp4 while recording.
    if src.suffix.lower() in (".h264", ".h265"):
        from v380.client.extras import remux_snapshot

        fmt = "hevc" if src.suffix.lower() == ".h265" else "h264"
        got = remux_snapshot(src, fmt)
        if got is not None:
            return got
        partial = src.with_name(src.stem + ".partial.mp4")
        if partial.is_file() and partial.stat().st_size > 1024:
            return partial
        raise RuntimeError(
            "This hour is still recording. Wait about 1 minute after Rec starts, "
            "then press Refresh and play the orange “Recording…” clip."
        )
    dest = src.with_suffix(".mp4")
    if dest.is_file() and dest.stat().st_size > 1024:
        return dest
    exe = _ffmpeg_exe()
    if not exe:
        return src
    subprocess.run(
        [exe, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dest)],
        check=False,
        **_win_hide_kwargs(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dest if dest.is_file() and dest.stat().st_size > 1024 else src


def _ensure_playable(src: Path) -> Path:
    return _to_mp4(src)

def _cache_remote(ref) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = ref.rel.replace("\\", "/").replace("..", "").replace("/", "_")
    dest = CACHE_DIR / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = int(getattr(ref, "_size", 0) or 0)
    if dest.is_file() and expected and dest.stat().st_size == expected:
        return dest
    req = urllib.request.Request(ref.url)
    token = getattr(ref, "token", "") or ""
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-Token", token)
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
    return dest


class ClipRef:
    def __init__(self, rel: str, size: int, base: str, token: str = ""):
        self.rel = rel.replace("\\", "/")
        p = Path(self.rel)
        self.name = p.name
        self.stem = p.stem
        self.suffix = p.suffix
        self.parent = p.parent
        self._size = size
        self.token = token
        self.url = f"{base}/file/{self.rel}"

    def is_file(self) -> bool:
        return True

    def stat(self):
        return type("S", (), {"st_size": self._size})()


def _index_remote(base: str, token: str = "") -> dict[str, dict[str, list]]:
    req = urllib.request.Request(base.rstrip("/") + "/api/index")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-Token", token)
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    cameras: dict[str, dict[str, list]] = {}
    for cam, dates in (payload.get("cameras") or {}).items():
        for day, items in dates.items():
            clips = [ClipRef(it["rel"], int(it.get("size") or 0), base.rstrip("/"), token) for it in items]
            clips = _dedupe_clips(clips)
            clips.sort(key=lambda c: _clip_when(c))
            cameras.setdefault(cam, {})[day] = clips
    return cameras


def _size_text(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n / 1000:.0f} KB"


def _clip_when(path: Path) -> str:
    stem = _base_stem(path).replace("_", "-")
    parts = stem.split("-")
    parent = str(getattr(getattr(path, "parent", None), "name", "") or "")
    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
        return f"{parts[0]}:{parts[1]}:{parts[2]}"
    if _HH.match(stem) and _HH.match(parent):
        return f"{parent}:{stem}:00"
    if _HH.match(stem):
        return f"{stem}:00:00"
    return _base_stem(path)

def _hour_of(path: Path) -> str:
    when = _clip_when(path)
    return when[:2] if len(when) >= 2 and when[:2].isdigit() else "00"


def _index(root: Path) -> dict[str, dict[str, list[Path]]]:
    data: dict[str, dict[str, list[Path]]] = {}
    if not root.is_dir():
        return data
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2:
            continue
        camera = parts[0]
        date = parts[1] if len(parts) >= 3 else "other"
        data.setdefault(camera, {}).setdefault(date, []).append(path)
    for dates in data.values():
        for day, files in list(dates.items()):
            files = _dedupe_clips(files)
            files.sort(key=lambda p: _clip_when(p))
            dates[day] = files
    return data


class ClipsFrame(tk.Frame):
    def __init__(self, master, on_back, api=None):
        super().__init__(master, bg=BG)
        self._on_back = on_back
        self._api = api
        self._data: dict[str, dict[str, list[Path]]] = {}
        self._camera = ""
        self._date = ""
        self._hour = ""
        self._hour_files: dict[str, list] = {}
        self._hour_btns: dict[str, tk.Button] = {}
        self._date_btns: dict[str, tk.Button] = {}
        self._remote = api.base if api is not None else ""
        self._token = api.token if api is not None else ""

        top = tk.Frame(self, bg=CARD, padx=18, pady=12, highlightthickness=1, highlightbackground=BORDER)
        top.pack(fill="x")
        ghost_button(top, "← Home", self._on_back).pack(side="left")
        brand = tk.Frame(top, bg=CARD)
        brand.pack(side="left", padx=(12, 0))
        tk.Label(brand, text="Playback", fg=TEXT, bg=CARD, font=FONT_HEAD).pack(anchor="w")
        tk.Label(brand, text="Recordings by camera and day", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")
        ghost_button(top, "Refresh", self.reload).pack(side="right")
        ghost_button(top, "Profile", self._open_profile).pack(side="right", padx=(0, 8))
        tk.Label(top, text="Server IP", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(side="right", padx=(0, 8))
        self._server_var = tk.StringVar()
        if api is not None:
            self._server_var.set(api.base.replace("http://", "").replace("https://", "").split(":")[0])
        elif SERVER_FILE.is_file():
            self._server_var.set(SERVER_FILE.read_text(encoding="utf-8").strip())
        tk.Entry(
            top,
            textvariable=self._server_var,
            width=16,
            bg=TILE,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        ).pack(side="right", padx=(0, 8), ipady=4)
        ghost_button(top, "This PC", self._use_pc).pack(side="right", padx=(0, 8))
        primary_button(top, "Connect", self._use_server).pack(side="right", padx=(0, 8))

        pick = tk.Frame(self, bg=CARD, padx=18, pady=12)
        pick.pack(fill="x")
        tk.Label(pick, text="Camera", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(side="left")
        self._cam_var = tk.StringVar()
        self._cam_box = ttk.Combobox(pick, textvariable=self._cam_var, state="readonly", width=28, font=("Segoe UI", 11))
        self._cam_box.pack(side="left", padx=(8, 24))
        self._cam_box.bind("<<ComboboxSelected>>", lambda _e: self._pick_camera())
        tk.Label(pick, text="Day", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(side="left")
        self._dates_row = tk.Frame(pick, bg=CARD)
        self._dates_row.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.status = status_bar(self)
        self.status.pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(1, weight=1)

        tk.Label(body, text="Hours this day", fg=MUTED, bg=BG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(body, text="Minutes in this hour  ·  click to play", fg=MUTED, bg=BG, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )

        left = tk.Frame(body, bg=TILE)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        self._hours_canvas = tk.Canvas(left, bg=TILE, highlightthickness=0)
        hs = ttk.Scrollbar(left, orient="vertical", command=self._hours_canvas.yview)
        self._hours_inner = tk.Frame(self._hours_canvas, bg=TILE)
        self._hours_inner.bind(
            "<Configure>", lambda _e: self._hours_canvas.configure(scrollregion=self._hours_canvas.bbox("all"))
        )
        self._hours_canvas.create_window((0, 0), window=self._hours_inner, anchor="nw")
        self._hours_canvas.configure(yscrollcommand=hs.set)
        self._hours_canvas.pack(side="left", fill="both", expand=True)
        hs.pack(side="right", fill="y")

        right = tk.Frame(body, bg=TILE)
        right.grid(row=1, column=1, sticky="nsew")
        self._mins = tk.Frame(right, bg=TILE)
        self._mins.pack(fill="both", expand=True, padx=10, pady=10)

        self.reload()

    def _use_pc(self) -> None:
        self._remote = ""
        self.reload()

    def _use_server(self) -> None:
        ip = self._server_var.get().strip()
        if not ip:
            messagebox.showinfo("Server", "Type the Ubuntu IP, then Connect.")
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SERVER_FILE.write_text(ip, encoding="utf-8")
        self._remote = ip if "://" in ip else f"http://{ip}:8080"
        self._token = getattr(self._api, "token", "") if self._api is not None else ""
        self.reload()

    def _open_profile(self) -> None:
        open_profile(self, self._api, self.status)

    def reload(self) -> None:
        keep_cam, keep_date, keep_hour = self._camera, self._date, self._hour
        try:
            rename_legacy_names(REC_DIR)
        except Exception:
            pass
        if self._remote:
            try:
                self._data = _index_remote(self._remote, getattr(self, "_token", "") or "")
            except Exception as exc:
                messagebox.showerror("Server", f"Cannot reach {self._remote}\n{exc}")
                self._data = {}
        else:
            self._data = _index(REC_DIR)
        cameras = sorted(self._data)
        self._cam_box["values"] = cameras
        if not cameras:
            self._cam_var.set("")
            self._clear_dates()
            self._clear_hours()
            self._clear_mins()
            where = self._remote or str(REC_DIR)
            self.status.configure(text=f"No recordings yet.  ·  {where}")
            return
        cam = keep_cam if keep_cam in cameras else cameras[0]
        self._cam_var.set(cam)
        self._pick_camera(keep_date, keep_hour)

    def _pick_camera(self, prefer_date: str = "", prefer_hour: str = "") -> None:
        self._camera = self._cam_var.get()
        dates = sorted(self._data.get(self._camera, {}), reverse=True)
        self._clear_dates()
        self._date_btns.clear()
        if not dates:
            self.status.configure(text=f"{self._camera}  ·  no days yet")
            self._clear_hours()
            self._clear_mins()
            return
        for d in dates:
            n = len(self._data[self._camera][d])
            b = tk.Button(
                self._dates_row,
                text=f"  {d}  ({n})  ",
                fg=TEXT,
                bg="#1e2937",
                relief="flat",
                command=lambda day=d: self._pick_date(day),
            )
            b.pack(side="left", padx=(0, 6))
            self._date_btns[d] = b
        day = prefer_date if prefer_date in dates else dates[0]
        self._pick_date(day, prefer_hour)

    def _clear_dates(self) -> None:
        for child in self._dates_row.winfo_children():
            child.destroy()

    def _pick_date(self, day: str, prefer_hour: str = "") -> None:
        self._date = day
        for d, b in self._date_btns.items():
            b.configure(bg=ACCENT if d == day else "#1e2937", fg="white" if d == day else TEXT)
        files = self._data.get(self._camera, {}).get(day, [])
        hours: dict[str, list[Path]] = {}
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
            hours.setdefault(_hour_of(path), []).append(path)
        self._hour_files = hours
        self._draw_hours(prefer_hour)
        self.status.configure(
            text=f"{self._camera}   ·   {day}   ·   {len(files)} clips   ·   {len(hours)} hour(s)   ·   {_size_text(total)}"
        )

    def _draw_hours(self, prefer_hour: str = "") -> None:
        for child in self._hours_inner.winfo_children():
            child.destroy()
        self._hour_btns.clear()
        keys = sorted(self._hour_files)
        if not keys:
            tk.Label(self._hours_inner, text="No clips this day.", fg=MUTED, bg=TILE).pack(anchor="w", padx=12, pady=12)
            self._clear_mins()
            return
        for hour in keys:
            files = self._hour_files[hour]
            size = 0
            for p in files:
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
            row = tk.Frame(self._hours_inner, bg=CARD)
            row.pack(fill="x", padx=8, pady=4)
            live = any(_is_partial(p) or str(getattr(p, "suffix", "")).lower() in (".h264", ".h265") for p in files)
            label = f"  {hour}:00     {len(files)} clip(s)     {_size_text(size)}"
            if live:
                label += "     Recording…  "
            else:
                label += "  "
            b = tk.Button(
                row,
                text=label,
                anchor="w",
                fg=TEXT,
                bg=CARD,
                relief="flat",
                font=("Segoe UI", 11),
                command=lambda h=hour: self._pick_hour(h),
            )
            b.pack(side="left", fill="x", expand=True)
            best = sorted(files, key=_play_rank)[0]
            tk.Button(
                row,
                text="Play so far" if live else "Play",
                bg="#c2410c" if live else ACCENT,
                fg="white",
                relief="flat",
                command=lambda p=best: self._play_path(p),
            ).pack(side="right", padx=6, pady=4)
            self._hour_btns[hour] = b
        hour = prefer_hour if prefer_hour in keys else keys[0]
        self._pick_hour(hour)

    def _pick_hour(self, hour: str) -> None:
        self._hour = hour
        for h, b in self._hour_btns.items():
            b.configure(bg=ACCENT if h == hour else CARD, fg="white" if h == hour else TEXT)
        self._clear_mins()
        files = self._hour_files.get(hour, [])
        tk.Label(
            self._mins,
            text=f"{self._camera}  ·  {self._date}  ·  {hour}:00–{hour}:59",
            fg=TEXT,
            bg=TILE,
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        grid = tk.Frame(self._mins, bg=TILE)
        grid.pack(fill="both", expand=True)
        for i, path in enumerate(files):
            when = _clip_when(path)[3:5] if len(_clip_when(path)) >= 5 else _clip_when(path)
            ready = str(getattr(path, "suffix", "")).lower() == ".mp4"
            live = _is_partial(path) or not ready
            title = f"{hour}:{when}" if len(when) == 2 else _clip_when(path)
            if _is_partial(path):
                title = f"{hour} so far"
            elif not ready:
                title += " …"
            b = tk.Button(
                grid,
                text=title,
                width=10,
                bg="#c2410c" if live else "#1e2937",
                fg="white",
                relief="flat",
                command=lambda p=path: self._play_path(p),
            )
            b.grid(row=i // 6, column=i % 6, padx=4, pady=4, sticky="ew")
        if not files:
            tk.Label(self._mins, text="No clips in this hour.", fg=MUTED, bg=TILE).pack(anchor="w")
        elif any(_is_partial(p) or str(getattr(p, "suffix", "")).lower() in (".h264", ".h265") for p in files):
            tk.Label(
                self._mins,
                text="Orange = still recording. Play so far opens what’s written (~last 10–15 min after each refresh).",
                fg=MUTED,
                bg=TILE,
                font=FONT_SMALL,
            ).pack(anchor="w", pady=(8, 0))

    def _clear_hours(self) -> None:
        for child in self._hours_inner.winfo_children():
            child.destroy()
        self._hour_btns.clear()
        self._hour_files = {}

    def _clear_mins(self) -> None:
        for child in self._mins.winfo_children():
            child.destroy()

    def _play_path(self, path) -> None:
        if getattr(path, "url", None):
            self.status.configure(text="Opening clip in the player…")
            threading.Thread(target=self._play_remote, args=(path,), daemon=True).start()
            return
        if not path.is_file():
            messagebox.showinfo("Playback", "That clip is not on disk yet.")
            return
        self.status.configure(text="Preparing clip…")
        threading.Thread(target=self._play_local, args=(path,), daemon=True).start()

    def _play_local(self, path: Path) -> None:
        try:
            play = _ensure_playable(path)
            _play_file(play)
            msg = "Playing recording so far (hour still open)." if _is_partial(play) or _is_partial(path) else "Playing in the Windows player."
            self.after(0, lambda: self.status.configure(text=msg))
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror("Play failed", str(exc)))

    def _play_remote(self, ref) -> None:
        try:
            local = _cache_remote(ref)
            play = _ensure_playable(local)
            _play_file(play)
            self.after(0, lambda: self.status.configure(text="Playing in the Windows player (not the browser)."))
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror("Play failed", str(exc)))
