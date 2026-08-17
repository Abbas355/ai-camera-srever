"""V380 live preview — modern desktop UI, no decoder process required."""

from __future__ import annotations

import io
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from extras import AlawPlayer, H264Recorder, discover_devices, get_relay_ip
from v380_client import LiveH264Decoder, V380SnapshotClient

REC_DIR = Path(__file__).with_name("recordings")
BG = "#0b1220"
CARD = "#111827"
TEXT = "#e5e7eb"
MUTED = "#94a3b8"
ACCENT = "#2563eb"
GREEN = "#16a34a"
ORANGE = "#ea580c"
RED = "#dc2626"


class SnapshotApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("V380 Studio")
        self.geometry("1280x780")
        self.minsize(1020, 640)
        self.configure(bg=BG)

        self._client: V380SnapshotClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=3)
        self._decoder: LiveH264Decoder | None = None
        self._player = AlawPlayer()
        self._recorder = H264Recorder(REC_DIR)
        self._last_jpeg: bytes | None = None
        self._fps_count = 0
        self._fps_t = 0.0
        self._photo: ImageTk.PhotoImage | None = None
        self._quality_name = "HD"
        self._devices = []

        self._build()
        self.after(16, self._drain_frames)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD, padx=14, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="V380 Studio", fg="white", bg=CARD, font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(top, text="Direct camera :8800  ·  no decoder app", fg=MUTED, bg=CARD).grid(row=1, column=0, sticky="w")

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
        self.ip_e = ent(2, 0, 16, "192.168.1.7")
        lab(2, 1, "Port")
        self.port_e = ent(2, 1, 8, "8800")
        lab(2, 2, "Device ID")
        self.id_e = ent(2, 2, 12, "80342739")
        lab(2, 3, "Username")
        self.user_e = ent(2, 3, 12, "80342739")
        lab(2, 4, "Password")
        self.pass_e = ent(2, 4, 14, "", show="*")

        self.connect_btn = tk.Button(form, text="Connect", bg=ACCENT, fg="white", relief="flat", width=12, command=self._toggle)
        self.connect_btn.grid(row=3, column=5, padx=(8, 0), pady=(2, 6))

        self.status = tk.Label(self, text="1) Discover  2) pick camera  3) password  4) Connect", anchor="w", fg=TEXT, bg="#1e2937", padx=14, pady=6)
        self.status.pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(8, 14))
        self.canvas = tk.Label(body, bg="#020617", text="Live video", fg=MUTED)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(0, 12))
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
        btn(media, "Snapshot", ACCENT, self._snapshot, 11).grid(row=0, column=1, padx=3, pady=2)
        self.rec_btn = btn(media, "Record", RED, self._toggle_record, 11)
        self.rec_btn.grid(row=1, column=0, padx=3, pady=2)
        btn(media, "Open folder", "#334155", self._open_folder, 11).grid(row=1, column=1, padx=3, pady=2)

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

        title("NOT IN PROTOCOL YET")
        tk.Label(panel, text="Talk / Siren / SD playback\nneed extra APK packets.", fg="#64748b", bg=CARD, justify="left").pack(anchor="w")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _discover(self) -> None:
        self._set_status("Scanning LAN UDP 10008/10009 …")

        def work():
            try:
                devs = discover_devices()
            except Exception as exc:
                self.after(0, lambda: self._set_status(f"Discover failed: {exc}"))
                return
            self._devices = devs
            labels = [f"{d.dev_id}  {d.ip}  {d.mac}" for d in devs]
            self.after(0, lambda: self._show_devices(labels))

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

    def _cmd(self, name: str) -> None:
        client = self._client
        if client is None:
            self._set_status("Connect first, then use controls")
            return
        ok = client.send_control(name)
        self._set_status(f"Sent {name.replace('_', ' ')}" if ok else f"Control failed: {name}")

    def _toggle_listen(self) -> None:
        self._player.enabled = not self._player.enabled
        self.listen_btn.configure(text="Listen ON" if self._player.enabled else "Listen OFF", bg=GREEN if self._player.enabled else "#334155")
        self._set_status("Camera mic ON — you should hear live audio" if self._player.enabled else "Listen off")

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
            path = self._recorder.start()
            self.rec_btn.configure(text="Stop rec", bg="#7f1d1d")
            self._set_status(f"Recording…  {path.name}")

    def _open_folder(self) -> None:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        import os
        os.startfile(REC_DIR)

    def _toggle(self) -> None:
        if self._thread and self._thread.is_alive():
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        ip = self.ip_e.get().strip()
        user = self.user_e.get().strip()
        password = self.pass_e.get()
        quality_name = self.quality_e.get() or "HD"
        source = "cloud" if self.source_e.get() == "Cloud" else "lan"
        quality = 1 if quality_name == "HD" else 0
        try:
            port = int(self.port_e.get().strip())
            device_id = int(self.id_e.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Port and Device ID must be numbers.")
            return
        if not user or not password:
            messagebox.showerror("Missing fields", "Username and password are required.")
            return
        if source == "lan" and not ip:
            messagebox.showerror("Missing fields", "Camera IP is required for LAN.")
            return

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
                        self.after(0, lambda: self._set_status("Looking up cloud relay …"))
                        host = get_relay_ip(device_id)
                        if not host:
                            raise RuntimeError("No reachable cloud relay")
                        self.after(0, lambda h=host: self._set_status(f"Relay {h}"))
                    client = V380SnapshotClient(host, device_id, user, password, port, quality=quality, source=source)
                    client.connect()
                    self._client = client
                    decoder = LiveH264Decoder(self._frames)
                    self._decoder = decoder
                    got_key = False
                    for kind, is_iframe, payload in client.iter_video_frames(self._stop):
                        if kind == "audio":
                            self._player.play(payload)
                            continue
                        if is_iframe:
                            got_key = True
                        if not got_key:
                            continue
                        decoder.write_frame(True if is_iframe else False, payload, client._sps, client._pps)
                        if self._recorder.active:
                            rec = payload
                            if is_iframe and client._sps and client._pps:
                                rec = client.h264_for_decode(payload)
                            self._recorder.write(rec)
                    break
                except Exception as exc:
                    msg = str(exc)
                    if self._stop.is_set():
                        break
                    if "invalid username" in msg or "invalid password" in msg or "invalid device id" in msg:
                        self.after(0, lambda m=msg: self._fail(m))
                        break
                    self.after(0, lambda m=msg: self._set_status(f"Reconnecting… ({m})"))
                    time.sleep(1.5)
                finally:
                    if decoder is not None:
                        decoder.close()
                    if self._decoder is decoder:
                        self._decoder = None
                    if client is not None:
                        client.close()
                    if self._client is client:
                        self._client = None
            if not self._stop.is_set():
                self.after(0, lambda: self.connect_btn.configure(text="Connect", bg=ACCENT))

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
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
        self.connect_btn.configure(text="Connect", bg=ACCENT)
        self._set_status("Disconnected")

    def _drain_frames(self) -> None:
        latest = None
        while True:
            try:
                latest = self._frames.get_nowait()
            except queue.Empty:
                break
        if latest:
            self._last_jpeg = latest
            img = Image.open(io.BytesIO(latest))
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
                mic = "  MIC" if self._player.enabled else ""
                self._set_status(f"Live  {self._quality_name}  {fps:.0f} fps{mic}{rec}")
        self.after(16, self._drain_frames)

    def _on_close(self) -> None:
        self._stop.set()
        if self._recorder.active:
            self._recorder.stop()
        self._player.close()
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
        self.destroy()


if __name__ == "__main__":
    SnapshotApp().mainloop()
