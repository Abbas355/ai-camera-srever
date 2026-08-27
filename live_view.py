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

from camera_store import Camera
from extras import AlawPlayer, AlertSiren, H264Recorder, Talker, discover_devices, get_relay_ip
from theme import ACCENT, BG, CARD, GREEN, MUTED, ORANGE, RED, TEXT
from v380_client import LiveH264Decoder, V380SnapshotClient

REC_DIR = Path(__file__).with_name("recordings")


class LiveView(tk.Frame):
    def __init__(
        self,
        master,
        on_back,
        on_connected=None,
        camera: Camera | None = None,
        auto_connect: bool = False,
        recorders=None,
    ):
        super().__init__(master, bg=BG)
        self._on_back = on_back
        self._on_connected = on_connected
        self._preset = camera
        self._auto_connect = auto_connect
        self._recorders = recorders

        self._client: V380SnapshotClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=3)
        self._decoder: LiveH264Decoder | None = None
        self._player = AlawPlayer()
        self._talker = Talker()
        self._alert = AlertSiren()
        self._recorder = H264Recorder(REC_DIR)
        self._last_jpeg: bytes | None = None
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
        self._alert_job = None

        self._build()
        if camera is not None:
            self._apply_camera(camera)
        self.after(16, self._drain_frames)
        if auto_connect and camera is not None:
            self.after(200, self._connect)

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
        top = tk.Frame(self, bg=CARD, padx=14, pady=10)
        top.pack(fill="x")
        tk.Button(top, text="← Home", bg="#334155", fg="white", relief="flat", command=self.go_back).grid(
            row=0, column=0, rowspan=2, padx=(0, 12), sticky="ns"
        )
        tk.Label(top, text="V380 Studio", fg="white", bg=CARD, font=("Segoe UI", 14, "bold")).grid(row=0, column=1, sticky="w")
        tk.Label(top, text="Live view  ·  all camera controls", fg=MUTED, bg=CARD).grid(row=1, column=1, sticky="w")

        form = tk.Frame(self, bg=BG, padx=14, pady=8)
        form.pack(fill="x")

        def lab(r, c, t):
            tk.Label(form, text=t, fg=MUTED, bg=BG).grid(row=r, column=c, sticky="w")

        def ent(r, c, w, default="", show=""):
            e = tk.Entry(form, width=w, show=show, bg="#1f2937", fg=TEXT, insertbackground=TEXT, relief="flat")
            e.insert(0, default)
            e.grid(row=r + 1, column=c, padx=(0, 8), pady=(2, 6), sticky="we")
            return e

        lab(0, 0, "Found cameras")
        self.cam_box = ttk.Combobox(form, width=28, state="readonly")
        self.cam_box.grid(row=1, column=0, padx=(0, 8), pady=(2, 6))
        self.cam_box.bind("<<ComboboxSelected>>", self._pick_device)
        ttk.Button(form, text="Discover", command=self._discover).grid(row=1, column=1, padx=(0, 8))

        lab(0, 2, "Source")
        self.source_e = ttk.Combobox(form, values=["LAN", "Cloud"], width=8, state="readonly")
        self.source_e.set("LAN")
        self.source_e.grid(row=1, column=2, padx=(0, 8), pady=(2, 6))

        lab(0, 3, "Quality")
        self.quality_e = ttk.Combobox(form, values=["HD", "SD"], width=6, state="readonly")
        self.quality_e.set("HD")
        self.quality_e.grid(row=1, column=3, padx=(0, 8), pady=(2, 6))

        lab(2, 0, "Camera IP")
        self.ip_e = ent(2, 0, 16, "")
        lab(2, 1, "Port")
        self.port_e = ent(2, 1, 8, "8800")
        lab(2, 2, "Device ID")
        self.id_e = ent(2, 2, 12, "")
        lab(2, 3, "Username")
        self.user_e = ent(2, 3, 12, "")
        lab(2, 4, "Password")
        self.pass_e = ent(2, 4, 14, "", show="*")
        lab(2, 5, "Display name")
        self.name_e = ent(2, 5, 16, "")

        self.connect_btn = tk.Button(form, text="Connect", bg=ACCENT, fg="white", relief="flat", width=12, command=self._toggle)
        self.connect_btn.grid(row=3, column=6, padx=(8, 0), pady=(2, 6))

        self.status = tk.Label(self, text="Discover, enter password, Connect — saved automatically", anchor="w", fg=TEXT, bg="#1e2937", padx=14, pady=6)
        self.status.pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(8, 14))
        self.canvas = tk.Label(body, bg="#020617", text="Live video", fg=MUTED)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(0, 12))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self._build_controls(body)

    def _build_controls(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=CARD, padx=12, pady=12, width=260)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)

        def title(text: str) -> None:
            tk.Label(panel, text=text, fg=MUTED, bg=CARD, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 6))

        def btn(parent, text, color, cmd, width=8):
            return tk.Button(parent, text=text, width=width, bg=color, fg="white", relief="flat", command=cmd)

        title("MEDIA")
        media = tk.Frame(panel, bg=CARD)
        media.pack(fill="x")
        self.listen_btn = btn(media, "Listen OFF", "#334155", self._toggle_listen, 11)
        self.listen_btn.grid(row=0, column=0, padx=3, pady=2)
        self.talk_btn = btn(media, "Talk OFF", "#334155", self._toggle_talk, 11)
        self.talk_btn.grid(row=0, column=1, padx=3, pady=2)
        btn(media, "Snapshot", ACCENT, self._snapshot, 11).grid(row=1, column=0, padx=3, pady=2)
        self.rec_btn = btn(media, "Record", RED, self._toggle_record, 11)
        self.rec_btn.grid(row=1, column=1, padx=3, pady=2)
        btn(media, "Open folder", "#334155", self._open_folder, 11).grid(row=2, column=0, padx=3, pady=2)

        title("PTZ  (hold to move)")
        grid = tk.Frame(panel, bg=CARD)
        grid.pack()

        def ptz(row, col, label, cmd):
            b = tk.Button(grid, text=label, width=7, bg=ACCENT, fg="white", relief="flat")
            b.grid(row=row, column=col, padx=3, pady=3)
            b.bind("<ButtonPress-1>", lambda _e, c=cmd: self._cmd(c))
            b.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))

        ptz(0, 1, "UP", "ptz_up")
        ptz(1, 0, "LEFT", "ptz_left")
        tk.Button(grid, text="STOP", width=7, bg="#334155", fg="white", relief="flat", command=lambda: self._cmd("ptz_stop")).grid(row=1, column=1, padx=3, pady=3)
        ptz(1, 2, "RIGHT", "ptz_right")
        ptz(2, 1, "DOWN", "ptz_down")
        zoom_out = tk.Button(grid, text="ZOOM -", width=7, bg=ACCENT, fg="white", relief="flat")
        zoom_out.grid(row=3, column=0, padx=3, pady=3)
        zoom_out.bind("<ButtonPress-1>", lambda _e: self._zoom_hold("ptz_zoom_out", -0.25))
        zoom_out.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))
        tk.Button(grid, text="1x", width=7, bg="#334155", fg="white", relief="flat", command=self._zoom_reset).grid(row=3, column=1, padx=3, pady=3)
        zoom_in = tk.Button(grid, text="ZOOM +", width=7, bg=ACCENT, fg="white", relief="flat")
        zoom_in.grid(row=3, column=2, padx=3, pady=3)
        zoom_in.bind("<ButtonPress-1>", lambda _e: self._zoom_hold("ptz_zoom_in", 0.25))
        zoom_in.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))

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
        btn(alert, "OFF", "#334155", self._alert_off, 11).grid(row=0, column=1, padx=3, pady=2)
        hold = tk.Button(alert, text="HOLD", width=11, bg=RED, fg="white", relief="flat")
        hold.grid(row=1, column=0, columnspan=2, padx=3, pady=2)
        hold.bind("<ButtonPress-1>", lambda _e: self._alert_on())
        hold.bind("<ButtonRelease-1>", lambda _e: self._alert_off())

        title("NOT IN PROTOCOL YET")
        tk.Label(panel, text="SD playback needs extra APK packets.", fg="#64748b", bg=CARD, justify="left").pack(anchor="w")

    def _apply_camera(self, cam: Camera) -> None:
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

    def _read_form(self) -> dict | None:
        ip = self.ip_e.get().strip()
        user = self.user_e.get().strip()
        password = self.pass_e.get()
        quality_name = self.quality_e.get() or "HD"
        source = "cloud" if self.source_e.get() == "Cloud" else "lan"
        try:
            port = int(self.port_e.get().strip())
            device_id = int(self.id_e.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Port and Device ID must be numbers.")
            return None
        if not user or not password:
            messagebox.showerror("Missing fields", "Username and password are required.")
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
        self._set_status("Scanning LAN UDP 10008/10009 …")

        def work():
            try:
                devs = discover_devices()
            except Exception as exc:
                self._ui( lambda: self._set_status(f"Discover failed: {exc}"))
                return
            self._devices = devs
            labels = [f"{d.dev_id}  {d.ip}  {d.mac}" for d in devs]
            self._ui( lambda: self._show_devices(labels))

        threading.Thread(target=work, daemon=True).start()

    def _show_devices(self, labels: list[str]) -> None:
        self.cam_box["values"] = labels
        if labels:
            self.cam_box.set(labels[0])
            self._pick_device()
            self._set_status(f"Found {len(labels)} camera(s). Pick one, enter password, Connect.")
        else:
            self._set_status("No cameras found. Check LAN / UDP 10008.")

    def _pick_device(self, _evt=None) -> None:
        idx = self.cam_box.current()
        if idx < 0 or idx >= len(self._devices):
            return
        d = self._devices[idx]
        self.ip_e.delete(0, "end")
        self.ip_e.insert(0, d.ip)
        self.id_e.delete(0, "end")
        self.id_e.insert(0, d.dev_id)
        self.user_e.delete(0, "end")
        self.user_e.insert(0, d.dev_id)
        if not self.name_e.get().strip():
            self.name_e.insert(0, d.dev_id)
        self._pending_mac = d.mac

    def _cmd(self, name: str) -> None:
        client = self._client
        if client is None:
            self._set_status("Connect first, then use controls")
            return
        ok = client.send_control(name)
        self._set_status(f"Sent {name.replace('_', ' ')}" if ok else f"Control failed: {name}")

    def _alert_on(self) -> None:
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
        self._alert.stop()
        self._set_status("Alert off")

    def _alert_auto_off(self) -> None:
        self._alert_job = None
        if self._alive:
            self._alert.stop()
            self._set_status("Alert off")

    def _set_zoom(self, value: float) -> None:
        self._view_zoom = min(4.0, max(1.0, round(value, 2)))
        self._set_status(f"Zoom {self._view_zoom:.0%}")

    def _zoom_hold(self, cmd: str, step: float) -> None:
        self._set_zoom(self._view_zoom + step)
        self._cmd(cmd)

    def _zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def _on_wheel(self, evt) -> None:
        step = 0.25 if getattr(evt, "delta", 0) > 0 else -0.25
        self._set_zoom(self._view_zoom + step)

    def _toggle_listen(self) -> None:
        self._player.enabled = not self._player.enabled
        self.listen_btn.configure(text="Listen ON" if self._player.enabled else "Listen OFF", bg=GREEN if self._player.enabled else "#334155")
        if self._player.enabled:
            ac = "IMA ADPCM" if self._client and self._client.audio_codec == "ima" else "G.711"
            self._set_status(f"Listen ON ({ac}, 8 kHz) — use PC speakers")
        else:
            self._set_status("Listen off")

    def _toggle_talk(self) -> None:
        if self._talker.enabled:
            self._talker.stop()
            self.talk_btn.configure(text="Talk OFF", bg="#334155")
            self._set_status("Talk off")
            return
        if self._client is None:
            self._set_status("Connect first, then Talk")
            return
        self._talker.attach(self._client)
        if self._talker.start():
            self.talk_btn.configure(text="Talk ON", bg=GREEN)
            self._set_status("Talk ON — speak into the PC microphone")
        else:
            self.talk_btn.configure(text="Talk OFF", bg="#334155")
            err = self._talker.error or "unknown error"
            self._set_status(f"Talk failed — {err}")
            if "microphone" in err.lower() or "invalid" in err.lower() or "device" in err.lower():
                open_settings = messagebox.askyesno(
                    "Microphone blocked",
                    "Windows is blocking the microphone, so there is no Allow button in this app.\n\n"
                    "Turn ON both of these:\n"
                    "• Microphone access\n"
                    "• Let desktop apps access your microphone\n\n"
                    "Open Windows microphone settings now?",
                )
                if open_settings:
                    Talker.open_mic_settings()
            else:
                messagebox.showerror("Talk failed", err)

    def _snapshot(self) -> None:
        if not self._last_jpeg:
            self._set_status("No frame yet. Connect first.")
            return
        REC_DIR.mkdir(parents=True, exist_ok=True)
        path = REC_DIR / time.strftime("snap_%Y%m%d_%H%M%S.jpg")
        path.write_bytes(self._last_jpeg)
        self._set_status(f"Snapshot saved  {path}")

    def _toggle_record(self) -> None:
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
        ip = fields["ip"]
        user = fields["username"]
        password = fields["password"]
        quality_name = fields["quality_name"]
        source = fields["source"]
        quality = fields["quality"]
        port = fields["port"]
        device_id = int(fields["device_id"])

        self._stop.clear()
        self._quality_name = quality_name
        self.connect_btn.configure(text="Disconnect", bg=RED)
        self._set_status(f"Connecting ({source.upper()} {quality_name}) …")

        def worker() -> None:
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
                            decoder = LiveH264Decoder(self._frames, fmt=client.video_codec)
                            self._decoder = decoder
                            codec = "H.265" if client.video_codec == "hevc" else "H.264"
                            self._ui( lambda c=codec: self._set_codec(c))
                        if is_iframe:
                            got_key = True
                        if not got_key:
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
                    self._ui(lambda: self.talk_btn.configure(text="Talk OFF", bg="#334155"))
                    if client is not None:
                        client.close()
                    if self._client is client:
                        self._client = None
            if not self._stop.is_set() and self._alive:
                self._ui( lambda: self.connect_btn.configure(text="Connect", bg=ACCENT))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _fail(self, msg: str) -> None:
        self._stop.set()
        self.connect_btn.configure(text="Connect", bg=ACCENT)
        self._set_status(f"Error: {msg}")
        messagebox.showerror("Login failed", msg)

    def _disconnect(self) -> None:
        self._stop.set()
        if self._recorder.active:
            self._toggle_record()
        if self._client is not None:
            try:
                self._alert.stop()
            except Exception:
                pass
        self._talker.stop()
        self.talk_btn.configure(text="Talk OFF", bg="#334155")
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
        self.connect_btn.configure(text="Connect", bg=ACCENT)
        self._set_status("Disconnected")

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
            if latest:
                self._last_jpeg = latest
                img = Image.open(io.BytesIO(latest))
                if self._view_zoom > 1.01:
                    zw, zh = img.size
                    cw = max(8, int(zw / self._view_zoom))
                    ch = max(8, int(zh / self._view_zoom))
                    x = (zw - cw) // 2
                    y = (zh - ch) // 2
                    img = img.crop((x, y, x + cw, y + ch))
                w = max(self.canvas.winfo_width(), 320)
                h = max(self.canvas.winfo_height(), 240)
                img.thumbnail((w, h), Image.Resampling.BILINEAR)
                self._photo = ImageTk.PhotoImage(img)
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
                    self._set_status(f"Live  {self._codec_label}  {self._quality_name}  {fps:.0f} fps{mic}{rec}{zoom}")
        except Exception:
            pass
        if self._alive:
            self.after(50, self._drain_frames)

    def go_back(self) -> None:
        self.shutdown()
        self._on_back()

    def shutdown(self) -> None:
        self._alive = False
        self._stop.set()
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
