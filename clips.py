"""Playback screen: pick camera and date, then hours and minutes — not a folder tree."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from auto_record import rename_legacy_names
from theme import ACCENT, BG, CARD, MUTED, TEXT, TILE

REC_DIR = Path(__file__).with_name("recordings")
VIDEO_EXT = {".mp4", ".h264", ".h265"}
_HH = re.compile(r"^\d{2}$")


def _play(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], start_new_session=True)


def _size_text(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n / 1000:.0f} KB"


def _clip_when(path: Path) -> str:
    stem = path.stem.replace("_", "-")
    parts = stem.split("-")
    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
        return f"{parts[0]}:{parts[1]}:{parts[2]}"
    if _HH.match(stem) and _HH.match(path.parent.name):
        return f"{path.parent.name}:{stem}:00"
    if _HH.match(stem):
        return f"{stem}:00:00"
    return path.stem


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
        for files in dates.values():
            files.sort(key=lambda p: _clip_when(p))
    return data


class ClipsFrame(tk.Frame):
    def __init__(self, master, on_back):
        super().__init__(master, bg=BG)
        self._on_back = on_back
        self._data: dict[str, dict[str, list[Path]]] = {}
        self._camera = ""
        self._date = ""
        self._hour = ""
        self._hour_files: dict[str, list[Path]] = {}
        self._hour_btns: dict[str, tk.Button] = {}
        self._date_btns: dict[str, tk.Button] = {}

        top = tk.Frame(self, bg=CARD, padx=16, pady=12)
        top.pack(fill="x")
        tk.Button(top, text="← Home", bg="#334155", fg="white", relief="flat", command=self._on_back).pack(side="left")
        tk.Label(top, text="  Playback", fg="white", bg=CARD, font=("Segoe UI", 16, "bold")).pack(side="left", padx=(12, 0))
        tk.Button(top, text="Refresh", bg="#334155", fg="white", relief="flat", command=self.reload).pack(side="right")

        pick = tk.Frame(self, bg=CARD, padx=16, pady=12)
        pick.pack(fill="x")
        tk.Label(pick, text="Camera", fg=MUTED, bg=CARD).pack(side="left")
        self._cam_var = tk.StringVar()
        self._cam_box = ttk.Combobox(pick, textvariable=self._cam_var, state="readonly", width=28, font=("Segoe UI", 11))
        self._cam_box.pack(side="left", padx=(8, 24))
        self._cam_box.bind("<<ComboboxSelected>>", lambda _e: self._pick_camera())
        tk.Label(pick, text="Day", fg=MUTED, bg=CARD).pack(side="left")
        self._dates_row = tk.Frame(pick, bg=CARD)
        self._dates_row.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.status = tk.Label(self, text="", anchor="w", fg=TEXT, bg="#1e2937", padx=16, pady=8)
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

    def reload(self) -> None:
        keep_cam, keep_date, keep_hour = self._camera, self._date, self._hour
        try:
            rename_legacy_names(REC_DIR)
        except Exception:
            pass
        self._data = _index(REC_DIR)
        cameras = sorted(self._data)
        self._cam_box["values"] = cameras
        if not cameras:
            self._cam_var.set("")
            self._clear_dates()
            self._clear_hours()
            self._clear_mins()
            self.status.configure(text="No recordings yet. Turn Rec ON, then come back here.")
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
            label = f"  {hour}:00     {len(files)} clip(s)     {_size_text(size)}  "
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
            tk.Button(
                row,
                text="Play",
                bg=ACCENT,
                fg="white",
                relief="flat",
                command=lambda h=hour: self._play_path(self._hour_files[h][0]),
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
            ready = path.suffix.lower() == ".mp4"
            title = f"{hour}:{when}" if len(when) == 2 else _clip_when(path)
            if not ready:
                title += " …"
            b = tk.Button(
                grid,
                text=title,
                width=8,
                bg="#1e2937" if ready else "#7f1d1d",
                fg="white",
                relief="flat",
                command=lambda p=path: self._play_path(p),
            )
            b.grid(row=i // 6, column=i % 6, padx=4, pady=4, sticky="ew")
        if not files:
            tk.Label(self._mins, text="No clips in this hour.", fg=MUTED, bg=TILE).pack(anchor="w")

    def _clear_hours(self) -> None:
        for child in self._hours_inner.winfo_children():
            child.destroy()
        self._hour_btns.clear()
        self._hour_files = {}

    def _clear_mins(self) -> None:
        for child in self._mins.winfo_children():
            child.destroy()

    def _play_path(self, path: Path) -> None:
        if not path.is_file():
            messagebox.showinfo("Playback", "That clip is not on disk yet.")
            return
        try:
            _play(path)
        except Exception as exc:
            messagebox.showerror("Play failed", str(exc))
