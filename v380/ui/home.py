"""Home mosaic: full-window live tiles and camera management."""

from __future__ import annotations

import io
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from v380.record.auto_record import RecordSupervisor
from v380.store.camera_store import Camera, CameraStore
from v380.client.preview import PreviewManager
from v380.ui.profile import open_profile
from v380.ui.theme import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    FONT_BODY,
    FONT_BTN,
    FONT_HEAD,
    FONT_SMALL,
    FONT_SUB,
    GREEN,
    MUTED,
    ORANGE,
    RED,
    TEXT,
    TILE,
    danger_button,
    entry,
    ghost_button,
    primary_button,
    status_bar,
)


def _fit_contain(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    if box_w < 8 or box_h < 8:
        return img
    img.thumbnail((box_w, box_h), Image.Resampling.BILINEAR)
    if img.size == (box_w, box_h):
        return img
    canvas = Image.new("RGB", (box_w, box_h), (10, 15, 20))
    x = (box_w - img.width) // 2
    y = (box_h - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


def _friendly_probe_error(probe: dict) -> str:
    err = str(probe.get("error") or "").strip()
    low = err.lower()
    if "invalid password" in low:
        return "Invalid password"
    if "invalid username" in low:
        return "Invalid username"
    if "invalid device" in low:
        return "Invalid device ID"
    if "rtsp" in low or str(probe.get("brand") or "").lower() == "ezviz":
        if not probe.get("reachable"):
            return f"EZVIZ offline — {err or 'unreachable on :554'}"
        return f"EZVIZ RTSP failed — {err or 'enable LAN Live View / RTSP in EZVIZ app'}"
    if not probe.get("reachable"):
        return f"Camera offline — {err or 'unreachable'}"
    return f"Login failed — {err or 'unknown error'}"


class HomeFrame(tk.Frame):
    def __init__(
        self,
        master,
        store: CameraStore,
        recorders: RecordSupervisor,
        on_add,
        on_view,
        on_edit,
        on_clips=None,
        previews=None,
        api=None,
    ):
        super().__init__(master, bg=BG)
        self.store = store
        self.api = api or getattr(store, "api", None)
        self._recorders = recorders
        self._on_add = on_add
        self._on_view = on_view
        self._on_edit = on_edit
        self._on_clips = on_clips
        self._tiles: dict[int, _Tile] = {}
        self._previews = previews if previews is not None else PreviewManager(store, self._push_state)
        self._drain_on = True
        self._paused = False

        top = tk.Frame(self, bg=CARD, padx=20, pady=14, highlightthickness=1, highlightbackground=BORDER)
        top.pack(fill="x")
        brand = tk.Frame(top, bg=CARD)
        brand.pack(side="left")
        tk.Label(brand, text="V380 Studio", fg=TEXT, bg=CARD, font=FONT_HEAD).pack(anchor="w")
        tk.Label(brand, text="V380 + EZVIZ cameras", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")

        primary_button(top, "Add camera", self._on_add).pack(side="right")
        if self._on_clips is not None:
            ghost_button(top, "Recordings", self._on_clips).pack(side="right", padx=(0, 8))
        ghost_button(top, "Profile", self._open_profile).pack(side="right", padx=(0, 8))
        ghost_button(top, "Check status", self._ping_all).pack(side="right", padx=(0, 8))
        ghost_button(top, "Refresh", self.reload).pack(side="right", padx=(0, 8))

        self.status = status_bar(self)
        self.status.pack(fill="x")

        self._stage = tk.Frame(self, bg=BG)
        self._stage.pack(fill="both", expand=True, padx=14, pady=14)
        self._stage.grid_rowconfigure(0, weight=1)
        self._stage.grid_columnconfigure(0, weight=1)

        self.reload()
        self.after(16, self._drain_previews)
        self.after(1000, self._poll_store)
        self.after(700, lambda: self._ping_all(quiet=True))
        self.after(60_000, self._auto_ping_loop)

    def _auto_ping_loop(self) -> None:
        if not self._drain_on:
            return
        if not self._paused and self._tiles:
            self._ping_all(quiet=True)
        self.after(60_000, self._auto_ping_loop)

    def _open_profile(self) -> None:
        open_profile(self, self.api, self.status)

    def reload(self) -> None:
        for child in self._stage.winfo_children():
            child.destroy()
        self._tiles.clear()
        cameras = self.store.list()
        if not cameras:
            self.status.configure(text="No cameras yet — add one, enter the password, then Connect.")
            empty = tk.Frame(self._stage, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            empty.place(relx=0.5, rely=0.5, anchor="center")
            tk.Label(empty, text="No cameras", fg=TEXT, bg=CARD, font=FONT_HEAD).pack(padx=40, pady=(28, 6))
            tk.Label(
                empty,
                text="Discover a camera on the LAN, type the password,\nand Connect. It saves only if login works.",
                fg=MUTED,
                bg=CARD,
                font=FONT_BODY,
                justify="center",
            ).pack(padx=40, pady=(0, 18))
            primary_button(empty, "Add camera", self._on_add).pack(pady=(0, 28))
            self._previews.stop()
            return
        self.status.configure(text=self._service_line(cameras))
        self._layout_tiles(cameras)
        self._previews.start(cameras)
        self.after(400, lambda: self._ping_all(quiet=True))

    def _layout_tiles(self, cameras: list[Camera]) -> None:
        n = len(cameras)
        tiles = [
            _Tile(self._stage, cam, self._on_view, self._on_edit, self._delete, self._toggle_rec, self._ping)
            for cam in cameras
        ]
        for cam, tile in zip(cameras, tiles):
            self._tiles[cam.id] = tile

        for r in range(4):
            self._stage.grid_rowconfigure(r, weight=0)
        for c in range(4):
            self._stage.grid_columnconfigure(c, weight=0)

        if n == 1:
            self._stage.grid_rowconfigure(0, weight=1)
            self._stage.grid_columnconfigure(0, weight=1)
            tiles[0].grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
            return

        if n == 2:
            self._stage.grid_rowconfigure(0, weight=1)
            self._stage.grid_columnconfigure(0, weight=1)
            self._stage.grid_columnconfigure(1, weight=1)
            tiles[0].grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
            tiles[1].grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
            return

        if n == 3:
            self._stage.grid_rowconfigure(0, weight=1)
            self._stage.grid_rowconfigure(1, weight=1)
            self._stage.grid_columnconfigure(0, weight=1)
            self._stage.grid_columnconfigure(1, weight=1)
            tiles[0].grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
            tiles[1].grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
            tiles[2].grid(row=0, column=1, rowspan=2, sticky="nsew", padx=6, pady=6)
            return

        cols = 2 if n <= 4 else 3 if n <= 9 else 4
        rows = (n + cols - 1) // cols
        for r in range(rows):
            self._stage.grid_rowconfigure(r, weight=1)
        for c in range(cols):
            self._stage.grid_columnconfigure(c, weight=1)
        for i, tile in enumerate(tiles):
            tile.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)

    def pause(self) -> None:
        self._paused = True
        self._previews.stop()

    def resume(self) -> None:
        self._paused = False
        self.reload()

    def shutdown(self) -> None:
        self._drain_on = False
        self._previews.stop()

    def _drain_previews(self) -> None:
        if not self._drain_on:
            return
        try:
            for cam_id, tile in list(self._tiles.items()):
                writing = self._recorders.is_recording(cam_id)
                tile.set_rec(tile.cam.auto_record, writing)
                tile.set_service(*self._recorders.camera_status(cam_id, tile.cam.auto_record))
                jpeg = self._previews.take_latest(cam_id)
                if jpeg is not None:
                    tile.set_jpeg(jpeg)
                else:
                    tile.paint_if_dirty()
            self._refresh_service_bar()
            self._recorders.watch()
        except Exception:
            pass
        if self._drain_on:
            self.after(50, self._drain_previews)

    def _poll_store(self) -> None:
        if not self._drain_on:
            return
        if not self._paused:
            try:
                self._apply_store()
            except Exception:
                pass
        self.after(1000, self._poll_store)

    def _apply_store(self) -> None:
        cameras = self.store.list()
        if {c.id for c in cameras} != set(self._tiles):
            self.reload()
            return
        restart_preview = False
        rec_changed = False
        for cam in cameras:
            tile = self._tiles.get(cam.id)
            if tile is None:
                continue
            old = tile.cam
            if old.auto_record != cam.auto_record or old.record_chunk != cam.record_chunk:
                rec_changed = True
            if (
                old.ip,
                old.port,
                old.username,
                old.password,
                old.source,
                old.quality,
                getattr(old, "brand", "v380"),
                getattr(old, "rtsp_url", ""),
            ) != (
                cam.ip,
                cam.port,
                cam.username,
                cam.password,
                cam.source,
                cam.quality,
                getattr(cam, "brand", "v380"),
                getattr(cam, "rtsp_url", ""),
            ):
                restart_preview = True
            tile.apply_cam(cam)
        if rec_changed:
            self._recorders.sync(cameras)
        if restart_preview:
            self._previews.start(cameras)
        self._refresh_service_bar()

    def _service_line(self, cameras: list[Camera] | None = None) -> str:
        cams = cameras if cameras is not None else [t.cam for t in self._tiles.values()]
        live = f"{len(cams)} camera(s)"
        try:
            svc = self._recorders.service_summary(cams)
        except Exception:
            svc = ""
        return f"{live}  ·  {svc}" if svc else live

    def _refresh_service_bar(self) -> None:
        text = self._service_line()
        cur = self.status.cget("text")
        if cur.startswith("Ping ") or cur.startswith("Checking"):
            return
        if cur != text:
            self.status.configure(text=text)

    def _toggle_rec(self, cam: Camera) -> None:
        cam.auto_record = not cam.auto_record
        self.store.set_auto_record(cam.id, cam.auto_record)
        self._recorders.sync(self.store.list())
        tile = self._tiles.get(cam.id)
        if tile is not None:
            tile.cam = cam
            writing = self._recorders.is_recording(cam.id)
            tile.set_rec(cam.auto_record, writing)
            tile.set_service(*self._recorders.camera_status(cam.id, cam.auto_record))
        self._refresh_service_bar()

    def _delete(self, cam: Camera) -> None:
        if not messagebox.askyesno("Delete camera", f"Remove {cam.name} ({cam.device_id})?"):
            return
        self.store.delete(cam.id)
        self._recorders.sync(self.store.list())
        self.reload()

    def _ping(self, cam: Camera, *, quiet: bool = False) -> None:
        ping_fn = getattr(self.store, "ping", None)
        if not callable(ping_fn):
            if not quiet:
                messagebox.showinfo("Status", "Camera check needs the studio server connection.")
            return
        tile = self._tiles.get(cam.id)
        if tile is not None and not quiet:
            tile.set_state("Checking…")
        cam_id = cam.id
        name = cam.name

        def work() -> None:
            try:
                out = ping_fn(cam_id)
                ms = out.get("ms")
                if out.get("online"):
                    text = f"Online  ·  {ms} ms"
                    ok = True
                elif out.get("reachable"):
                    text = f"Login failed  ·  {_friendly_probe_error(out)}"
                    ok = False
                else:
                    text = f"Offline  ·  {_friendly_probe_error(out)}"
                    ok = False
            except Exception as exc:
                text = f"Offline  ·  {exc}"
                ok = False
            try:
                self.after(0, lambda: self._show_ping(cam_id, name, text, ok, quiet))
            except Exception:
                return

        threading.Thread(target=work, daemon=True, name=f"ping-{cam_id}").start()

    def _ping_all(self, quiet: bool = False) -> None:
        cams = [t.cam for t in self._tiles.values()]
        if not cams:
            if not quiet:
                messagebox.showinfo("Status", "No cameras to check.")
            return
        if not quiet:
            self.status.configure(text=f"Checking {len(cams)} camera(s)…")
        for cam in cams:
            self._ping(cam, quiet=quiet)

    def _show_ping(self, camera_id: int, name: str, text: str, ok: bool, quiet: bool) -> None:
        tile = self._tiles.get(camera_id)
        if tile is not None:
            tile.set_state(text)
        if not quiet:
            mark = "OK" if ok else "FAIL"
            self.status.configure(text=f"{name}: {mark}  ·  {text}")

    def _push_state(self, camera_id: int, text: str) -> None:
        if not self._drain_on:
            return
        try:
            if not self.winfo_exists():
                return
            self.after(0, lambda: self._show_state(camera_id, text))
        except Exception:
            return

    def _show_state(self, camera_id: int, text: str) -> None:
        tile = self._tiles.get(camera_id)
        if tile is None:
            return
        # Don't overwrite a fresh Offline/Online check with "Starting…"
        cur = (tile._state.cget("text") or "").lower()
        if text == "Starting…" and (cur.startswith("offline") or cur.startswith("online") or "login failed" in cur):
            return
        tile.set_state(text)


class _Tile(tk.Frame):
    def __init__(self, master, cam: Camera, on_view, on_edit, on_delete, on_rec, on_ping):
        super().__init__(master, bg=CARD, padx=10, pady=10, highlightthickness=1, highlightbackground=BORDER)
        self.cam = cam
        self._jpeg: bytes | None = None
        self._jpeg_id = 0
        self._photo: ImageTk.PhotoImage | None = None
        self._dirty = False
        self._box = (0, 0)

        foot = tk.Frame(self, bg=CARD)
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        head = tk.Frame(foot, bg=CARD)
        head.pack(fill="x")
        self._name = tk.Label(head, text=cam.name, fg=TEXT, bg=CARD, font=(FONT_BTN[0], 12, "bold"))
        self._name.pack(side="left", anchor="w")
        self._badge = tk.Label(head, text="…", fg=MUTED, bg=CARD, font=FONT_SMALL)
        self._badge.pack(side="right")

        self._meta = tk.Label(
            foot,
            text=f"{cam.brand_name}  ·  {cam.device_id}  ·  {cam.ip or 'cloud'}  ·  {cam.quality_name}",
            fg=MUTED,
            bg=CARD,
            font=FONT_SMALL,
        )
        self._meta.pack(anchor="w", pady=(2, 0))
        self._state = tk.Label(foot, text="Checking…", fg=MUTED, bg=CARD, font=FONT_BODY)
        self._state.pack(anchor="w")
        self._svc = tk.Label(foot, text="Service: off", fg=MUTED, bg=CARD, font=FONT_SMALL)
        self._svc.pack(anchor="w")
        self._svc_text = ""
        btns = tk.Frame(foot, bg=CARD)
        btns.pack(fill="x", pady=(8, 0))
        primary_button(btns, "View", lambda: on_view(self.cam)).pack(side="left", padx=(0, 6))
        ghost_button(btns, "Edit", lambda: on_edit(self.cam)).pack(side="left", padx=(0, 6))
        ghost_button(btns, "Check", lambda: on_ping(self.cam)).pack(side="left", padx=(0, 6))
        self._rec_btn = ghost_button(btns, "Rec OFF", lambda: on_rec(self.cam))
        self._rec_btn.pack(side="left", padx=(0, 6))
        danger_button(btns, "Delete", lambda: on_delete(self.cam)).pack(side="left")
        self.set_rec(cam.auto_record, False)
        self.set_service("Service: off", "off")

        self._view = tk.Canvas(self, bg=TILE, highlightthickness=0, cursor="hand2")
        self._view.pack(side="top", fill="both", expand=True)
        self._view.bind("<Button-1>", lambda _e: on_view(self.cam))
        self._view.bind("<Configure>", lambda _e: self._mark_dirty())
        self._placeholder()

    def _placeholder(self) -> None:
        self._view.delete("all")
        w = max(self._view.winfo_width(), 40)
        h = max(self._view.winfo_height(), 40)
        self._view.create_text(w // 2, h // 2, text="Waiting for live video…", fill="#5c6b7a", font=FONT_SUB)

    def _mark_dirty(self) -> None:
        self._dirty = True

    def set_jpeg(self, jpeg: bytes) -> None:
        self._jpeg = jpeg
        self._dirty = True
        self._paint()

    def paint_if_dirty(self) -> None:
        if self._dirty:
            self._paint()

    def _paint(self) -> None:
        w = self._view.winfo_width()
        h = self._view.winfo_height()
        if w < 16 or h < 16:
            return
        if not self._jpeg:
            self._placeholder()
            self._dirty = False
            self._box = (w, h)
            return
        jpeg_id = id(self._jpeg)
        if self._photo is not None and self._box == (w, h) and self._jpeg_id == jpeg_id:
            self._view.delete("all")
            self._view.create_image(w // 2, h // 2, image=self._photo)
            self._draw_rec_badge(w)
            self._dirty = False
            return
        try:
            img = Image.open(io.BytesIO(self._jpeg))
            fitted = _fit_contain(img, w, h)
            self._photo = ImageTk.PhotoImage(fitted)
        except Exception:
            self._dirty = False
            return
        self._jpeg_id = jpeg_id
        self._view.delete("all")
        self._view.create_image(w // 2, h // 2, image=self._photo)
        self._draw_rec_badge(w)
        self._box = (w, h)
        self._dirty = False

    def _draw_rec_badge(self, w: int) -> None:
        if not self.cam.auto_record:
            return
        label = "REC" if self._rec_btn.cget("text").startswith("REC ") else "REC…"
        self._view.create_rectangle(10, 10, 78, 34, fill="#7f1d1d", outline="")
        self._view.create_text(44, 22, text=label, fill="white", font=FONT_BTN)

    def apply_cam(self, cam: Camera) -> None:
        self.cam = cam
        self._name.configure(text=cam.name)
        self._meta.configure(text=f"{cam.brand_name}  ·  {cam.device_id}  ·  {cam.ip or 'cloud'}  ·  {cam.quality_name}")

    def set_state(self, text: str) -> None:
        low = text.lower()
        if text == "Live" or low.startswith("online"):
            color = GREEN
            badge = "ONLINE" if low.startswith("online") else "LIVE"
        elif low.startswith("offline") or "login failed" in low:
            color = RED
            badge = "OFFLINE" if low.startswith("offline") else "AUTH"
        elif low.startswith("checking") or low.startswith("pinging") or low.startswith("reachable"):
            color = ORANGE
            badge = "…"
        else:
            color = MUTED
            badge = "…"
        self._state.configure(text=text, fg=color)
        self._badge.configure(text=badge, fg=color)

    def set_rec(self, enabled: bool, writing: bool) -> None:
        unit = "1m" if self.cam.record_chunk == "minute" else "1h"
        if enabled and writing:
            text, bg = f"REC {unit}", RED
        elif enabled:
            text, bg = f"REC… {unit}", "#7f1d1d"
        else:
            text, bg = "Rec OFF", "#243040"
        if self._rec_btn.cget("text") == text:
            return
        self._rec_btn.configure(text=text, bg=bg)
        self._dirty = True

    def set_service(self, text: str, level: str) -> None:
        if text == self._svc_text:
            return
        self._svc_text = text
        color = {"ok": GREEN, "wait": ORANGE, "down": RED}.get(level, MUTED)
        self._svc.configure(text=text, fg=color)


class EditDialog(tk.Toplevel):
    def __init__(self, master, cam: Camera, on_save, api=None):
        super().__init__(master)
        self.title(f"Edit  ·  {cam.name}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._cam = cam
        self._on_save = on_save
        self._api = api
        self.transient(master)
        self.grab_set()

        wrap = tk.Frame(self, bg=CARD, padx=24, pady=20, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(padx=16, pady=16)

        tk.Label(wrap, text="Camera settings", fg=TEXT, bg=CARD, font=FONT_HEAD).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        def row(r, label, value, show=""):
            tk.Label(wrap, text=label, fg=MUTED, bg=CARD, font=FONT_SMALL).grid(row=r, column=0, sticky="w", pady=5)
            e = entry(wrap, width=30, show=show)
            e.insert(0, value)
            e.grid(row=r, column=1, pady=5, padx=(12, 0), ipady=5)
            return e

        tk.Label(wrap, text="Brand", fg=MUTED, bg=CARD, font=FONT_SMALL).grid(row=1, column=0, sticky="w", pady=5)
        self.brand_e = ttk.Combobox(wrap, values=["V380", "EZVIZ"], width=28, state="readonly")
        self.brand_e.set(cam.brand_name)
        self.brand_e.grid(row=1, column=1, pady=5, padx=(12, 0))
        self.brand_e.bind("<<ComboboxSelected>>", lambda _e: self._on_brand())

        self.name_e = row(2, "Name", cam.name)
        self.ip_e = row(3, "IP", cam.ip)
        self.port_e = row(4, "Port", str(cam.port))
        self.id_e = row(5, "Device ID", cam.device_id)
        self.user_e = row(6, "Username", cam.username or ("admin" if cam.is_ezviz else ""))
        self.pass_e = row(7, "Password", cam.password, "*")
        self._pass_label = wrap.grid_slaves(row=7, column=0)[0]
        self.rtsp_e = row(8, "RTSP URL", getattr(cam, "rtsp_url", "") or "")
        tk.Label(wrap, text="Source", fg=MUTED, bg=CARD, font=FONT_SMALL).grid(row=9, column=0, sticky="w", pady=5)
        self.source_e = ttk.Combobox(wrap, values=["LAN", "Cloud"], width=28, state="readonly")
        self.source_e.set(cam.source_name)
        self.source_e.grid(row=9, column=1, pady=5, padx=(12, 0))
        tk.Label(wrap, text="Quality", fg=MUTED, bg=CARD, font=FONT_SMALL).grid(row=10, column=0, sticky="w", pady=5)
        self.quality_e = ttk.Combobox(wrap, values=["HD", "SD"], width=28, state="readonly")
        self.quality_e.set(cam.quality_name)
        self.quality_e.grid(row=10, column=1, pady=5, padx=(12, 0))
        self.rec_var = tk.IntVar(value=1 if cam.auto_record else 0)
        tk.Checkbutton(
            wrap,
            text="Auto record (keeps running after you close Studio)",
            variable=self.rec_var,
            fg=TEXT,
            bg=CARD,
            selectcolor=TILE,
            activebackground=CARD,
            activeforeground=TEXT,
            font=FONT_SMALL,
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(10, 0))
        tk.Label(wrap, text="Chunk size", fg=MUTED, bg=CARD, font=FONT_SMALL).grid(row=12, column=0, sticky="w", pady=5)
        self.chunk_e = ttk.Combobox(wrap, values=["Every hour", "Every minute"], width=28, state="readonly")
        self.chunk_e.set("Every minute" if cam.record_chunk == "minute" else "Every hour")
        self.chunk_e.grid(row=12, column=1, pady=5, padx=(12, 0))

        self._hint = tk.Label(wrap, text="", fg=MUTED, bg=CARD, font=FONT_SMALL)
        self._hint.grid(row=13, column=0, columnspan=2, sticky="w", pady=(10, 0))

        actions = tk.Frame(wrap, bg=CARD)
        actions.grid(row=14, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ghost_button(actions, "Cancel", self.destroy).pack(side="right", padx=(8, 0))
        self._save_btn = primary_button(actions, "Save", self._save)
        self._save_btn.pack(side="right")
        self._on_brand()

    def _brand(self) -> str:
        return "ezviz" if self.brand_e.get() == "EZVIZ" else "v380"

    def _on_brand(self) -> None:
        ez = self._brand() == "ezviz"
        self._pass_label.configure(text="Verify code" if ez else "Password")
        if ez:
            if not self.port_e.get().strip() or self.port_e.get().strip() == "8800":
                self.port_e.delete(0, "end")
                self.port_e.insert(0, "554")
            if not self.user_e.get().strip():
                self.user_e.insert(0, "admin")
            self.source_e.set("LAN")
            self._hint.configure(
                text="EZVIZ: enable LAN Live View / RTSP in the EZVIZ app. Password = 6-letter verification code.",
                fg=MUTED,
            )
        else:
            if self.port_e.get().strip() == "554":
                self.port_e.delete(0, "end")
                self.port_e.insert(0, "8800")
            self._hint.configure(text="", fg=MUTED)

    def _save(self) -> None:
        brand = self._brand()
        try:
            port = int(self.port_e.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Port must be a number.")
            return
        device_id = self.id_e.get().strip()
        if brand == "v380":
            try:
                int(device_id)
            except ValueError:
                messagebox.showerror("Invalid input", "Device ID must be a number for V380.")
                return
        elif not device_id:
            from v380.client.ezviz_rtsp import synthetic_device_id

            device_id = synthetic_device_id(self.ip_e.get().strip(), self.rtsp_e.get().strip())
        cam = Camera(
            id=self._cam.id,
            name=self.name_e.get().strip() or device_id,
            device_id=device_id,
            mac=self._cam.mac,
            ip=self.ip_e.get().strip(),
            port=port,
            username=self.user_e.get().strip() or ("admin" if brand == "ezviz" else ""),
            password=self.pass_e.get(),
            source="cloud" if self.source_e.get() == "Cloud" and brand != "ezviz" else "lan",
            quality=1 if self.quality_e.get() == "HD" else 0,
            auto_record=bool(self.rec_var.get()),
            record_chunk="minute" if self.chunk_e.get() == "Every minute" else "hour",
            created_at=self._cam.created_at,
            updated_at=self._cam.updated_at,
            brand=brand,
            rtsp_url=self.rtsp_e.get().strip(),
        )
        if not cam.username or not cam.password:
            messagebox.showerror(
                "Missing fields",
                "Username and verification code are required." if brand == "ezviz" else "Username and password are required.",
            )
            return
        if brand == "ezviz" and not cam.ip and not cam.rtsp_url:
            messagebox.showerror("Missing fields", "IP or full RTSP URL is required for EZVIZ.")
            return

        if self._api is None:
            self._on_save(cam)
            self.destroy()
            return

        self._save_btn.configure(state="disabled", text="Checking…")
        self._hint.configure(text="Verifying camera…", fg=ORANGE)

        def work() -> None:
            try:
                probe = self._api.probe_camera(
                    {
                        "name": cam.name,
                        "device_id": cam.device_id,
                        "mac": cam.mac,
                        "ip": cam.ip,
                        "port": cam.port,
                        "username": cam.username,
                        "password": cam.password,
                        "source": cam.source,
                        "quality": cam.quality,
                        "brand": cam.brand,
                        "rtsp_url": cam.rtsp_url,
                    }
                )
                if not probe.get("online"):
                    err = _friendly_probe_error(probe)
                    self.after(0, lambda: self._probe_fail(err))
                    return
                self.after(0, lambda: self._probe_ok(cam))
            except Exception as exc:
                self.after(0, lambda: self._probe_fail(str(exc)))

        threading.Thread(target=work, daemon=True, name="edit-probe").start()

    def _probe_fail(self, err: str) -> None:
        self._save_btn.configure(state="normal", text="Save")
        self._hint.configure(text=err, fg=RED)
        messagebox.showerror("Camera", err)

    def _probe_ok(self, cam: Camera) -> None:
        self._on_save(cam)
        self.destroy()
