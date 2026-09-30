"""Full live-view screen — same features as before (Listen, PTZ, Record, …)."""

from __future__ import annotations

import io
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from types import SimpleNamespace

from v380.client.api_client import ApiTalkClient
from v380.store.camera_store import Camera
from v380.client.extras import AlawPlayer, AlertSiren, H264Recorder, Talker, discover_devices, get_relay_ip
from v380.ui.profile import open_profile
from v380.ui.theme import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    FONT_BODY,
    FONT_HEAD,
    FONT_SMALL,
    GREEN,
    MUTED,
    ORANGE,
    RED,
    TEXT,
    TILE,
    ghost_button,
    primary_button,
    status_bar,
)
from v380.client.v380_client import LiveH264Decoder, V380SnapshotClient, make_live_decoder
from v380.paths import REC_DIR

import math


class PtzJoystick(tk.Canvas):
    """Round drag pad: move in any direction; release to stop."""

    SIZE = 148
    PAD_R = 58
    KNOB_R = 18
    DEAD = 0.22

    def __init__(self, master, on_dir, on_stop, **kwargs):
        super().__init__(
            master,
            width=self.SIZE,
            height=self.SIZE,
            bg=CARD,
            highlightthickness=0,
            cursor="hand2",
            **kwargs,
        )
        self._on_dir = on_dir
        self._on_stop = on_stop
        self._cx = self.SIZE // 2
        self._cy = self.SIZE // 2
        self._kx = self._cx
        self._ky = self._cy
        self._dragging = False
        self._last_cmd: str | None = None
        self._diag: tuple[str, str] | None = None
        self._tick = 0
        self._job = None
        self._draw()
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Leave>", self._maybe_release)

    def _draw(self) -> None:
        self.delete("all")
        # outer ring
        r = self.PAD_R + 8
        self.create_oval(
            self._cx - r,
            self._cy - r,
            self._cx + r,
            self._cy + r,
            outline=BORDER,
            width=2,
            fill="#0f161c",
        )
        self.create_oval(
            self._cx - self.PAD_R,
            self._cy - self.PAD_R,
            self._cx + self.PAD_R,
            self._cy + self.PAD_R,
            outline="#2a4a4a",
            width=1,
            fill="#121a22",
        )
        # crosshair
        self.create_line(self._cx, self._cy - self.PAD_R + 4, self._cx, self._cy + self.PAD_R - 4, fill="#2a3542")
        self.create_line(self._cx - self.PAD_R + 4, self._cy, self._cx + self.PAD_R - 4, self._cy, fill="#2a3542")
        for label, ang in (("U", -90), ("R", 0), ("D", 90), ("L", 180)):
            rad = math.radians(ang)
            lx = self._cx + math.cos(rad) * (self.PAD_R - 14)
            ly = self._cy + math.sin(rad) * (self.PAD_R - 14)
            self.create_text(lx, ly, text=label, fill=MUTED, font=FONT_SMALL)
        # knob
        self.create_oval(
            self._kx - self.KNOB_R,
            self._ky - self.KNOB_R,
            self._kx + self.KNOB_R,
            self._ky + self.KNOB_R,
            fill=ACCENT,
            outline="#5eead4",
            width=2,
            tags="knob",
        )

    def _set_knob(self, x: float, y: float) -> None:
        dx = x - self._cx
        dy = y - self._cy
        dist = math.hypot(dx, dy)
        if dist > self.PAD_R:
            dx *= self.PAD_R / dist
            dy *= self.PAD_R / dist
        self._kx = self._cx + dx
        self._ky = self._cy + dy
        self._draw()

    def _press(self, evt) -> None:
        self._dragging = True
        self._set_knob(evt.x, evt.y)
        self._update_dir(evt.x, evt.y)
        self._arm_tick()

    def _drag(self, evt) -> None:
        if not self._dragging:
            return
        self._set_knob(evt.x, evt.y)
        self._update_dir(evt.x, evt.y)

    def _maybe_release(self, _evt=None) -> None:
        if self._dragging:
            self._release()

    def _release(self, _evt=None) -> None:
        if not self._dragging and self._last_cmd is None and self._diag is None:
            return
        self._dragging = False
        self._kx = self._cx
        self._ky = self._cy
        self._draw()
        self._cancel_tick()
        self._last_cmd = None
        self._diag = None
        self._on_stop()

    def _arm_tick(self) -> None:
        self._cancel_tick()
        self._job = self.after(160, self._tick_diag)

    def _arm_hold_tick(self) -> None:
        """Re-send the current cardinal direction so motors do not time out."""
        self._cancel_tick()
        self._job = self.after(280, self._tick_hold)

    def _cancel_tick(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick_hold(self) -> None:
        self._job = None
        if not self._dragging or self._diag or not self._last_cmd:
            return
        self._on_dir(self._last_cmd)
        self._arm_hold_tick()

    def _tick_diag(self) -> None:
        self._job = None
        if not self._dragging or not self._diag:
            return
        a, b = self._diag
        self._tick ^= 1
        self._on_dir(a if self._tick else b)
        self._arm_tick()

    def _update_dir(self, x: float, y: float) -> None:
        dx = (x - self._cx) / float(self.PAD_R)
        dy = (y - self._cy) / float(self.PAD_R)
        mag = math.hypot(dx, dy)
        if mag < self.DEAD:
            if self._last_cmd is not None or self._diag is not None:
                self._last_cmd = None
                self._diag = None
                self._cancel_tick()
                self._on_stop()
            return
        # Screen y grows downward → invert for camera "up"
        angle = math.degrees(math.atan2(-dy, dx))  # -180..180, 0=right
        # 8-way sectors of 45°
        sector = int((angle + 22.5) % 360 // 45)
        mapping = {
            0: ("ptz_right", None),
            1: ("ptz_up", "ptz_right"),
            2: ("ptz_up", None),
            3: ("ptz_up", "ptz_left"),
            4: ("ptz_left", None),
            5: ("ptz_down", "ptz_left"),
            6: ("ptz_down", None),
            7: ("ptz_down", "ptz_right"),
        }
        primary, secondary = mapping[sector]
        if secondary:
            pair = (primary, secondary)
            if self._diag != pair:
                self._diag = pair
                self._last_cmd = None
                self._on_dir(primary)
                self._arm_tick()
        else:
            self._diag = None
            # Keepalive while held — V380 motors stop without refreshed START packets.
            if self._last_cmd != primary:
                self._last_cmd = primary
                self._on_dir(primary)
            self._arm_hold_tick()


class LiveView(tk.Frame):
    def __init__(
        self,
        master,
        on_back,
        on_connected=None,
        camera: Camera | None = None,
        auto_connect: bool = False,
        recorders=None,
        api=None,
    ):
        super().__init__(master, bg=BG)
        self._on_back = on_back
        self._on_connected = on_connected
        self._preset = camera
        self._auto_connect = auto_connect
        self._recorders = recorders
        self._api = api
        self._cam_id = camera.id if camera is not None else 0

        self._client: V380SnapshotClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames: queue.Queue = queue.Queue(maxsize=2)
        self._decoder: LiveH264Decoder | None = None
        self._player = AlawPlayer()
        self._talker = Talker()
        self._alert = AlertSiren()
        self._recorder = H264Recorder(REC_DIR)
        self._ezviz_recorder = None
        self._ezviz_ready = False
        self._last_jpeg: bytes | None = None
        self._last_rgb = None
        self._fps_count = 0
        self._fps_t = 0.0
        self._photo: ImageTk.PhotoImage | None = None
        self._quality_name = "HD"
        self._codec_label = "H.264"
        self._devices = []
        self._pending_mac = camera.mac if camera else ""
        self._saved_once = False
        self._alive = True
        self._view_zoom = 1.0
        self._dual_mode = "off"  # off | split | top | bottom
        self._dual_checked = False
        self._photo_top: ImageTk.PhotoImage | None = None
        self._photo_bot: ImageTk.PhotoImage | None = None
        self._need_keyframe = False
        self._gray_hits = 0
        self._alert_job = None
        self._ptz_pan = 0.0
        self._ptz_tilt = 0.0
        self._ptz_zoom_pos = 0.0
        self._ptz_last_cmd: str | None = None
        self._ptz_last_t: float | None = None
        self._ptz_busy = False
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._job_loop, daemon=True, name="live-jobs").start()

        self._build()
        if camera is not None:
            self._apply_camera(camera)
        self.after(20, self._drain_frames)
        self.after(400, self._talker.prepare)
        if auto_connect and camera is not None:
            self.after(200, self._connect)

    def _job_loop(self) -> None:
        while True:
            fn = self._jobs.get()
            if fn is None:
                break
            try:
                fn()
            except Exception:
                pass

    def _bg(self, fn) -> None:
        self._jobs.put(fn)

    def _ui(self, fn) -> None:
        if not self._alive:
            return
        try:
            if not self.winfo_exists():
                return
            self.after(0, fn)
        except Exception:
            return

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD, padx=18, pady=12, highlightthickness=1, highlightbackground=BORDER)
        top.pack(fill="x")
        ghost_button(top, "← Home", self.go_back).pack(side="left", padx=(0, 14))
        brand = tk.Frame(top, bg=CARD)
        brand.pack(side="left")
        tk.Label(brand, text="Live view", fg=TEXT, bg=CARD, font=FONT_HEAD).pack(anchor="w")
        tk.Label(brand, text="Discover, connect, control", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")
        ghost_button(top, "Profile", self._open_profile).pack(side="right")

        form = tk.Frame(self, bg=BG, padx=18, pady=10)
        form.pack(fill="x")

        def lab(r, c, t):
            tk.Label(form, text=t, fg=MUTED, bg=BG, font=FONT_SMALL).grid(row=r, column=c, sticky="w")

        def ent(r, c, w, default="", show=""):
            e = tk.Entry(
                form,
                width=w,
                show=show,
                bg=TILE,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
                font=FONT_BODY,
            )
            e.insert(0, default)
            e.grid(row=r + 1, column=c, padx=(0, 8), pady=(2, 6), sticky="we", ipady=4)
            return e

        lab(0, 0, "Found cameras")
        self.cam_box = ttk.Combobox(form, width=28, state="readonly")
        self.cam_box.grid(row=1, column=0, padx=(0, 8), pady=(2, 6))
        self.cam_box.bind("<<ComboboxSelected>>", self._pick_device)
        ttk.Button(form, text="Discover", command=self._discover).grid(row=1, column=1, padx=(0, 8))

        lab(0, 2, "Brand")
        self.brand_e = ttk.Combobox(form, values=["V380", "EZVIZ"], width=8, state="readonly")
        self.brand_e.set("V380")
        self.brand_e.grid(row=1, column=2, padx=(0, 8), pady=(2, 6))
        self.brand_e.bind("<<ComboboxSelected>>", lambda _e: self._on_brand_change())

        lab(0, 3, "Source")
        self.source_e = ttk.Combobox(form, values=["LAN", "Cloud"], width=8, state="readonly")
        self.source_e.set("LAN")
        self.source_e.grid(row=1, column=3, padx=(0, 8), pady=(2, 6))

        lab(0, 4, "Quality")
        self.quality_e = ttk.Combobox(form, values=["HD", "SD"], width=6, state="readonly")
        self.quality_e.set("HD")
        self.quality_e.grid(row=1, column=4, padx=(0, 8), pady=(2, 6))

        lab(2, 0, "Camera IP")
        self.ip_e = ent(2, 0, 16, "")
        lab(2, 1, "Port")
        self.port_e = ent(2, 1, 8, "8800")
        lab(2, 2, "Device ID")
        self.id_e = ent(2, 2, 12, "")
        lab(2, 3, "Username")
        self.user_e = ent(2, 3, 12, "")
        self._pass_lab = tk.Label(form, text="Password", fg=MUTED, bg=BG, font=FONT_SMALL)
        self._pass_lab.grid(row=2, column=4, sticky="w")
        self.pass_e = ent(2, 4, 14, "", show="*")
        lab(2, 5, "Display name")
        self.name_e = ent(2, 5, 16, "")

        self.connect_btn = primary_button(form, "Connect", self._toggle)
        self.connect_btn.grid(row=3, column=6, padx=(8, 0), pady=(2, 6))

        self.status = status_bar(self)
        self.status.configure(text="Pick brand (V380 or EZVIZ), enter credentials, Connect — saved only if login works")
        self.status.pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(10, 16))
        self._video_host = tk.Frame(body, bg=TILE)
        self._video_host.pack(side="left", fill="both", expand=True, padx=(0, 12))

        self._split = tk.Frame(self._video_host, bg=TILE)
        self._split.rowconfigure(0, weight=1)
        self._split.rowconfigure(1, weight=1)
        self._split.columnconfigure(0, weight=1)

        top_box = tk.Frame(self._split, bg=TILE, highlightthickness=1, highlightbackground=BORDER)
        top_box.grid(row=0, column=0, sticky="nsew", padx=0, pady=(0, 3))
        tk.Label(top_box, text="Fixed lens  ·  click for full view", fg=MUTED, bg=TILE, font=FONT_SMALL).pack(
            anchor="w", padx=8, pady=(4, 0)
        )
        self._top_view = tk.Label(top_box, bg=TILE, text="", cursor="hand2")
        self._top_view.pack(fill="both", expand=True)
        self._top_view.bind("<Button-1>", lambda _e: self._set_dual_mode("top"))
        self._top_view.bind("<MouseWheel>", self._on_wheel)

        bot_box = tk.Frame(self._split, bg=TILE, highlightthickness=1, highlightbackground=BORDER)
        bot_box.grid(row=1, column=0, sticky="nsew", padx=0, pady=(3, 0))
        tk.Label(bot_box, text="PTZ lens  ·  click for full view  ·  this one moves", fg=MUTED, bg=TILE, font=FONT_SMALL).pack(
            anchor="w", padx=8, pady=(4, 0)
        )
        self._bot_view = tk.Label(bot_box, bg=TILE, text="", cursor="hand2")
        self._bot_view.pack(fill="both", expand=True)
        self._bot_view.bind("<Button-1>", lambda _e: self._set_dual_mode("bottom"))
        self._bot_view.bind("<MouseWheel>", self._on_wheel)

        self.canvas = tk.Label(self._video_host, bg=TILE, text="Live video", fg=MUTED, font=FONT_BODY, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-1>", self._on_single_click)
        self._build_controls(body)

    def _build_controls(self, parent: tk.Frame) -> None:
        shell = tk.Frame(parent, bg=CARD, width=290, highlightthickness=1, highlightbackground=BORDER)
        shell.pack(side="right", fill="y")
        shell.pack_propagate(False)

        scroll = ttk.Scrollbar(shell, orient="vertical")
        scroll.pack(side="right", fill="y")
        canvas = tk.Canvas(shell, bg=CARD, highlightthickness=0, width=268)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.configure(command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)

        panel = tk.Frame(canvas, bg=CARD, padx=12, pady=10)
        win = canvas.create_window((0, 0), window=panel, anchor="nw")

        def _sync_scroll(_evt=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win, width=max(canvas.winfo_width(), 250))

        panel.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_scroll)

        def _wheel(evt) -> None:
            if evt.delta:
                canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
            elif getattr(evt, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(evt, "num", None) == 5:
                canvas.yview_scroll(1, "units")

        def _bind_wheel(_evt=None) -> None:
            canvas.bind_all("<MouseWheel>", _wheel)
            canvas.bind_all("<Button-4>", _wheel)
            canvas.bind_all("<Button-5>", _wheel)

        def _unbind_wheel(_evt=None) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        shell.bind("<Enter>", _bind_wheel)
        shell.bind("<Leave>", _unbind_wheel)
        self._controls_canvas = canvas

        def title(text: str) -> None:
            tk.Label(panel, text=text, fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w", pady=(12, 6))

        def btn(parent, text, color, cmd, width=8):
            return tk.Button(
                parent,
                text=text,
                width=width,
                bg=color,
                fg="white",
                relief="flat",
                command=cmd,
                cursor="hand2",
                font=FONT_BODY,
            )

        title("MEDIA")
        media = tk.Frame(panel, bg=CARD)
        media.pack(fill="x")
        self.listen_btn = btn(media, "Listen OFF", "#243040", self._toggle_listen, 11)
        self.listen_btn.grid(row=0, column=0, padx=3, pady=2)
        self.talk_btn = btn(media, "Talk OFF", "#243040", self._toggle_talk, 11)
        self.talk_btn.grid(row=0, column=1, padx=3, pady=2)
        btn(media, "Snapshot", ACCENT, self._snapshot, 11).grid(row=1, column=0, padx=3, pady=2)
        self.rec_btn = btn(media, "Record", RED, self._toggle_record, 11)
        self.rec_btn.grid(row=1, column=1, padx=3, pady=2)
        btn(media, "Open folder", "#243040", self._open_folder, 11).grid(row=2, column=0, padx=3, pady=2)
        self.dual_btn = btn(media, "Dual view", "#243040", self._toggle_dual, 11)
        self.dual_btn.grid(row=2, column=1, padx=3, pady=2)
        self.both_btn = btn(media, "Both lenses", ACCENT, lambda: self._set_dual_mode("split"), 11)
        self.both_btn.grid(row=3, column=0, columnspan=2, padx=3, pady=2)
        self.both_btn.grid_remove()

        title("PTZ control")
        mode_row = tk.Frame(panel, bg=CARD)
        mode_row.pack(fill="x", pady=(0, 6))
        self._ptz_mode = tk.StringVar(value="both")
        for label, val in (("Joystick", "joy"), ("Buttons", "buttons"), ("Both", "both")):
            tk.Radiobutton(
                mode_row,
                text=label,
                variable=self._ptz_mode,
                value=val,
                command=self._apply_ptz_mode,
                fg=TEXT,
                bg=CARD,
                activebackground=CARD,
                activeforeground=TEXT,
                selectcolor=CARD,
                highlightthickness=0,
                font=FONT_SMALL,
            ).pack(side="left", padx=(0, 8))

        self._joy_wrap = tk.Frame(panel, bg=CARD)
        self._joy_wrap.pack(pady=(0, 6))
        self._joystick = PtzJoystick(self._joy_wrap, on_dir=self._joy_dir, on_stop=self._joy_stop)
        self._joystick.pack()
        tk.Label(self._joy_wrap, text="Drag any direction · release to stop", fg=MUTED, bg=CARD, font=FONT_SMALL).pack()

        self._btn_wrap = tk.Frame(panel, bg=CARD)
        self._btn_wrap.pack(pady=(0, 4))

        def ptz(row, col, label, cmd):
            b = tk.Button(self._btn_wrap, text=label, width=7, bg=ACCENT, fg="white", relief="flat", cursor="hand2")
            b.grid(row=row, column=col, padx=3, pady=3)
            b.bind("<ButtonPress-1>", lambda _e, c=cmd: self._cmd(c))
            b.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))

        ptz(0, 1, "UP", "ptz_up")
        ptz(1, 0, "LEFT", "ptz_left")
        tk.Button(self._btn_wrap, text="STOP", width=7, bg="#243040", fg="white", relief="flat", command=lambda: self._cmd("ptz_stop")).grid(
            row=1, column=1, padx=3, pady=3
        )
        ptz(1, 2, "RIGHT", "ptz_right")
        ptz(2, 1, "DOWN", "ptz_down")

        zoom_row = tk.Frame(panel, bg=CARD)
        self._zoom_row = zoom_row
        zoom_row.pack(pady=(4, 0))
        zoom_out = tk.Button(zoom_row, text="ZOOM -", width=8, bg=ACCENT, fg="white", relief="flat", cursor="hand2")
        zoom_out.grid(row=0, column=0, padx=3, pady=3)
        zoom_out.bind("<ButtonPress-1>", lambda _e: self._zoom_hold("ptz_zoom_out", -0.25))
        zoom_out.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))
        tk.Button(zoom_row, text="1x", width=7, bg="#243040", fg="white", relief="flat", command=self._zoom_reset).grid(
            row=0, column=1, padx=3, pady=3
        )
        zoom_in = tk.Button(zoom_row, text="ZOOM +", width=8, bg=ACCENT, fg="white", relief="flat", cursor="hand2")
        zoom_in.grid(row=0, column=2, padx=3, pady=3)
        zoom_in.bind("<ButtonPress-1>", lambda _e: self._zoom_hold("ptz_zoom_in", 0.25))
        zoom_in.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))
        cal_row = tk.Frame(panel, bg=CARD)
        cal_row.pack(pady=(8, 0))
        btn(cal_row, "Calibrate PTZ", ORANGE, self._calibrate_ptz, 22).pack(fill="x", padx=3, pady=2)
        tk.Label(
            cal_row,
            text="Self-check motors · clears tracking offset",
            fg=MUTED,
            bg=CARD,
            font=FONT_SMALL,
        ).pack(anchor="w", padx=4)

        self._apply_ptz_mode()

        title("LIGHT")
        lights = tk.Frame(panel, bg=CARD)
        lights.pack(fill="x")
        for i, (label, cmd) in enumerate((("ON", "light_on"), ("OFF", "light_off"), ("AUTO", "light_auto"))):
            btn(lights, label, GREEN, lambda c=cmd: self._cmd(c), 7).grid(row=0, column=i, padx=3, pady=2)

        title("IMAGE")
        imgs = tk.Frame(panel, bg=CARD)
        imgs.pack(fill="x")
        for i, (label, cmd) in enumerate((("COLOR", "image_color"), ("B&W", "image_bw"))):
            btn(imgs, label, ORANGE, lambda c=cmd: self._cmd(c), 11).grid(row=0, column=i, padx=3, pady=2)
        for i, (label, cmd) in enumerate((("AUTO", "image_auto"), ("FLIP", "image_flip"))):
            btn(imgs, label, ORANGE, lambda c=cmd: self._cmd(c), 11).grid(row=1, column=i, padx=3, pady=2)

        title("ALERT")
        alert = tk.Frame(panel, bg=CARD)
        alert.pack(fill="x")
        btn(alert, "ON", RED, self._alert_on, 11).grid(row=0, column=0, padx=3, pady=2)
        btn(alert, "OFF", "#243040", self._alert_off, 11).grid(row=0, column=1, padx=3, pady=2)
        hold = tk.Button(alert, text="HOLD", width=11, bg=RED, fg="white", relief="flat", cursor="hand2")
        hold.grid(row=1, column=0, columnspan=2, padx=3, pady=2)
        hold.bind("<ButtonPress-1>", lambda _e: self._alert_on())
        hold.bind("<ButtonRelease-1>", lambda _e: self._alert_off())
        self._alert_wav_btn = btn(alert, "Play alert.wav on camera", ORANGE, self._toggle_alert_wav, 22)
        self._alert_wav_btn.grid(row=2, column=0, columnspan=2, padx=3, pady=(6, 2), sticky="ew")
        tk.Label(
            alert,
            text="Plays audio/alert.wav from the CAMERA speaker.\n"
            "EZVIZ: needs Open Platform keys for custom WAV,\n"
            "otherwise uses camera siren (not PC).",
            fg=MUTED,
            bg=CARD,
            justify="left",
            font=FONT_SMALL,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

        self._ezviz_panel = tk.Frame(panel, bg=CARD)
        self._build_ezviz_panel(self._ezviz_panel, title, btn)

        title("NOTE")
        self._note_lab = tk.Label(
            panel,
            text="Hold arrows to pan/tilt.\nScroll for light, image, alert.",
            fg=MUTED,
            bg=CARD,
            justify="left",
            font=FONT_SMALL,
        )
        self._note_lab.pack(anchor="w", pady=(0, 12))
        self._on_brand_change()

    def _build_ezviz_panel(self, host: tk.Frame, title, btn) -> None:
        """Toggle-style controls that show current ON/OFF state."""
        self._ezviz_toggles: dict[str, tk.Button] = {}
        self._ezviz_modes: dict[str, dict[str, tk.Button]] = {}
        self._ezviz_states: dict = {}
        self._ezviz_siren_local = False
        self._ezviz_armed_local: bool | None = None

        def ez_title(text: str) -> None:
            tk.Label(host, text=text, fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w", pady=(12, 6))

        # Emergency stop — always first
        stop = tk.Button(
            host,
            text="STOP ALL ALARMS / SILENCE",
            bg=RED,
            fg="white",
            relief="flat",
            cursor="hand2",
            font=FONT_BODY,
            command=lambda: self._ezviz_action("silence_all"),
        )
        stop.pack(fill="x", padx=3, pady=(4, 6))
        self._ezviz_online_lab = tk.Label(
            host,
            text="States: not loaded — Connect + sign in, then Refresh",
            fg=MUTED,
            bg=CARD,
            justify="left",
            font=FONT_SMALL,
        )
        self._ezviz_online_lab.pack(anchor="w", padx=4, pady=(0, 4))
        ref_row = tk.Frame(host, bg=CARD)
        ref_row.pack(fill="x", pady=(0, 4))
        btn(ref_row, "Refresh states", ACCENT, self._refresh_ezviz_states, 22).pack(fill="x", padx=3)

        def toggle(key: str, label: str, on_color=GREEN) -> None:
            b = tk.Button(
                host,
                text=f"{label}: —",
                width=24,
                bg="#243040",
                fg="white",
                relief="flat",
                cursor="hand2",
                font=FONT_BODY,
                command=lambda k=key: self._ezviz_toggle(k),
            )
            b.pack(fill="x", padx=3, pady=2)
            b._ez_label = label  # type: ignore[attr-defined]
            b._ez_on_color = on_color  # type: ignore[attr-defined]
            self._ezviz_toggles[key] = b

        def mode_row(group: str, title_text: str, options: list[tuple[str, str]]) -> None:
            ez_title(title_text)
            fr = tk.Frame(host, bg=CARD)
            fr.pack(fill="x", pady=(0, 2))
            self._ezviz_modes[group] = {}
            for i, (label, value) in enumerate(options):
                b = tk.Button(
                    fr,
                    text=label,
                    width=11,
                    bg="#243040",
                    fg="white",
                    relief="flat",
                    cursor="hand2",
                    font=FONT_BODY,
                    command=lambda g=group, v=value: self._ezviz_set_mode(g, v),
                )
                b.grid(row=0, column=i, padx=3, pady=2, sticky="ew")
                self._ezviz_modes[group][value] = b

        ez_title("Privacy / Sleep")
        toggle("privacy", "Privacy", ORANGE)
        toggle("sleep", "Sleep", ORANGE)

        ez_title("Tracking")
        toggle("track", "Smart track", GREEN)
        toggle("cruise", "Cruise", GREEN)
        toggle("feature_track", "Feature track", GREEN)

        ez_title("Lights / IR / Flood")
        toggle("led", "Status LED", GREEN)
        toggle("ir", "IR night LEDs", GREEN)
        btn(host, "IR AUTO (day/night)", ACCENT, lambda: self._ezviz_action("ir_auto"), 24).pack(
            fill="x", padx=3, pady=2
        )
        toggle("flood", "Flood light", GREEN)
        toggle("flicker", "Strobe (flood+flash)", RED)
        tk.Label(
            host,
            text="IR OFF = force day mode (LEDs off).\n"
            "IR ON = force night / B&W (LEDs on — look pink on a phone camera).\n"
            "IR AUTO = camera decides from light sensor.",
            fg=MUTED,
            bg=CARD,
            justify="left",
            font=FONT_SMALL,
        ).pack(anchor="w", padx=4, pady=(0, 4))

        mode_row(
            "night_vision",
            "Night vision",
            [("Colour", "1"), ("B&W", "0"), ("Smart", "2")],
        )

        ez_title("Audio")
        toggle("mic", "Microphone", ACCENT)
        toggle("logo", "OSD logo", "#243040")
        toggle("alarm_tone", "Alarm tone", ORANGE)

        ez_title("Volume (speaker / mic)")
        vol_fr = tk.Frame(host, bg=CARD)
        vol_fr.pack(fill="x", padx=2, pady=(0, 4))
        self._ezviz_spk_lab = tk.Label(
            vol_fr, text="Alarm / speaker: 80%", fg="white", bg=CARD, font=FONT_SMALL, anchor="w"
        )
        self._ezviz_spk_lab.pack(fill="x")
        self._ezviz_spk_scale = tk.Scale(
            vol_fr,
            from_=0,
            to=100,
            orient="horizontal",
            bg=CARD,
            fg="white",
            highlightthickness=0,
            troughcolor="#243040",
            activebackground=ACCENT,
            length=220,
            showvalue=0,
            command=self._on_ezviz_spk_slide,
        )
        self._ezviz_spk_scale.set(80)
        self._ezviz_spk_scale.pack(fill="x", pady=(0, 6))
        self._ezviz_mic_lab = tk.Label(
            vol_fr, text="Microphone: 50%", fg="white", bg=CARD, font=FONT_SMALL, anchor="w"
        )
        self._ezviz_mic_lab.pack(fill="x")
        self._ezviz_mic_scale = tk.Scale(
            vol_fr,
            from_=0,
            to=100,
            orient="horizontal",
            bg=CARD,
            fg="white",
            highlightthickness=0,
            troughcolor="#243040",
            activebackground=ACCENT,
            length=220,
            showvalue=0,
            command=self._on_ezviz_mic_slide,
        )
        self._ezviz_mic_scale.set(50)
        self._ezviz_mic_scale.pack(fill="x", pady=(0, 4))
        self._ezviz_vol_job = None
        self._ezviz_vol_busy = False
        btn(vol_fr, "Apply volumes now", ACCENT, self._apply_ezviz_volumes, 22).pack(fill="x", padx=1, pady=2)
        tk.Label(
            vol_fr,
            text="Sliders auto-save ~0.8s after you stop dragging.\n"
            "Custom alarm WAV: use EZVIZ phone app → Settings → Sound.",
            fg=MUTED,
            bg=CARD,
            justify="left",
            font=FONT_SMALL,
        ).pack(anchor="w", pady=(2, 4))

        ez_title("Detection")
        toggle("armed", "Armed (alerts)", RED)
        toggle("human", "Human detect", GREEN)
        toggle("dnd", "Do not disturb", ORANGE)
        toggle("allday", "All-day record", GREEN)

        mode_row(
            "alarm_sound",
            "Alarm sound level",
            [("Soft", "0"), ("Loud", "1"), ("Mute", "2")],
        )
        mode_row(
            "sens",
            "Detection sensitivity",
            [("Low", "1"), ("Mid", "3"), ("High", "6")],
        )

        ez_title("Siren (active defense)")
        toggle("siren", "Siren", RED)

        tk.Label(
            host,
            text="Green/red button = ON now. Grey = OFF.\n"
            "Press STOP ALL ALARMS if the camera is beeping.\n"
            "Refresh states after power-on (camera must be Online).",
            fg=MUTED,
            bg=CARD,
            justify="left",
            font=FONT_SMALL,
        ).pack(anchor="w", pady=(8, 4))

    def _paint_ezviz_toggle(self, key: str, on: bool | None) -> None:
        btn = (getattr(self, "_ezviz_toggles", {}) or {}).get(key)
        if btn is None:
            return
        label = getattr(btn, "_ez_label", key)
        on_color = getattr(btn, "_ez_on_color", GREEN)
        if on is None:
            btn.configure(text=f"{label}: ?", bg="#243040")
        elif on:
            btn.configure(text=f"{label}: ON", bg=on_color)
        else:
            btn.configure(text=f"{label}: OFF", bg="#243040")

    def _paint_ezviz_mode(self, group: str, current: str | None) -> None:
        modes = (getattr(self, "_ezviz_modes", {}) or {}).get(group) or {}
        for value, btn in modes.items():
            active = current is not None and str(current) == str(value)
            btn.configure(bg=ORANGE if active else "#243040")

    def _apply_ezviz_states(self, states: dict) -> None:
        self._ezviz_states = dict(states or {})
        if "siren" in states:
            self._ezviz_siren_local = bool(states.get("siren"))
        online = states.get("online")
        serial = states.get("serial") or ""
        lab = getattr(self, "_ezviz_online_lab", None)
        if lab is not None:
            if online is True:
                lab.configure(text=f"Cloud: ONLINE  {serial}", fg=GREEN)
            elif online is False:
                lab.configure(text=f"Cloud: OFFLINE  {serial} — plug in / wait", fg=ORANGE)
            else:
                lab.configure(text="States loaded", fg=MUTED)
        for key in (
            "privacy",
            "sleep",
            "track",
            "cruise",
            "feature_track",
            "led",
            "ir",
            "flood",
            "flicker",
            "mic",
            "logo",
            "alarm_tone",
            "human",
            "allday",
            "dnd",
        ):
            if key in states:
                self._paint_ezviz_toggle(key, bool(states.get(key)))
        armed = states.get("armed")
        if armed is None:
            armed = self._ezviz_armed_local
        self._paint_ezviz_toggle("armed", armed if armed is not None else None)
        self._paint_ezviz_toggle("siren", bool(self._ezviz_siren_local or states.get("siren")))
        if "night_vision" in states:
            self._paint_ezviz_mode("night_vision", str(states.get("night_vision")))
        if "alarm_sound" in states:
            self._paint_ezviz_mode("alarm_sound", str(states.get("alarm_sound")))
        if "sens" in states:
            self._paint_ezviz_mode("sens", str(states.get("sens")))
        # Volume sliders (avoid feedback loop while user is dragging)
        if not getattr(self, "_ezviz_vol_busy", False):
            if "speaker_volume" in states and getattr(self, "_ezviz_spk_scale", None) is not None:
                sp = int(states.get("speaker_volume") or 80)
                self._ezviz_spk_scale.set(sp)
                if getattr(self, "_ezviz_spk_lab", None) is not None:
                    self._ezviz_spk_lab.configure(text=f"Alarm / speaker: {sp}%")
            if "mic_volume" in states and getattr(self, "_ezviz_mic_scale", None) is not None:
                mic = int(states.get("mic_volume") or 50)
                self._ezviz_mic_scale.set(mic)
                if getattr(self, "_ezviz_mic_lab", None) is not None:
                    self._ezviz_mic_lab.configure(text=f"Microphone: {mic}%")

    def _on_ezviz_spk_slide(self, raw) -> None:
        try:
            sp = int(float(raw))
        except (TypeError, ValueError):
            return
        lab = getattr(self, "_ezviz_spk_lab", None)
        if lab is not None:
            lab.configure(text=f"Alarm / speaker: {sp}%")
        self._schedule_ezviz_volume_apply()

    def _on_ezviz_mic_slide(self, raw) -> None:
        try:
            mic = int(float(raw))
        except (TypeError, ValueError):
            return
        lab = getattr(self, "_ezviz_mic_lab", None)
        if lab is not None:
            lab.configure(text=f"Microphone: {mic}%")
        self._schedule_ezviz_volume_apply()

    def _schedule_ezviz_volume_apply(self) -> None:
        job = getattr(self, "_ezviz_vol_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._ezviz_vol_job = self.after(800, self._apply_ezviz_volumes)

    def _apply_ezviz_volumes(self) -> None:
        if self._brand() != "ezviz":
            return
        ip = self.ip_e.get().strip()
        if not ip:
            self._set_status("Enter camera IP first")
            return
        sp = int(self._ezviz_spk_scale.get()) if getattr(self, "_ezviz_spk_scale", None) else 80
        mic = int(self._ezviz_mic_scale.get()) if getattr(self, "_ezviz_mic_scale", None) else 50
        self._ezviz_vol_busy = True
        self._set_status(f"Setting volumes speaker {sp}% · mic {mic}%…")

        def work() -> None:
            try:
                if not self._ezviz_cloud_blocking():
                    self._ui(lambda: self._set_status("Sign in with EZVIZ app account first"))
                    return
                from v380.client.ezviz_session import set_audio_volumes

                msg = set_audio_volumes(ip, speaker=sp, microphone=mic)
                st = dict(getattr(self, "_ezviz_states", {}) or {})
                st["speaker_volume"] = sp
                st["mic_volume"] = mic
                self._ui(lambda s=st: self._apply_ezviz_states(s))
                self._ui(lambda m=msg: self._set_status(m))
            except Exception as exc:
                self._ui(lambda m=str(exc): self._set_status(f"Volume failed: {m}"))
            finally:
                self._ui(lambda: setattr(self, "_ezviz_vol_busy", False))

        threading.Thread(target=work, daemon=True, name="ezviz-vol").start()

    def _refresh_ezviz_states(self) -> None:
        if self._brand() != "ezviz":
            return
        ip = self.ip_e.get().strip()
        if not ip:
            lab = getattr(self, "_ezviz_online_lab", None)
            if lab is not None:
                lab.configure(text="Enter camera IP, then states load automatically", fg=MUTED)
            return
        if getattr(self, "_ezviz_refreshing", False):
            return
        self._ezviz_refreshing = True
        self._set_status("Reading EZVIZ feature states…")

        def work() -> None:
            try:
                if not self._ezviz_cloud_blocking():
                    self._ui(lambda: self._set_status("Sign in with EZVIZ app account first"))
                    return
                from v380.client.ezviz_session import get_feature_states

                st = get_feature_states(ip)
                st["siren"] = bool(getattr(self, "_ezviz_siren_local", False))
                if self._ezviz_armed_local is not None:
                    st["armed"] = self._ezviz_armed_local
                self._ui(lambda s=st: self._apply_ezviz_states(s))
                self._ui(
                    lambda: self._set_status(
                        "States updated — coloured = ON, grey = OFF"
                    )
                )
            except Exception as exc:
                self._ui(lambda m=str(exc): self._set_status(f"State refresh failed: {m}"))
            finally:
                self._ui(lambda: setattr(self, "_ezviz_refreshing", False))

        threading.Thread(target=work, daemon=True, name="ezviz-states").start()

    def _ezviz_toggle(self, key: str) -> None:
        """Flip a boolean feature; button label shows new state after success."""
        action_map = {
            "privacy": ("privacy_on", "privacy_off"),
            "sleep": ("sleep_on", "sleep_off"),
            "track": ("track_on", "track_off"),
            "cruise": ("cruise_on", "cruise_off"),
            "feature_track": ("feature_track_on", "feature_track_off"),
            "led": ("led_on", "led_off"),
            "ir": ("ir_on", "ir_off"),
            "flood": ("flood_on", "flood_off"),
            "flicker": ("flicker_on", "flicker_off"),
            "mic": ("mic_on", "mic_off"),
            "logo": ("logo_on", "logo_off"),
            "alarm_tone": ("alarm_tone_on", "alarm_tone_off"),
            "human": ("human_on", "human_off"),
            "allday": ("allday_on", "allday_off"),
            "dnd": ("dnd_on", "dnd_off"),
            "armed": ("arm", "disarm"),
            "siren": ("siren_on", "siren_off"),
        }
        pair = action_map.get(key)
        if not pair:
            return
        cur = bool((self._ezviz_states or {}).get(key))
        if key == "siren":
            cur = bool(getattr(self, "_ezviz_siren_local", False))
        if key == "armed" and self._ezviz_armed_local is not None:
            cur = bool(self._ezviz_armed_local)
        name = pair[1] if cur else pair[0]
        self._ezviz_action(name, state_key=key, new_state=not cur)

    def _ezviz_set_mode(self, group: str, value: str) -> None:
        if group == "night_vision":
            name = {"0": "nv_bw", "1": "nv_color", "2": "nv_smart"}.get(str(value), "nv_smart")
        elif group == "alarm_sound":
            name = {"0": "sound_soft", "1": "sound_loud", "2": "sound_mute"}.get(str(value), "sound_soft")
        elif group == "sens":
            name = {"1": "sens_low", "3": "sens_mid", "6": "sens_high"}.get(str(value), "sens_mid")
        else:
            return
        self._ezviz_action(name, mode_group=group, mode_value=value)

    def _ezviz_action(
        self,
        name: str,
        state_key: str | None = None,
        new_state: bool | None = None,
        mode_group: str | None = None,
        mode_value: str | None = None,
    ) -> None:
        if self._brand() != "ezviz":
            return
        if not self.ip_e.get().strip():
            self._set_status("Connect / enter camera IP first")
            return
        self._set_status(name.replace("_", " ") + "…")

        def work() -> None:
            try:
                if not self._ezviz_cloud_blocking():
                    self._ui(lambda: self._set_status("Sign in with EZVIZ app account first"))
                    return
                from v380.client.ezviz_session import run_action

                msg = run_action(self.ip_e.get().strip(), name)
                if name in ("siren_on", "alert_on"):
                    self._ezviz_siren_local = True
                if name in ("siren_off", "alert_off", "silence_all"):
                    self._ezviz_siren_local = False
                if name == "arm":
                    self._ezviz_armed_local = True
                if name == "disarm":
                    self._ezviz_armed_local = False
                if name == "silence_all":
                    # reflect silenced switches locally
                    st = dict(self._ezviz_states or {})
                    st.update(
                        {
                            "siren": False,
                            "flicker": False,
                            "flood": False,
                            "alarm_tone": False,
                            "alarm_sound": 2,
                        }
                    )
                    self._ui(lambda s=st: self._apply_ezviz_states(s))
                elif state_key is not None and new_state is not None:
                    st = dict(self._ezviz_states or {})
                    st[state_key] = new_state
                    if state_key == "siren":
                        st["siren"] = new_state
                    if state_key == "flicker":
                        # strobe also drives flood light
                        st["flood"] = bool(new_state)
                    if state_key == "ir":
                        st["night_vision"] = 0 if new_state else 2  # B&W / smart
                    self._ui(lambda s=st: self._apply_ezviz_states(s))
                elif mode_group is not None and mode_value is not None:
                    st = dict(self._ezviz_states or {})
                    if mode_group == "night_vision":
                        st["night_vision"] = int(mode_value)
                    elif mode_group == "alarm_sound":
                        st["alarm_sound"] = int(mode_value)
                    elif mode_group == "sens":
                        st["sens"] = int(mode_value)
                    self._ui(lambda s=st: self._apply_ezviz_states(s))
                self._ui(lambda m=msg: self._set_status(m))
            except Exception as exc:
                self._ui(lambda m=str(exc): self._set_status(f"Failed: {m}"))

        threading.Thread(target=work, daemon=True, name="ezviz-act").start()

    def _apply_camera(self, cam: Camera) -> None:
        self.brand_e.set(cam.brand_name)
        self.name_e.delete(0, "end")
        self.name_e.insert(0, cam.name)
        self.ip_e.delete(0, "end")
        self.ip_e.insert(0, cam.ip)
        self.port_e.delete(0, "end")
        self.port_e.insert(0, str(cam.port))
        self.id_e.delete(0, "end")
        self.id_e.insert(0, cam.device_id)
        self.user_e.delete(0, "end")
        self.user_e.insert(0, cam.username)
        self.pass_e.delete(0, "end")
        self.pass_e.insert(0, cam.password)
        self.source_e.set(cam.source_name)
        self.quality_e.set(cam.quality_name)
        self._pending_mac = cam.mac
        self._quality_name = cam.quality_name
        self._reset_ptz_origin()
        self._on_brand_change()
        if (getattr(cam, "brand", "") == "ezviz" or self._brand() == "ezviz") and cam.ip:
            self.after(300, self._refresh_ezviz_states)
        if self._brand() == "ezviz" and cam.ip:
            self.after(300, self._refresh_ezviz_states)

    def _brand(self) -> str:
        return "ezviz" if self.brand_e.get() == "EZVIZ" else "v380"

    def _on_brand_change(self) -> None:
        ez = self._brand() == "ezviz"
        self._pass_lab.configure(text="Verify code" if ez else "Password")
        panel = getattr(self, "_ezviz_panel", None)
        note = getattr(self, "_note_lab", None)
        if panel is not None:
            if ez:
                if note is not None:
                    panel.pack(fill="x", before=note)
                else:
                    panel.pack(fill="x")
            else:
                panel.pack_forget()
        if note is not None:
            note.configure(
                text=(
                    "Hold arrows to pan/tilt.\n"
                    "EZVIZ app features are in the panels above."
                    if ez
                    else "Hold arrows to pan/tilt.\nScroll for light, image, alert."
                )
            )
        if ez:
            if self.port_e.get().strip() in ("", "8800"):
                self.port_e.delete(0, "end")
                self.port_e.insert(0, "554")
            if not self.user_e.get().strip():
                self.user_e.insert(0, "admin")
            self.source_e.set("LAN")
            self._set_status("EZVIZ: video via RTSP · controls via EZVIZ app account")
            # Auto-load ON/OFF + volumes whenever the EZVIZ panel is shown
            if self.ip_e.get().strip():
                self.after(200, self._refresh_ezviz_states)
        else:
            if self.port_e.get().strip() == "554":
                self.port_e.delete(0, "end")
                self.port_e.insert(0, "8800")

    def _read_form(self) -> dict | None:
        brand = self._brand()
        ip = self.ip_e.get().strip()
        user = self.user_e.get().strip() or ("admin" if brand == "ezviz" else "")
        password = self.pass_e.get()
        if brand == "ezviz":
            # EZVIZ verification codes are case-sensitive; sticker codes are uppercase.
            password = password.strip().upper()
            self.pass_e.delete(0, "end")
            self.pass_e.insert(0, password)
        quality_name = self.quality_e.get() or "HD"
        source = "lan" if brand == "ezviz" else ("cloud" if self.source_e.get() == "Cloud" else "lan")
        try:
            port = int(self.port_e.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Port must be a number.")
            return None
        device_id = self.id_e.get().strip()
        if brand == "v380":
            try:
                int(device_id)
            except ValueError:
                messagebox.showerror("Invalid input", "Device ID must be a number for V380.")
                return None
        elif not device_id:
            from v380.client.ezviz_rtsp import synthetic_device_id

            device_id = synthetic_device_id(ip)
            self.id_e.delete(0, "end")
            self.id_e.insert(0, device_id)
        if not user or not password:
            messagebox.showerror(
                "Missing fields",
                "Username and verification code are required." if brand == "ezviz" else "Username and password are required.",
            )
            return None
        if source == "lan" and not ip:
            messagebox.showerror("Missing fields", "Camera IP is required for LAN.")
            return None
        name = self.name_e.get().strip() or str(device_id)
        return {
            "name": name,
            "ip": ip,
            "port": port,
            "device_id": str(device_id),
            "username": user,
            "password": password,
            "source": source,
            "quality": 1 if quality_name == "HD" else 0,
            "quality_name": quality_name,
            "mac": self._pending_mac,
            "brand": brand,
            "rtsp_url": "",
        }

    def _set_status(self, text: str) -> None:
        if not self._alive:
            return
        try:
            if self.winfo_exists():
                self.status.configure(text=text)
        except Exception:
            return

    def _set_codec(self, label: str) -> None:
        self._codec_label = label
        self._set_status(f"Live  {label}  {self._quality_name}")

    def _discover(self) -> None:
        self._set_status("Scanning LAN — V380 (UDP) + EZVIZ (ONVIF / RTSP :554) …")

        def work():
            # Discover must run on a host that shares the camera LAN.
            # Prefer this PC first (Windows UI on same Wi‑Fi), then merge
            # server-side results (Linux box on the camera LAN).
            by_key: dict[str, SimpleNamespace] = {}
            errors: list[str] = []

            def add_dev(*, mac: str, dev_id: str, ip: str, brand: str, rtsp_ready: bool = True) -> None:
                ip = (ip or "").strip()
                mac = (mac or "").strip()
                dev_id = str(dev_id or "").strip()
                # Always key by IP when present so local+server results do not duplicate.
                key = ip or mac or dev_id
                if not key:
                    return
                prev = by_key.get(key)
                if prev is not None and getattr(prev, "brand", "") == "ezviz" and brand != "ezviz":
                    return
                by_key[key] = SimpleNamespace(
                    mac=mac,
                    dev_id=dev_id,
                    ip=ip,
                    brand=brand,
                    rtsp_ready=bool(rtsp_ready) if prev is None else (bool(rtsp_ready) or bool(getattr(prev, "rtsp_ready", False))),
                )

            def add_local(devs, brand_default: str = "v380") -> None:
                for d in devs:
                    brand = str(getattr(d, "brand", "") or brand_default)
                    add_dev(
                        mac=getattr(d, "mac", "") or "",
                        dev_id=str(getattr(d, "dev_id", "") or ""),
                        ip=getattr(d, "ip", "") or "",
                        brand=brand,
                        rtsp_ready=bool(getattr(d, "rtsp_ready", brand != "ezviz")),
                    )

            def add_remote(rows: list[dict]) -> None:
                for d in rows:
                    brand = str(d.get("brand") or "v380")
                    add_dev(
                        mac=d.get("mac") or "",
                        dev_id=str(d.get("device_id") or ""),
                        ip=d.get("ip") or "",
                        brand=brand,
                        rtsp_ready=bool(d.get("rtsp_ready", brand != "ezviz")),
                    )

            try:
                add_local(discover_devices(), "v380")
            except Exception as exc:
                errors.append(f"v380: {exc}")
            try:
                from v380.client.ezviz_rtsp import discover_ezviz_devices

                add_local(discover_ezviz_devices(), "ezviz")
            except Exception as exc:
                errors.append(f"ezviz: {exc}")
            if self._api is not None:
                try:
                    add_remote(self._api.discover())
                except Exception as exc:
                    errors.append(f"server: {exc}")

            # Prefer EZVIZ-first when user already selected that brand.
            prefer_ez = self._brand() == "ezviz"
            devs = sorted(
                by_key.values(),
                key=lambda d: (0 if (getattr(d, "brand", "") == "ezviz") == prefer_ez else 1, d.ip or ""),
            )
            if not devs and errors:
                self._ui(lambda: self._set_status(f"Discover failed: {'; '.join(errors)}"))
                return
            self._devices = devs
            labels = []
            for d in devs:
                tag = "EZVIZ" if getattr(d, "brand", "") == "ezviz" else "V380"
                mac = f"  {d.mac}" if d.mac else ""
                note = ""
                if getattr(d, "brand", "") == "ezviz" and not getattr(d, "rtsp_ready", True):
                    note = "  (RTSP off)"
                labels.append(f"[{tag}] {d.ip}  {d.dev_id}{mac}{note}")
            self._ui(lambda: self._show_devices(labels))

        threading.Thread(target=work, daemon=True).start()

    def _show_devices(self, labels: list[str]) -> None:
        self.cam_box["values"] = labels
        if labels:
            self.cam_box.set(labels[0])
            self._pick_device()
            need_rtsp = any(
                getattr(d, "brand", "") == "ezviz" and not getattr(d, "rtsp_ready", True) for d in self._devices
            )
            if need_rtsp:
                self._set_status(
                    f"Found {len(labels)} camera(s). EZVIZ RTSP is OFF — enable it in app "
                    "(LAN Live View → camera → Local Server Settings → RTSP), then Connect."
                )
            else:
                self._set_status(
                    f"Found {len(labels)} camera(s). For EZVIZ enter the 6-letter verification code, then Connect."
                )
        else:
            self._set_status(
                "No cameras found. EZVIZ: enable RTSP in app (LAN Live View). Same Wi‑Fi as this PC?"
            )

    def _pick_device(self, _evt=None) -> None:
        idx = self.cam_box.current()
        if idx < 0 or idx >= len(self._devices):
            return
        d = self._devices[idx]
        brand = getattr(d, "brand", "") or "v380"
        self.brand_e.set("EZVIZ" if brand == "ezviz" else "V380")
        self._on_brand_change()
        self.ip_e.delete(0, "end")
        self.ip_e.insert(0, d.ip)
        self.id_e.delete(0, "end")
        self.id_e.insert(0, d.dev_id)
        self.user_e.delete(0, "end")
        if brand == "ezviz":
            self.user_e.insert(0, "admin")
        else:
            self.user_e.insert(0, d.dev_id)
        if not self.name_e.get().strip():
            self.name_e.insert(0, d.dev_id)
        self._pending_mac = d.mac
        if brand == "ezviz" and not getattr(d, "rtsp_ready", True):
            self._set_status(
                f"Camera {d.ip} found, but RTSP port 554 is closed. Enable RTSP in EZVIZ app, then Connect."
            )

    def _apply_ptz_mode(self) -> None:
        mode = self._ptz_mode.get() if hasattr(self, "_ptz_mode") else "both"
        if hasattr(self, "_joy_wrap"):
            self._joy_wrap.pack_forget()
        if hasattr(self, "_btn_wrap"):
            self._btn_wrap.pack_forget()
        anchor = getattr(self, "_zoom_row", None)
        if mode in ("joy", "both") and hasattr(self, "_joy_wrap"):
            if anchor is not None:
                self._joy_wrap.pack(pady=(0, 6), before=anchor)
            else:
                self._joy_wrap.pack(pady=(0, 6))
        if mode in ("buttons", "both") and hasattr(self, "_btn_wrap"):
            if anchor is not None:
                self._btn_wrap.pack(pady=(0, 4), before=anchor)
            else:
                self._btn_wrap.pack(pady=(0, 4))
        try:
            self._controls_canvas.configure(scrollregion=self._controls_canvas.bbox("all"))
        except Exception:
            pass

    def _request_keyframe(self) -> None:
        """After PTZ stops, H.265 often needs a fresh I-frame or the picture goes gray."""
        self._need_keyframe = True

    def _joy_dir(self, name: str) -> None:
        self._cmd(name, resync=False)

    def _joy_stop(self) -> None:
        self._cmd("ptz_stop", resync=True)

    def _ptz_ready(self) -> bool:
        if self._brand() == "ezviz":
            return bool(self.ip_e.get().strip())
        if self._api is not None and self._cam_id:
            return True
        client = self._client
        return client is not None and hasattr(client, "send_control")

    def _ensure_ezviz_cloud(self) -> bool:
        if self._ezviz_ready:
            return True
        from v380.client import ezviz_session as ez
        from v380.ui.ezviz_account import ask_ezviz_account

        try:
            ez.get_client()
            self._ezviz_ready = True
            if self._brand() == "ezviz" and self.ip_e.get().strip():
                self.after(100, self._refresh_ezviz_states)
            return True
        except Exception:
            pass
        creds = ask_ezviz_account(self)
        if not creds or not creds[0] or not creds[1]:
            return False
        acc, pw, sms = creds
        try:
            ez.login(acc, pw, sms or None)
            self._ezviz_ready = True
            self._set_status("EZVIZ account signed in — loading feature states…")
            if self.ip_e.get().strip():
                self.after(100, self._refresh_ezviz_states)
            return True
        except Exception as exc:
            msg = str(exc)
            if "MFA" in msg or "verification" in msg.lower() or "retry with code" in msg.lower():
                from tkinter import simpledialog

                code = simpledialog.askstring("EZVIZ 2FA", "Enter the SMS code sent by EZVIZ:", parent=self)
                if code:
                    try:
                        ez.login(acc, pw, code)
                        self._ezviz_ready = True
                        self._set_status("EZVIZ account signed in")
                        if self.ip_e.get().strip():
                            self.after(100, self._refresh_ezviz_states)
                        return True
                    except Exception as exc2:
                        messagebox.showerror("EZVIZ account", str(exc2))
                        return False
            messagebox.showerror("EZVIZ account", msg)
            return False

    def _ezviz_cloud_blocking(self) -> bool:
        if self._ezviz_ready:
            return True
        done = threading.Event()
        ok = [False]

        def run() -> None:
            try:
                ok[0] = self._ensure_ezviz_cloud()
            finally:
                done.set()

        self.after(0, run)
        done.wait(120)
        return bool(ok[0])

    def _send_ezviz_cmd(self, name: str, hold: float = 0.0) -> bool:
        from v380.client import ezviz_session as ez

        ip = self.ip_e.get().strip()
        if not ip:
            return False
        if name.startswith("ptz_"):
            if name == "ptz_stop":
                last = (self._ptz_last_cmd or "up").replace("ptz_", "").upper()
                dirs = ["UP", "DOWN", "LEFT", "RIGHT"]
                if last in dirs:
                    dirs.insert(0, last)
                seen: set[str] = set()
                for d in dirs:
                    if d in seen:
                        continue
                    seen.add(d)
                    try:
                        ez.ptz(ip, d, "STOP")
                    except Exception:
                        pass
                return True
            d = name.replace("ptz_", "").upper()
            if d not in ("UP", "DOWN", "LEFT", "RIGHT"):
                return False
            ez.ptz(ip, d, "START")
            if hold > 0.05:
                time.sleep(hold)
                ez.ptz(ip, d, "STOP")
            return True
        # light / image / alert aliases + full app feature set
        ez.run_action(ip, name)
        return True

    def _send_ezviz_ptz(self, name: str, hold: float = 0.0) -> bool:
        return self._send_ezviz_cmd(name, hold=hold)

    def _send_control_sync(self, name: str, hold: float = 0.0) -> bool:
        """Prefer a live TCP client with send_control; otherwise studio API (optional hold)."""
        try:
            if self._brand() == "ezviz":
                return self._send_ezviz_cmd(name, hold=hold)
            client = self._client
            hold = float(hold or 0)
            if client is not None and hasattr(client, "send_control"):
                if hold > 0.05:
                    ok = bool(client.send_control(name))
                    if not ok:
                        return False
                    deadline = time.time() + hold
                    while time.time() < deadline and not self._stop.is_set():
                        time.sleep(min(0.28, max(0.0, deadline - time.time())))
                        if time.time() < deadline:
                            client.send_control(name)
                    client.send_control("ptz_stop")
                    return True
                return bool(client.send_control(name))
            if self._api is not None and self._cam_id:
                return bool(self._api.command(self._cam_id, name, hold=hold))
        except Exception:
            return False
        return False

    def _ptz_track(self, name: str) -> None:
        now = time.time()
        if self._ptz_last_cmd and self._ptz_last_t is not None:
            dt = max(0.0, now - self._ptz_last_t)
            self._apply_ptz_delta(self._ptz_last_cmd, dt)
        if name == "ptz_stop":
            self._ptz_last_cmd = None
            self._ptz_last_t = None
        else:
            self._ptz_last_cmd = name
            self._ptz_last_t = now

    def _apply_ptz_delta(self, name: str, dt: float) -> None:
        if dt <= 0:
            return
        if name == "ptz_right":
            self._ptz_pan += dt
        elif name == "ptz_left":
            self._ptz_pan -= dt
        elif name == "ptz_up":
            self._ptz_tilt += dt
        elif name == "ptz_down":
            self._ptz_tilt -= dt
        elif name == "ptz_zoom_in":
            self._ptz_zoom_pos += dt
        elif name == "ptz_zoom_out":
            self._ptz_zoom_pos -= dt

    def _reset_ptz_origin(self) -> None:
        self._ptz_pan = 0.0
        self._ptz_tilt = 0.0
        self._ptz_zoom_pos = 0.0
        self._ptz_last_cmd = None
        self._ptz_last_t = None

    def _hold_move(self, name: str, seconds: float, *, track: bool = True) -> bool:
        """Hold a direction with server-side keepalive (same as a long joystick press)."""
        if seconds <= 0.05:
            return True
        ok = self._send_control_sync(name, hold=float(seconds))
        if ok and track:
            self._apply_ptz_delta(name, seconds)
        return ok

    def _calibrate_ptz(self) -> None:
        if not self._ptz_ready():
            self._set_status("Connect first, then Calibrate")
            return
        if self._ptz_busy:
            self._set_status("PTZ is busy — wait for the current move to finish")
            return
        if not messagebox.askyesno(
            "Calibrate PTZ",
            "One fast pass to the limits:\n"
            "  LEFT → RIGHT → UP → DOWN\n\n"
            "Same as holding the joystick. Start?",
        ):
            return
        self._ptz_busy = True
        self._set_status("Calibrating… left")

        def work() -> None:
            ok = False
            try:
                if self._brand() == "ezviz" and not self._ezviz_cloud_blocking():
                    self._ui(lambda: self._set_status("Sign in with EZVIZ app account to calibrate"))
                    self._ptz_busy = False
                    return
                # Single-start continuous moves (identical to joystick hold).
                # Durations long enough to hit mechanical stops at full motor speed.
                steps = (
                    ("ptz_left", 8.0),
                    ("ptz_right", 8.0),
                    ("ptz_up", 5.0),
                    ("ptz_down", 5.0),
                )
                for cmd, secs in steps:
                    if self._stop.is_set():
                        break
                    label = cmd.replace("ptz_", "")
                    self._ui(lambda l=label: self._set_status(f"Calibrating… {l}"))
                    if self._hold_move(cmd, secs, track=False):
                        ok = True
                    else:
                        self._ui(lambda: self._set_status("Calibrate failed — reconnect live view"))
                        break
                    time.sleep(0.2)
                self._send_control_sync("ptz_stop")
                self._reset_ptz_origin()
                self._request_keyframe()
            except Exception as exc:
                self._ui(lambda: self._set_status(f"Calibrate failed — {exc}"))
                self._ptz_busy = False
                return
            self._ptz_busy = False
            self._ui(
                lambda: self._set_status(
                    "Calibration done — origin reset"
                    if ok
                    else "PTZ calibration failed — reconnect and retry"
                )
            )

        threading.Thread(target=work, daemon=True, name="ptz-calibrate").start()

    def _cmd(self, name: str, resync: bool | None = None) -> None:
        # Only resync after movement ends — resyncing on every tick causes lag/gray.
        if resync is None:
            resync = name == "ptz_stop"
        if resync and name.startswith("ptz_"):
            self._request_keyframe()
        self._ptz_track(name)
        if name != "ptz_stop":
            self._set_status(name.replace("_", " "))

        def work() -> None:
            try:
                if self._brand() == "ezviz" and not self._ezviz_cloud_blocking():
                    self._ui(lambda: self._set_status("Sign in with EZVIZ app email/password to move / light / alert"))
                    return
                ok = self._send_control_sync(name)
                if name != "ptz_stop" and not ok:
                    if self._brand() == "ezviz":
                        self._ui(
                            lambda: self._set_status(
                                "Control failed — check EZVIZ account login and that this camera is on that account"
                            )
                        )
                    else:
                        self._ui(lambda: self._set_status(f"Control failed: {name}"))
            except Exception as exc:
                self._ui(lambda: self._set_status(f"Control failed: {exc}"))

        # Fire immediately — do not wait behind other UI jobs.
        threading.Thread(target=work, daemon=True, name="ptz-cmd").start()

    def _toggle_alert_wav(self) -> None:
        """Play / stop only audio/alert.wav (not the EZVIZ cloud siren)."""
        if getattr(self, "_alert_wav_playing", False):
            self._stop_alert_wav()
            return
        self._play_alert_wav()

    def _play_alert_wav(self) -> None:
        from v380.client.extras import ALERT_WAV

        if not ALERT_WAV.is_file():
            self._set_status(f"Missing file: {ALERT_WAV}")
            return

        self._alert_wav_playing = True
        btn = getattr(self, "_alert_wav_btn", None)
        if btn is not None:
            btn.configure(text="Stop camera sound", bg=RED)

        ip = self.ip_e.get().strip()
        brand = self._brand()

        def work() -> None:
            try:
                if brand == "ezviz":
                    if not self._ezviz_cloud_blocking():
                        self._ui(lambda: self._set_status("Sign in with EZVIZ app account first"))
                        self._ui(self._alert_wav_btn_reset)
                        return
                    from v380.client.ezviz_alert_wav import play_alert_on_camera

                    msg = play_alert_on_camera(ip, ALERT_WAV)
                    self._ezviz_siren_local = "siren" in msg.lower() or "Playing" in msg
                    self._ui(lambda m=msg: self._set_status(m))
                    # auto-stop siren fallback after ~12s
                    if "siren ON" in msg:
                        self._ui(lambda: self.after(12000, self._stop_alert_wav))
                    return

                # V380 (or API talk client): send WAV through camera talk channel only
                client = self._client
                if client is None or not hasattr(client, "send_talk_audio"):
                    self._ui(lambda: self._set_status("Connect the camera first to play on its speaker"))
                    self._ui(self._alert_wav_btn_reset)
                    return
                ok = bool(self._alert.start(client))
                if not ok:
                    self._ui(lambda: self._set_status("Camera talk failed — cannot play alert.wav on speaker"))
                    self._ui(self._alert_wav_btn_reset)
                    return
                self._ui(lambda: self._set_status("Playing audio/alert.wav on camera speaker"))
                if self._alert_job is not None:
                    try:
                        self.after_cancel(self._alert_job)
                    except Exception:
                        pass
                self._ui(lambda: setattr(self, "_alert_job", self.after(60000, self._stop_alert_wav)))
            except Exception as exc:
                self._ui(lambda m=str(exc): self._set_status(f"Play failed: {m}"))
                self._ui(self._alert_wav_btn_reset)

        threading.Thread(target=work, daemon=True, name="alert-wav-cam").start()

    def _alert_wav_btn_reset(self) -> None:
        self._alert_wav_playing = False
        btn = getattr(self, "_alert_wav_btn", None)
        if btn is not None:
            btn.configure(text="Play alert.wav on camera", bg=ORANGE)

    def _alert_wav_btn_idle_if_pc_only(self) -> None:
        self._alert_wav_btn_reset()

    def _stop_alert_wav(self) -> None:
        self._alert_wav_playing = False
        if self._alert_job is not None:
            try:
                self.after_cancel(self._alert_job)
            except Exception:
                pass
            self._alert_job = None
        try:
            self._alert.stop()
        except Exception:
            pass
        if self._brand() == "ezviz":
            ip = self.ip_e.get().strip()

            def work() -> None:
                try:
                    from v380.client.ezviz_alert_wav import stop_alert_on_camera

                    stop_alert_on_camera(ip)
                except Exception:
                    pass
                self._ezviz_siren_local = False

            threading.Thread(target=work, daemon=True, name="alert-wav-stop").start()
        btn = getattr(self, "_alert_wav_btn", None)
        if btn is not None:
            btn.configure(text="Play alert.wav on camera", bg=ORANGE)
        self._set_status("Camera alert sound stopped")

    def _alert_on(self) -> None:
        if self._brand() == "ezviz":
            if not self._ensure_ezviz_cloud():
                return
            ip = self.ip_e.get().strip()
            self._set_status("Alert ON")
            if self._alert_job is not None:
                try:
                    self.after_cancel(self._alert_job)
                except Exception:
                    pass
            self._alert_job = self.after(20000, self._alert_auto_off)

            def work() -> None:
                try:
                    from v380.client.ezviz_session import sound_alarm

                    sound_alarm(ip, True)
                    self._ezviz_siren_local = True
                    st = dict(getattr(self, "_ezviz_states", {}) or {})
                    st["siren"] = True
                    self._ui(lambda s=st: self._apply_ezviz_states(s))
                    self._ui(lambda: self._set_status("Siren / alert ON"))
                except Exception as exc:
                    self._ui(lambda m=str(exc): self._set_status(f"Alert failed: {m}"))

            threading.Thread(target=work, daemon=True, name="ezviz-alert").start()
            return
        if self._api is not None and self._cam_id:
            cam_id = self._cam_id
            self._set_status("Alert ON")
            if self._alert_job is not None:
                try:
                    self.after_cancel(self._alert_job)
                except Exception:
                    pass
            self._alert_job = self.after(20000, self._alert_auto_off)
            self._bg(lambda: self._api.alert(cam_id, True))
            return
        if self._client is None:
            self._set_status("Connect first, then Alert")
            return
        if self._alert.start(self._client):
            if self._alert.using_file:
                self._set_status("Alert ON — playing audio/alert.wav")
            else:
                self._set_status("Alert ON — beep (put a WAV in audio/alert.wav for a voice)")
        else:
            self._set_status("Alert failed — connect first")
            return
        if self._alert_job is not None:
            try:
                self.after_cancel(self._alert_job)
            except Exception:
                pass
        self._alert_job = self.after(20000, self._alert_auto_off)

    def _alert_off(self) -> None:
        if self._alert_job is not None:
            try:
                self.after_cancel(self._alert_job)
            except Exception:
                pass
            self._alert_job = None
        if self._api is not None and self._cam_id:
            cam_id = self._cam_id
            self._bg(lambda: self._api.alert(cam_id, False))
        if self._brand() == "ezviz":
            ip = self.ip_e.get().strip()

            def work() -> None:
                try:
                    from v380.client.ezviz_session import sound_alarm

                    sound_alarm(ip, False)
                    self._ezviz_siren_local = False
                    st = dict(getattr(self, "_ezviz_states", {}) or {})
                    st["siren"] = False
                    self._ui(lambda s=st: self._apply_ezviz_states(s))
                except Exception:
                    pass

            threading.Thread(target=work, daemon=True, name="ezviz-alert-off").start()
        self._alert.stop()
        self._alert_wav_btn_reset()
        self._set_status("Alert off")

    def _alert_auto_off(self) -> None:
        self._alert_job = None
        if self._alive:
            if self._api is not None and self._cam_id:
                cam_id = self._cam_id
                self._bg(lambda: self._api.alert(cam_id, False))
            self._alert.stop()
            if self._brand() == "ezviz":
                ip = self.ip_e.get().strip()
                try:
                    from v380.client.ezviz_session import sound_alarm

                    sound_alarm(ip, False)
                except Exception:
                    pass
            self._set_status("Alert off")

    def _set_zoom(self, value: float) -> None:
        self._view_zoom = min(4.0, max(1.0, round(value, 2)))
        self._set_status(f"Zoom {self._view_zoom:.0%}")

    def _zoom_hold(self, cmd: str, step: float) -> None:
        # Digital zoom always works; hardware zoom only when the camera exposes PTZ.
        self._set_zoom(self._view_zoom + step)
        if self._brand() == "ezviz":
            return
        self._cmd(cmd)

    def _zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def _on_wheel(self, evt) -> None:
        step = 0.25 if getattr(evt, "delta", 0) > 0 else -0.25
        self._set_zoom(self._view_zoom + step)

    def _toggle_dual(self) -> None:
        if self._brand() == "ezviz":
            self._set_status("Dual view is for V380 dual-lens cameras only")
            return
        if self._dual_mode == "off":
            self._set_dual_mode("split")
        else:
            self._set_dual_mode("off")

    def _on_single_click(self, _evt=None) -> None:
        # From a full single lens, go back to dual split when dual is active.
        if self._dual_mode in ("top", "bottom"):
            self._set_dual_mode("split")

    def _set_dual_mode(self, mode: str) -> None:
        if mode not in ("off", "split", "top", "bottom"):
            mode = "off"
        self._dual_mode = mode
        self._view_zoom = 1.0
        if mode == "split":
            self.canvas.pack_forget()
            self._split.pack(fill="both", expand=True)
            self.dual_btn.configure(text="Dual ON", bg=GREEN)
            self.both_btn.grid()
            self._set_status("Dual view — click a lens for full screen (PTZ moves the bottom lens)")
        elif mode in ("top", "bottom"):
            self._split.pack_forget()
            self.canvas.pack(fill="both", expand=True)
            self.dual_btn.configure(text="Dual ON", bg=GREEN)
            self.both_btn.grid()
            which = "Fixed lens" if mode == "top" else "PTZ lens"
            self._set_status(f"{which} full view — click video or Both lenses to go back")
        else:
            self._split.pack_forget()
            self.canvas.pack(fill="both", expand=True)
            self.dual_btn.configure(text="Dual view", bg="#243040")
            self.both_btn.grid_remove()
            self._set_status("Full dual stream (both lenses in one picture)")

    @staticmethod
    def _lens_crop(img: Image.Image, which: str) -> Image.Image:
        w, h = img.size
        mid = h // 2
        if which == "top":
            return img.crop((0, 0, w, mid))
        if which == "bottom":
            return img.crop((0, mid, w, h))
        return img

    def _toggle_listen(self) -> None:
        self._player.enabled = not self._player.enabled
        self.listen_btn.configure(text="Listen ON" if self._player.enabled else "Listen OFF", bg=GREEN if self._player.enabled else "#243040")
        if self._player.enabled:
            if self._brand() == "ezviz":
                ip = self.ip_e.get().strip()
                self._set_status("Listen ON — camera microphone through speakers")

                def work() -> None:
                    try:
                        if self._ezviz_cloud_blocking():
                            from v380.client.ezviz_session import enable_mic

                            enable_mic(ip, True)
                    except Exception:
                        pass

                threading.Thread(target=work, daemon=True, name="ezviz-listen").start()
            else:
                ac = "IMA ADPCM" if self._client and getattr(self._client, "audio_codec", "") == "ima" else "G.711"
                self._set_status(f"Listen ON ({ac}, 8 kHz) — use PC speakers")
        else:
            self._set_status("Listen off")

    def _toggle_talk(self) -> None:
        if self._brand() == "ezviz":
            if not self._ensure_ezviz_cloud():
                return
            ip = self.ip_e.get().strip()
            turning_on = self.talk_btn.cget("text") != "Talk ON"
            if turning_on:
                self.talk_btn.configure(text="Talk ON", bg=GREEN)
                if not self._player.enabled:
                    self._toggle_listen()
                self._set_status("Talk: enables camera mic path. Use Alert / Siren for alarm sound — Talk does not sound the siren.")

                def work() -> None:
                    try:
                        from pyezviz.constants import DeviceSwitchType

                        from v380.client.ezviz_session import set_switch

                        try:
                            set_switch(ip, DeviceSwitchType.SOUND.value, True)
                        except Exception:
                            pass
                        try:
                            set_switch(ip, DeviceSwitchType.DOORBELL_TALK.value, True)
                        except Exception:
                            pass
                    except Exception as exc:
                        self._ui(lambda m=str(exc): self._set_status(f"Talk failed: {m}"))

                threading.Thread(target=work, daemon=True, name="ezviz-talk").start()
            else:
                self.talk_btn.configure(text="Talk OFF", bg="#243040")
                self._set_status("Talk off")

                def work() -> None:
                    try:
                        from pyezviz.constants import DeviceSwitchType

                        from v380.client.ezviz_session import set_switch

                        try:
                            set_switch(ip, DeviceSwitchType.DOORBELL_TALK.value, False)
                        except Exception:
                            pass
                    except Exception:
                        pass

                threading.Thread(target=work, daemon=True, name="ezviz-talk-off").start()
            return
        if self._talker.enabled:
            self._talker.enabled = False
            self._bg(self._talker.stop)
            self.talk_btn.configure(text="Talk OFF", bg="#243040")
            self._set_status("Talk off")
            return
        if self._api is not None and self._cam_id:
            self._talker.attach(ApiTalkClient(self._api, self._cam_id))
        elif self._client is None:
            self._set_status("Connect first, then Talk")
            return
        else:
            self._talker.attach(self._client)
        self.talk_btn.configure(text="Talk ON", bg=GREEN)
        self._set_status("Talk ON — speak into the PC microphone")
        def work() -> None:
            if self._talker.start():
                return
            err = self._talker.error or "unknown error"
            def fail() -> None:
                self.talk_btn.configure(text="Talk OFF", bg="#243040")
                self._set_status(f"Talk failed — {err}")
                low = err.lower()
                if any(w in low for w in ("blocked", "denied", "privacy", "access is denied")):
                    if messagebox.askyesno(
                        "Microphone blocked",
                        "Windows is blocking the microphone, so there is no Allow button in this app.\n\n"
                        "Turn ON both of these:\n"
                        "• Microphone access\n"
                        "• Let desktop apps access your microphone\n\n"
                        "Open Windows microphone settings now?",
                    ):
                        Talker.open_mic_settings()
                elif any(w in low for w in ("device -1", "querying device", "no working microphone", "no default")):
                    if messagebox.askyesno(
                        "No microphone",
                        "Windows has no usable microphone (device -1).\n\n"
                        "Check:\n"
                        "• A mic is plugged in / enabled\n"
                        "• Sound settings → Input has a default device\n"
                        "• Privacy → Microphone allows desktop apps\n\n"
                        "Open Sound settings now?",
                    ):
                        Talker.open_sound_settings()
                else:
                    messagebox.showerror("Talk failed", err)
            self._ui(fail)
        self._bg(work)

    def _snapshot(self) -> None:
        if self._last_rgb is not None and not self._last_jpeg:
            try:
                import cv2

                bgr = cv2.cvtColor(self._last_rgb, cv2.COLOR_RGB2BGR)
                ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                if ok:
                    self._last_jpeg = enc.tobytes()
            except Exception:
                pass
        if not self._last_jpeg and self._last_rgb is None:
            self._set_status("No frame yet. Connect first.")
            return
        REC_DIR.mkdir(parents=True, exist_ok=True)
        path = REC_DIR / time.strftime("snap_%Y%m%d_%H%M%S.jpg")
        data = self._last_jpeg
        if data is None and self._last_rgb is not None:
            try:
                import cv2

                bgr = cv2.cvtColor(self._last_rgb, cv2.COLOR_RGB2BGR)
                ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                data = enc.tobytes() if ok else None
            except Exception:
                data = None
        if not data:
            self._set_status("No frame yet. Connect first.")
            return
        if self._dual_mode in ("top", "bottom"):
            try:
                if self._last_rgb is not None:
                    img = self._lens_crop(Image.fromarray(self._last_rgb), self._dual_mode)
                else:
                    img = self._lens_crop(Image.open(io.BytesIO(data)), self._dual_mode)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=92)
                data = buf.getvalue()
            except Exception:
                pass
        path.write_bytes(data)
        self._set_status(f"Snapshot saved  {path}")

    def _toggle_record(self) -> None:
        if self._brand() == "ezviz":
            self._toggle_ezviz_record()
            return
        if self._recorder.active:
            out = self._recorder.stop()
            self.rec_btn.configure(text="Record", bg=RED)
            self._set_status(f"Recording saved  {out}" if out else "Recording stopped (empty)")
        else:
            if self._client is None:
                self._set_status("Connect first, then Record")
                return
            fmt = self._client.video_codec if self._client is not None else "h264"
            path = self._recorder.start(fmt)
            self.rec_btn.configure(text="Stop rec", bg="#7f1d1d")
            self._set_status(f"Recording…  {path.name}")

    def _toggle_ezviz_record(self) -> None:
        from v380.client.ezviz_rtsp import EzvizRtspRecorder

        if self._ezviz_recorder is not None and self._ezviz_recorder.active:
            out = self._ezviz_recorder.stop()
            self._ezviz_recorder = None
            self.rec_btn.configure(text="Record", bg=RED)
            self._set_status(f"Recording saved  {out}" if out else "Recording stopped (empty)")
            return
        client = self._client
        url = getattr(client, "url", "") if client is not None else ""
        if not url:
            self._set_status("Connect first, then Record")
            return
        try:
            rec = EzvizRtspRecorder(REC_DIR)
            path = rec.start(url)
            self._ezviz_recorder = rec
            self.rec_btn.configure(text="Stop rec", bg="#7f1d1d")
            self._set_status(f"Recording EZVIZ RTSP…  {path.name}")
        except Exception as exc:
            self._set_status(f"Record failed: {exc}")

    def _open_folder(self) -> None:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(REC_DIR)

    def _toggle(self) -> None:
        if self._thread and self._thread.is_alive():
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        fields = self._read_form()
        if fields is None:
            return
        brand = fields.get("brand") or "v380"
        ip = fields["ip"]
        user = fields["username"]
        password = fields["password"]
        quality_name = fields["quality_name"]
        source = fields["source"]
        quality = fields["quality"]
        port = fields["port"]
        device_raw = str(fields["device_id"])

        self._stop.clear()
        self._quality_name = quality_name
        self.connect_btn.configure(text="Disconnect", bg=RED)
        label = "EZVIZ" if brand == "ezviz" else f"{source.upper()} {quality_name}"
        self._set_status(f"Connecting ({label}) …")

        def worker() -> None:
            if self._api is not None:
                self._connect_remote(fields)
                return
            if brand == "ezviz":
                self._connect_local_ezviz(fields)
                return
            device_id = int(device_raw)
            while not self._stop.is_set():
                client = None
                decoder = None
                try:
                    host = ip
                    if source == "cloud":
                        self._ui( lambda: self._set_status("Looking up cloud relay …"))
                        host = get_relay_ip(device_id)
                        if not host:
                            raise RuntimeError("No reachable cloud relay")
                        self._ui( lambda h=host: self._set_status(f"Relay {h}"))
                    client = V380SnapshotClient(host, device_id, user, password, port, quality=quality, source=source)
                    client.connect()
                    self._client = client
                    self._talker.attach(client)
                    if not self._saved_once and self._on_connected is not None:
                        self._saved_once = True
                        self._ui( lambda f=fields: self._on_connected(f))
                    decoder = None
                    got_key = False
                    for kind, is_iframe, payload in client.iter_video_frames(self._stop):
                        if kind == "audio":
                            self._player.play(payload, client.audio_codec)
                            continue
                        if self._recorders is not None and self._preset is not None:
                            try:
                                self._recorders.on_video(self._preset, is_iframe, payload, client)
                            except Exception:
                                pass
                        if decoder is None:
                            decoder = make_live_decoder(
                                self._frames, fmt=client.video_codec, scale_width=0, jpeg_q=3, threads=2
                            )
                            self._decoder = decoder
                            codec = "H.265" if client.video_codec == "hevc" else "H.264"
                            self._ui( lambda c=codec: self._set_codec(c))
                        if is_iframe:
                            got_key = True
                            if self._need_keyframe:
                                # Restart decoder so PTZ motion does not leave a gray ghost picture.
                                try:
                                    decoder.close()
                                except Exception:
                                    pass
                                decoder = make_live_decoder(
                                    self._frames, fmt=client.video_codec, scale_width=0, jpeg_q=3, threads=2
                                )
                                self._decoder = decoder
                                self._need_keyframe = False
                        if not got_key:
                            continue
                        if self._need_keyframe and not is_iframe:
                            continue
                        decoder.write_frame(bool(is_iframe), payload, client._sps, client._pps, client._vps)
                        if self._recorder.active:
                            rec = payload
                            if is_iframe:
                                rec = client.h264_for_decode(payload)
                            self._recorder.write(rec)
                    break
                except Exception as exc:
                    msg = str(exc)
                    if self._stop.is_set():
                        break
                    if "invalid username" in msg or "invalid password" in msg or "invalid device id" in msg:
                        self._ui( lambda m=msg: self._fail(m))
                        break
                    self._ui( lambda m=msg: self._set_status(f"Reconnecting… ({m})"))
                    time.sleep(1.5)
                finally:
                    if decoder is not None:
                        decoder.close()
                    if self._decoder is decoder:
                        self._decoder = None
                    self._talker.stop()
                    self._ui(lambda: self.talk_btn.configure(text="Talk OFF", bg="#243040"))
                    if client is not None:
                        client.close()
                    if self._client is client:
                        self._client = None

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _connect_local_ezviz(self, fields: dict) -> None:
        from v380.client.ezviz_rtsp import EzvizRtspClient, build_rtsp_url

        url = build_rtsp_url(
            fields["ip"],
            fields["password"],
            username=fields["username"],
            port=int(fields["port"]),
        )
        while not self._stop.is_set():
            client = None
            try:
                client = EzvizRtspClient(
                    url,
                    ip=fields["ip"],
                    username=fields["username"],
                    password=fields["password"],
                )
                client.set_audio_callback(lambda pcm, rate=8000: self._player.play_pcm(pcm, rate=rate))
                client.connect()
                self._client = client
                if not self._saved_once and self._on_connected is not None:
                    self._saved_once = True
                    self._ui(lambda f=fields: self._on_connected(f))
                self._ui(lambda: self._set_codec("JPEG / RTSP"))
                self._ui(lambda: self._ensure_ezviz_cloud())
                # _ensure_ezviz_cloud already schedules a state refresh when IP is set
                self._ui(
                    lambda: self._set_status(
                        "EZVIZ live — toggles auto-refresh after sign-in. Use STOP ALL ALARMS if it beeps."
                    )
                )
                for kind, _is_iframe, payload in client.iter_video_frames(self._stop):
                    if kind != "video" or not payload:
                        continue
                    self._push_jpeg(payload)
                break
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._ui(lambda m=str(exc): self._set_status(f"Reconnecting… ({m})"))
                time.sleep(1.5)
            finally:
                if client is not None:
                    client.close()
                if self._client is client:
                    self._client = None

    def _connect_remote(self, fields: dict) -> None:
        try:
            self._ui(lambda: self._set_status("Checking camera password…"))
            probe = self._api.probe_camera(fields)
            if not probe.get("online"):
                raise RuntimeError(self._probe_error(probe))
            if not self._cam_id and self._on_connected is not None:
                saved = self._on_connected(fields)
                self._cam_id = int(getattr(saved, "id", 0) or 0)
                self._saved_once = True
            elif self._cam_id and self._on_connected is not None:
                # Refresh saved credentials only after a successful login.
                self._on_connected({**fields, "id": self._cam_id})
                self._saved_once = True
            if not self._cam_id:
                raise RuntimeError("Camera was not saved on the server")
            self._ui(lambda: self._set_status("Live from server…"))
            self._pump_stream(self._cam_id)
        except Exception as exc:
            if not self._stop.is_set():
                self._ui(lambda m=str(exc): self._fail(m))
        finally:
            if not self._stop.is_set() and self._alive:
                self._ui(lambda: self.connect_btn.configure(text="Connect", bg=ACCENT))

    @staticmethod
    def _probe_error(probe: dict) -> str:
        err = str(probe.get("error") or "").strip()
        low = err.lower()
        if "invalid password" in low:
            return "Invalid password"
        if "invalid username" in low:
            return "Invalid username"
        if "invalid device" in low:
            return "Invalid device ID"
        if "rtsp port" in low and "closed" in low:
            return err
        if "enable" in low and "rtsp" in low:
            return err
        if str(probe.get("brand") or "").lower() == "ezviz" or "rtsp" in low:
            if probe.get("reachable") and not probe.get("online"):
                return err or "EZVIZ online but RTSP failed — enable RTSP in EZVIZ app"
            if not probe.get("reachable"):
                return f"EZVIZ unreachable — {err or 'check IP / Wi‑Fi'}"
        if not probe.get("reachable"):
            return f"Camera offline — {err or 'unreachable'}"
        return f"Login failed — {err or 'unknown error'}"

    def _push_jpeg(self, jpeg: bytes) -> None:
        if not jpeg:
            return
        self._last_jpeg = jpeg
        self._last_rgb = None
        try:
            self._frames.put_nowait(jpeg)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(jpeg)
            except queue.Full:
                pass

    def _frame_to_image(self, frame) -> Image.Image | None:
        try:
            import numpy as np

            if isinstance(frame, np.ndarray):
                arr = frame
                if arr.ndim == 3 and arr.shape[2] >= 3:
                    return Image.fromarray(arr[:, :, :3].astype("uint8", copy=False))
                return None
        except Exception:
            pass
        if isinstance(frame, (bytes, bytearray, memoryview)):
            return self._jpeg_to_image(bytes(frame))
        return None

    def _pump_h264(self, cam_id: int) -> bool:
        try:
            resp = self._api.open_h264(cam_id)
        except Exception:
            return False
        decoder = None
        codec = "h264"
        try:
            for kind, a, b in self._api.iter_h264(resp):
                if self._stop.is_set():
                    break
                if kind == "codec":
                    codec = str(a)
                    if decoder is not None:
                        decoder.close()
                    decoder = make_live_decoder(self._frames, fmt=codec, scale_width=0, jpeg_q=3, threads=2)
                    self._decoder = decoder
                    label = "H.265" if a == "hevc" else "H.264"
                    self._ui(lambda c=label: self._set_codec(c))
                    continue
                is_iframe = bool(a)
                if self._need_keyframe and not is_iframe:
                    continue
                if self._need_keyframe and is_iframe:
                    if decoder is not None:
                        try:
                            decoder.close()
                        except Exception:
                            pass
                    decoder = make_live_decoder(self._frames, fmt=codec, scale_width=0, jpeg_q=3, threads=2)
                    self._decoder = decoder
                    self._need_keyframe = False
                if decoder is None:
                    decoder = make_live_decoder(self._frames, fmt=codec, scale_width=0, jpeg_q=3, threads=2)
                    self._decoder = decoder
                decoder.write_frame(is_iframe, b, None, None, None)
            return True
        except Exception:
            return False
        finally:
            try:
                resp.close()
            except Exception:
                pass
            if decoder is not None:
                decoder.close()
                if self._decoder is decoder:
                    self._decoder = None

    def _pump_stream(self, cam_id: int) -> None:
        while not self._stop.is_set():
            # EZVIZ Hub sessions are JPEG/MJPEG only — skip H.264 pump.
            if self._brand() != "ezviz" and self._pump_h264(cam_id):
                if self._stop.is_set():
                    break
                time.sleep(0.05)
                continue
            try:
                resp = self._api.open_mjpeg(cam_id)
            except Exception:
                jpeg = self._api.snapshot(cam_id)
                if jpeg:
                    self._push_jpeg(jpeg)
                time.sleep(0.04)
                continue
            try:
                for jpeg in self._api.iter_mjpeg(resp):
                    if self._stop.is_set():
                        break
                    self._push_jpeg(jpeg)
            except Exception:
                if self._stop.is_set():
                    break
                time.sleep(0.05)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

    def _fail(self, msg: str) -> None:
        self._stop.set()
        self.connect_btn.configure(text="Connect", bg=ACCENT)
        low = msg.lower()
        if "invalid password" in low:
            msg = "Invalid password"
        elif "invalid username" in low:
            msg = "Invalid username"
        elif "invalid device" in low:
            msg = "Invalid device ID"
        self._set_status(f"Error: {msg}")
        messagebox.showerror("Camera", msg)

    def _disconnect(self) -> None:
        self._stop.set()
        if self._ezviz_recorder is not None and self._ezviz_recorder.active:
            self._toggle_ezviz_record()
        elif self._recorder.active:
            self._toggle_record()
        if self._client is not None:
            try:
                self._alert.stop()
            except Exception:
                pass
        self._talker.stop()
        self.talk_btn.configure(text="Talk OFF", bg="#243040")
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
        self.connect_btn.configure(text="Connect", bg=ACCENT)
        self._set_status("Disconnected")
        self._dual_checked = False
        if self._dual_mode != "off":
            self._set_dual_mode("off")

    @staticmethod
    def _looks_corrupt_gray(img: Image.Image) -> bool:
        """Detect classic H.265 gray-ghost frames (almost flat mid-gray)."""
        try:
            import numpy as np

            small = img.resize((32, 32), Image.Resampling.NEAREST)
            arr = np.asarray(small.convert("L"), dtype=np.uint8)
            mid = float(((arr >= 90) & (arr <= 170)).mean())
            dark = float((arr < 40).mean())
            bright = float((arr > 210).mean())
            return mid > 0.72 and dark < 0.08 and bright < 0.08
        except Exception:
            return False

    def _jpeg_to_image(self, jpeg: bytes) -> Image.Image | None:
        try:
            import cv2
            import numpy as np

            arr = np.frombuffer(jpeg, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                return Image.open(io.BytesIO(jpeg)).convert("RGB")
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb)
        except Exception:
            try:
                return Image.open(io.BytesIO(jpeg)).convert("RGB")
            except Exception:
                return None

    def _fit_image(self, img: Image.Image, box_w: int, box_h: int) -> Image.Image:
        if self._view_zoom > 1.01:
            zw, zh = img.size
            cw = max(8, int(zw / self._view_zoom))
            ch = max(8, int(zh / self._view_zoom))
            x = (zw - cw) // 2
            y = (zh - ch) // 2
            img = img.crop((x, y, x + cw, y + ch))
        try:
            import cv2
            import numpy as np

            arr = np.asarray(img)
            h, w = arr.shape[:2]
            if w < 1 or h < 1:
                return img
            scale = min(max(box_w, 8) / w, max(box_h, 8) / h)
            if scale >= 0.999:
                return img
            nw = max(1, int(w * scale))
            nh = max(1, int(h * scale))
            out = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA)
            return Image.fromarray(out)
        except Exception:
            out = img.copy()
            out.thumbnail((max(box_w, 8), max(box_h, 8)), Image.Resampling.BILINEAR)
            return out

    def _drain_frames(self) -> None:
        if not self._alive:
            return
        try:
            latest = None
            while True:
                try:
                    latest = self._frames.get_nowait()
                except queue.Empty:
                    break
            if latest is not None:
                try:
                    import numpy as np

                    if isinstance(latest, np.ndarray):
                        self._last_rgb = latest
                        self._last_jpeg = None
                    else:
                        self._last_jpeg = latest
                        self._last_rgb = None
                except Exception:
                    self._last_jpeg = latest if isinstance(latest, (bytes, bytearray)) else self._last_jpeg
                    self._last_rgb = None
                img = self._frame_to_image(latest)
                if img is None:
                    pass
                elif self._looks_corrupt_gray(img):
                    self._gray_hits += 1
                    if self._gray_hits >= 2:
                        self._need_keyframe = True
                        self._gray_hits = 0
                    # Skip painting corrupt frames so the last good picture stays.
                else:
                    self._gray_hits = 0
                    w0, h0 = img.size
                    # Tall stacked frame → dual-lens stream; enable split automatically once.
                    if (not self._dual_checked) and h0 >= int(w0 * 1.35):
                        self._dual_checked = True
                        self._set_dual_mode("split")
                    elif not self._dual_checked:
                        self._dual_checked = True

                    if self._dual_mode == "split":
                        top = self._lens_crop(img, "top")
                        bot = self._lens_crop(img, "bottom")
                        tw = max(self._top_view.winfo_width(), 160)
                        th = max(self._top_view.winfo_height(), 100)
                        bw = max(self._bot_view.winfo_width(), 160)
                        bh = max(self._bot_view.winfo_height(), 100)
                        self._photo_top = ImageTk.PhotoImage(self._fit_image(top, tw, th))
                        self._photo_bot = ImageTk.PhotoImage(self._fit_image(bot, bw, bh))
                        self._top_view.configure(image=self._photo_top, text="")
                        self._bot_view.configure(image=self._photo_bot, text="")
                    else:
                        show = img
                        if self._dual_mode in ("top", "bottom"):
                            show = self._lens_crop(img, self._dual_mode)
                        w = max(self.canvas.winfo_width(), 320)
                        h = max(self.canvas.winfo_height(), 240)
                        self._photo = ImageTk.PhotoImage(self._fit_image(show, w, h))
                        self.canvas.configure(image=self._photo, text="")

                    self._fps_count += 1
                    now = time.time()
                    if self._fps_t == 0:
                        self._fps_t = now
                    elif now - self._fps_t >= 1.0:
                        fps = self._fps_count / (now - self._fps_t)
                        self._fps_count = 0
                        self._fps_t = now
                        rec = "  REC" if self._recorder.active else ""
                        mic = ""
                        if self._player.enabled:
                            ac = "IMA" if self._client and self._client.audio_codec == "ima" else "G.711"
                            mic = f"  MIC {ac}"
                        zoom = f"  {self._view_zoom:.0%}" if self._view_zoom > 1.01 else ""
                        dual = ""
                        if self._dual_mode == "split":
                            dual = "  Dual"
                        elif self._dual_mode == "top":
                            dual = "  Fixed full"
                        elif self._dual_mode == "bottom":
                            dual = "  PTZ full"
                        self._set_status(
                            f"Live  {self._codec_label}  {self._quality_name}  {fps:.0f} fps{mic}{rec}{zoom}{dual}"
                        )
        except Exception:
            pass
        if self._alive:
            self.after(20, self._drain_frames)

    def go_back(self) -> None:
        self.shutdown()
        self._on_back()

    def _open_profile(self) -> None:
        open_profile(self, self._api, self.status)

    def shutdown(self) -> None:
        self._alive = False
        self._stop.set()
        self._jobs.put(None)
        if self._ezviz_recorder is not None and self._ezviz_recorder.active:
            try:
                self._ezviz_recorder.stop()
            except Exception:
                pass
            self._ezviz_recorder = None
        if self._recorder.active:
            self._recorder.stop()
        if self._client is not None:
            try:
                self._alert.stop()
            except Exception:
                pass
        self._talker.close()
        self._player.close()
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
