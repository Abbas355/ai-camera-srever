"""Home mosaic: full-window live tiles and camera management."""

from __future__ import annotations

import io
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from v380.record.auto_record import RecordSupervisor
from v380.store.camera_store import Camera, CameraStore
from v380.client.preview import PreviewManager
from v380.ui.theme import ACCENT, BG, CARD, GREEN, MUTED, ORANGE, RED, TEXT, TILE


def _fit_contain(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    if box_w < 8 or box_h < 8:
        return img
    img.thumbnail((box_w, box_h), Image.Resampling.BILINEAR)
    if img.size == (box_w, box_h):
        return img
    canvas = Image.new("RGB", (box_w, box_h), (2, 6, 23))
    x = (box_w - img.width) // 2
    y = (box_h - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas


class HomeFrame(tk.Frame):
    def __init__(self, master, store: CameraStore, recorders: RecordSupervisor, on_add, on_view, on_edit, on_clips=None, previews=None):
        super().__init__(master, bg=BG)
        self.store = store
        self._recorders = recorders
        self._on_add = on_add
        self._on_view = on_view
        self._on_edit = on_edit
        self._on_clips = on_clips
        self._tiles: dict[int, _Tile] = {}
        self._previews = previews if previews is not None else PreviewManager(store, self._push_state)
        self._drain_on = True
        self._paused = False

        top = tk.Frame(self, bg=CARD, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="V380 Studio", fg="white", bg=CARD, font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(top, text="  Home  ·  live cameras", fg=MUTED, bg=CARD).pack(side="left", padx=(8, 0))
        tk.Button(top, text="Add camera", bg=ACCENT, fg="white", relief="flat", command=self._on_add).pack(side="right")
        if self._on_clips is not None:
            tk.Button(top, text="Recordings", bg="#334155", fg="white", relief="flat", command=self._on_clips).pack(
                side="right", padx=(0, 8)
            )
        tk.Button(top, text="Refresh", bg="#334155", fg="white", relief="flat", command=self.reload).pack(side="right", padx=(0, 8))

        self.status = tk.Label(self, text="", anchor="w", fg=TEXT, bg="#1e2937", padx=16, pady=6)
        self.status.pack(fill="x")

        self._stage = tk.Frame(self, bg=BG)
        self._stage.pack(fill="both", expand=True, padx=10, pady=10)
        self._stage.grid_rowconfigure(0, weight=1)
        self._stage.grid_columnconfigure(0, weight=1)

        self.reload()
        self.after(16, self._drain_previews)
        self.after(1000, self._poll_store)

    def reload(self) -> None:
        for child in self._stage.winfo_children():
            child.destroy()
        self._tiles.clear()
        cameras = self.store.list()
        if not cameras:
            self.status.configure(text="No cameras yet. Click Add camera — Discover, password, Connect. It saves automatically.")
            tk.Label(
                self._stage,
                text="Add your first camera to see a full-screen live preview here.",
                fg=MUTED,
                bg=BG,
                font=("Segoe UI", 13),
            ).pack(expand=True)
            self._previews.stop()
            return
        self.status.configure(text=self._service_line(cameras))
        self._layout_tiles(cameras)
        self._previews.start(cameras)

    def _layout_tiles(self, cameras: list[Camera]) -> None:
        n = len(cameras)
        tiles = [
            _Tile(self._stage, cam, self._on_view, self._on_edit, self._delete, self._toggle_rec)
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
            # Two stacked on the left, one tall tile on the right
            self._stage.grid_rowconfigure(0, weight=1)
            self._stage.grid_rowconfigure(1, weight=1)
            self._stage.grid_columnconfigure(0, weight=1)
            self._stage.grid_columnconfigure(1, weight=1)
            tiles[0].grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
            tiles[1].grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
            tiles[2].grid(row=0, column=1, rowspan=2, sticky="nsew", padx=6, pady=6)
            return

        cols = 2 if n == 4 else 3
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
            ) != (cam.ip, cam.port, cam.username, cam.password, cam.source, cam.quality):
                restart_preview = True
            tile.apply_cam(cam)
        if rec_changed:
            self._recorders.sync(cameras)
        if restart_preview:
            self._previews.start(cameras)
        self._refresh_service_bar()

    def _service_line(self, cameras: list[Camera] | None = None) -> str:
        cams = cameras if cameras is not None else [t.cam for t in self._tiles.values()]
        live = f"{len(cams)} camera(s)  ·  live mosaic"
        extra = self._recorders.service_summary(cams)
        return f"{live}  ·  {extra}"

    def _refresh_service_bar(self) -> None:
        if not self._tiles:
            return
        text = self._service_line()
        if self.status.cget("text") != text:
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
        if not messagebox.askyesno("Delete camera", f"Remove {cam.name} ({cam.device_id}) from the server?"):
            return
        self.store.delete(cam.id)
        self._recorders.sync(self.store.list())
        self.reload()

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
        if tile is not None:
            tile.set_state(text)


class _Tile(tk.Frame):
    def __init__(self, master, cam: Camera, on_view, on_edit, on_delete, on_rec):
        super().__init__(master, bg=CARD, padx=8, pady=8)
        self.cam = cam
        self._jpeg: bytes | None = None
        self._jpeg_id = 0
        self._photo: ImageTk.PhotoImage | None = None
        self._dirty = False
        self._box = (0, 0)

        foot = tk.Frame(self, bg=CARD)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        self._name = tk.Label(foot, text=cam.name, fg=TEXT, bg=CARD, font=("Segoe UI", 12, "bold"))
        self._name.pack(anchor="w")
        self._meta = tk.Label(
            foot,
            text=f"{cam.device_id}  ·  {cam.ip or 'cloud'}  ·  {cam.quality_name}",
            fg=MUTED,
            bg=CARD,
        )
        self._meta.pack(anchor="w")
        self._state = tk.Label(foot, text="Starting…", fg=GREEN, bg=CARD)
        self._state.pack(anchor="w")
        self._svc = tk.Label(foot, text="Service: off", fg=MUTED, bg=CARD, font=("Segoe UI", 10, "bold"))
        self._svc.pack(anchor="w")
        self._svc_text = ""
        btns = tk.Frame(foot, bg=CARD)
        btns.pack(fill="x", pady=(6, 0))
        tk.Button(btns, text="View", bg=ACCENT, fg="white", relief="flat", width=8, command=lambda: on_view(self.cam)).pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Edit", bg="#334155", fg="white", relief="flat", width=8, command=lambda: on_edit(self.cam)).pack(side="left", padx=(0, 6))
        self._rec_btn = tk.Button(btns, text="Rec OFF", bg="#334155", fg="white", relief="flat", width=8, command=lambda: on_rec(self.cam))
        self._rec_btn.pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Delete", bg=RED, fg="white", relief="flat", width=8, command=lambda: on_delete(self.cam)).pack(side="left")
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
        self._view.create_text(w // 2, h // 2, text="Waiting for live video…", fill="#64748b", font=("Segoe UI", 12))

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
        self._view.create_text(44, 22, text=label, fill="white", font=("Segoe UI", 10, "bold"))

    def apply_cam(self, cam: Camera) -> None:
        self.cam = cam
        self._name.configure(text=cam.name)
        self._meta.configure(text=f"{cam.device_id}  ·  {cam.ip or 'cloud'}  ·  {cam.quality_name}")

    def set_state(self, text: str) -> None:
        color = GREEN if text == "Live" else MUTED
        self._state.configure(text=text, fg=color)

    def set_rec(self, enabled: bool, writing: bool) -> None:
        unit = "1m" if self.cam.record_chunk == "minute" else "1h"
        if enabled and writing:
            text, bg = f"REC {unit}", RED
        elif enabled:
            text, bg = f"REC… {unit}", "#7f1d1d"
        else:
            text, bg = "Rec OFF", "#334155"
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
    def __init__(self, master, cam: Camera, on_save):
        super().__init__(master)
        self.title(f"Edit  {cam.name}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._cam = cam
        self._on_save = on_save
        self.transient(master)
        self.grab_set()

        form = tk.Frame(self, bg=BG, padx=16, pady=14)
        form.pack()

        def row(r, label, value, show=""):
            tk.Label(form, text=label, fg=MUTED, bg=BG).grid(row=r, column=0, sticky="w", pady=4)
            e = tk.Entry(form, width=28, show=show, bg="#1f2937", fg=TEXT, insertbackground=TEXT, relief="flat")
            e.insert(0, value)
            e.grid(row=r, column=1, pady=4, padx=(8, 0))
            return e

        self.name_e = row(0, "Name", cam.name)
        self.ip_e = row(1, "IP", cam.ip)
        self.port_e = row(2, "Port", str(cam.port))
        self.id_e = row(3, "Device ID", cam.device_id)
        self.user_e = row(4, "Username", cam.username)
        self.pass_e = row(5, "Password", cam.password, "*")
        tk.Label(form, text="Source", fg=MUTED, bg=BG).grid(row=6, column=0, sticky="w", pady=4)
        self.source_e = ttk.Combobox(form, values=["LAN", "Cloud"], width=25, state="readonly")
        self.source_e.set(cam.source_name)
        self.source_e.grid(row=6, column=1, pady=4, padx=(8, 0))
        tk.Label(form, text="Quality", fg=MUTED, bg=BG).grid(row=7, column=0, sticky="w", pady=4)
        self.quality_e = ttk.Combobox(form, values=["HD", "SD"], width=25, state="readonly")
        self.quality_e.set(cam.quality_name)
        self.quality_e.grid(row=7, column=1, pady=4, padx=(8, 0))
        self.rec_var = tk.IntVar(value=1 if cam.auto_record else 0)
        tk.Checkbutton(
            form,
            text="Auto record (keeps running after you close Studio)",
            variable=self.rec_var,
            fg=TEXT,
            bg=BG,
            selectcolor="#1f2937",
            activebackground=BG,
            activeforeground=TEXT,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(form, text="Chunk size", fg=MUTED, bg=BG).grid(row=9, column=0, sticky="w", pady=4)
        self.chunk_e = ttk.Combobox(form, values=["Every hour", "Every minute"], width=25, state="readonly")
        self.chunk_e.set("Every minute" if cam.record_chunk == "minute" else "Every hour")
        self.chunk_e.grid(row=9, column=1, pady=4, padx=(8, 0))

        tk.Button(form, text="Save", bg=ACCENT, fg="white", relief="flat", width=12, command=self._save).grid(
            row=10, column=1, sticky="e", pady=(12, 0)
        )

    def _save(self) -> None:
        try:
            port = int(self.port_e.get().strip())
            device_id = self.id_e.get().strip()
            int(device_id)
        except ValueError:
            messagebox.showerror("Invalid input", "Port and Device ID must be numbers.")
            return
        cam = Camera(
            id=self._cam.id,
            name=self.name_e.get().strip() or device_id,
            device_id=device_id,
            mac=self._cam.mac,
            ip=self.ip_e.get().strip(),
            port=port,
            username=self.user_e.get().strip(),
            password=self.pass_e.get(),
            source="cloud" if self.source_e.get() == "Cloud" else "lan",
            quality=1 if self.quality_e.get() == "HD" else 0,
            auto_record=bool(self.rec_var.get()),
            record_chunk="minute" if self.chunk_e.get() == "Every minute" else "hour",
            created_at=self._cam.created_at,
            updated_at=self._cam.updated_at,
        )
        if not cam.username or not cam.password:
            messagebox.showerror("Missing fields", "Username and password are required.")
            return
        self._on_save(cam)
        self.destroy()
