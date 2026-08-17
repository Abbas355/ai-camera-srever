"""Home mosaic: full-window live tiles and camera management."""

from __future__ import annotations

import io
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from auto_record import RecordSupervisor
from camera_store import Camera, CameraStore
from preview import PreviewManager
from theme import ACCENT, BG, CARD, GREEN, MUTED, RED, TEXT, TILE


def _fit_contain(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    if box_w < 8 or box_h < 8:
        return img
    fitted = img.copy()
    fitted.thumbnail((box_w, box_h), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (box_w, box_h), (2, 6, 23))
    x = (box_w - fitted.width) // 2
    y = (box_h - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


class HomeFrame(tk.Frame):
    def __init__(self, master, store: CameraStore, recorders: RecordSupervisor, on_add, on_view, on_edit):
        super().__init__(master, bg=BG)
        self.store = store
        self._recorders = recorders
        self._on_add = on_add
        self._on_view = on_view
        self._on_edit = on_edit
        self._tiles: dict[int, _Tile] = {}
        self._previews = PreviewManager(store, self._push_state)
        self._drain_on = True

        top = tk.Frame(self, bg=CARD, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="V380 Studio", fg="white", bg=CARD, font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(top, text="  Home  ·  live cameras", fg=MUTED, bg=CARD).pack(side="left", padx=(8, 0))
        tk.Button(top, text="Add camera", bg=ACCENT, fg="white", relief="flat", command=self._on_add).pack(side="right")
        tk.Button(top, text="Refresh", bg="#334155", fg="white", relief="flat", command=self.reload).pack(side="right", padx=(0, 8))

        self.status = tk.Label(self, text="", anchor="w", fg=TEXT, bg="#1e2937", padx=16, pady=6)
        self.status.pack(fill="x")

        self._stage = tk.Frame(self, bg=BG)
        self._stage.pack(fill="both", expand=True, padx=10, pady=10)
        self._stage.grid_rowconfigure(0, weight=1)
        self._stage.grid_columnconfigure(0, weight=1)

        self.reload()
        self.after(16, self._drain_previews)

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
        rec_n = sum(1 for c in cameras if c.auto_record)
        extra = "  ·  auto-record stays on after you close Studio" if rec_n else ""
        self.status.configure(text=f"{len(cameras)} camera(s)  ·  live mosaic{extra}")
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
        self._previews.stop()

    def resume(self) -> None:
        self.reload()

    def shutdown(self) -> None:
        self._drain_on = False
        self._previews.stop()

    def _drain_previews(self) -> None:
        if not self._drain_on:
            return
        for cam_id, tile in list(self._tiles.items()):
            jpeg = self._previews.take_latest(cam_id)
            if jpeg is not None:
                tile.set_jpeg(jpeg)
            else:
                tile.paint_if_dirty()
            tile.set_rec(tile.cam.auto_record, self._recorders.is_recording(cam_id))
        self._recorders.watch()
        self.after(16, self._drain_previews)

    def _toggle_rec(self, cam: Camera) -> None:
        cam.auto_record = not cam.auto_record
        self.store.set_auto_record(cam.id, cam.auto_record)
        self._recorders.sync(self.store.list())
        tile = self._tiles.get(cam.id)
        if tile is not None:
            tile.cam = cam
            tile.set_rec(cam.auto_record, self._recorders.is_recording(cam.id))

    def _delete(self, cam: Camera) -> None:
        if not messagebox.askyesno("Delete camera", f"Remove {cam.name} ({cam.device_id}) from this PC?"):
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
        self._photo: ImageTk.PhotoImage | None = None
        self._dirty = False
        self._box = (0, 0)

        foot = tk.Frame(self, bg=CARD)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        tk.Label(foot, text=cam.name, fg=TEXT, bg=CARD, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(foot, text=f"{cam.device_id}  ·  {cam.ip or 'cloud'}  ·  {cam.quality_name}", fg=MUTED, bg=CARD).pack(anchor="w")
        self._state = tk.Label(foot, text="Starting…", fg=GREEN, bg=CARD)
        self._state.pack(anchor="w")
        btns = tk.Frame(foot, bg=CARD)
        btns.pack(fill="x", pady=(6, 0))
        tk.Button(btns, text="View", bg=ACCENT, fg="white", relief="flat", width=8, command=lambda: on_view(cam)).pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Edit", bg="#334155", fg="white", relief="flat", width=8, command=lambda: on_edit(cam)).pack(side="left", padx=(0, 6))
        self._rec_btn = tk.Button(btns, text="Rec OFF", bg="#334155", fg="white", relief="flat", width=8, command=lambda: on_rec(cam))
        self._rec_btn.pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Delete", bg=RED, fg="white", relief="flat", width=8, command=lambda: on_delete(cam)).pack(side="left")
        self.set_rec(cam.auto_record, False)

        self._view = tk.Canvas(self, bg=TILE, highlightthickness=0, cursor="hand2")
        self._view.pack(side="top", fill="both", expand=True)
        self._view.bind("<Button-1>", lambda _e: on_view(cam))
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
        if not self._dirty and self._box == (w, h) and self._photo is not None:
            return
        img = Image.open(io.BytesIO(self._jpeg))
        fitted = _fit_contain(img, w, h)
        self._photo = ImageTk.PhotoImage(fitted)
        self._view.delete("all")
        self._view.create_image(w // 2, h // 2, image=self._photo)
        self._box = (w, h)
        self._dirty = False

    def set_state(self, text: str) -> None:
        color = GREEN if text == "Live" else MUTED
        self._state.configure(text=text, fg=color)

    def set_rec(self, enabled: bool, writing: bool) -> None:
        if enabled and writing:
            self._rec_btn.configure(text="REC ON", bg=RED)
        elif enabled:
            self._rec_btn.configure(text="REC…", bg="#7f1d1d")
        else:
            self._rec_btn.configure(text="Rec OFF", bg="#334155")


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
            text="Auto record (hourly MP4; keeps running after you close Studio)",
            variable=self.rec_var,
            fg=TEXT,
            bg=BG,
            selectcolor="#1f2937",
            activebackground=BG,
            activeforeground=TEXT,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        tk.Button(form, text="Save", bg=ACCENT, fg="white", relief="flat", width=12, command=self._save).grid(
            row=9, column=1, sticky="e", pady=(12, 0)
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
            created_at=self._cam.created_at,
            updated_at=self._cam.updated_at,
        )
        if not cam.username or not cam.password:
            messagebox.showerror("Missing fields", "Username and password are required.")
            return
        self._on_save(cam)
        self.destroy()
