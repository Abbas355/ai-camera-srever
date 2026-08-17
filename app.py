"""V380 live preview — TCP :8800, auto-reconnect, HD/SD quality."""

from __future__ import annotations

import io
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from v380_client import LiveH264Decoder, V380SnapshotClient


class SnapshotApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("V380 Live Preview")
        self.geometry("1180x720")
        self.minsize(900, 560)
        self.configure(bg="#1e2937")

        self._client: V380SnapshotClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=3)
        self._decoder: LiveH264Decoder | None = None
        self._fps_count = 0
        self._fps_t = 0.0
        self._photo: ImageTk.PhotoImage | None = None
        self._quality_name = "HD"

        self._build()
        self.after(16, self._drain_frames)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        form = tk.Frame(self, bg="#0f172a", padx=12, pady=10)
        form.pack(fill="x")

        def field(col: int, label: str, default: str, show: str | None = None) -> tk.Entry:
            tk.Label(form, text=label, fg="#94a3b8", bg="#0f172a").grid(row=0, column=col, sticky="w")
            e = tk.Entry(form, width=14, show=show or "")
            e.insert(0, default)
            e.grid(row=1, column=col, padx=(0, 8), pady=(2, 0))
            return e

        self.ip_e = field(0, "Camera IP", "192.168.1.7")
        self.port_e = field(1, "Port", "8800")
        self.id_e = field(2, "Device ID", "80342739")
        self.user_e = field(3, "Username", "80342739")
        self.pass_e = field(4, "Password", "", show="*")

        tk.Label(form, text="Quality", fg="#94a3b8", bg="#0f172a").grid(row=0, column=5, sticky="w")
        self.quality_e = ttk.Combobox(form, values=["HD", "SD"], width=6, state="readonly")
        self.quality_e.set("HD")
        self.quality_e.grid(row=1, column=5, padx=(0, 8), pady=(2, 0))

        btns = tk.Frame(form, bg="#0f172a")
        btns.grid(row=1, column=6, padx=(8, 0))
        self.connect_btn = ttk.Button(btns, text="Connect", command=self._toggle)
        self.connect_btn.pack(side="left")

        self.status = tk.Label(
            self,
            text="Enter password, pick HD or SD, then Connect.",
            anchor="w",
            fg="#cbd5e1",
            bg="#1e2937",
            padx=12,
            pady=6,
        )
        self.status.pack(fill="x")

        body = tk.Frame(self, bg="#1e2937")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.canvas = tk.Label(body, bg="#020617", text="No snapshot yet", fg="#64748b")
        self.canvas.pack(side="left", fill="both", expand=True, padx=(0, 10))

        self._build_controls(body)

    def _build_controls(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg="#0f172a", padx=12, pady=12, width=240)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)

        def title(text: str) -> None:
            tk.Label(panel, text=text, fg="#94a3b8", bg="#0f172a", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 6))

        title("PTZ  (hold to move)")
        grid = tk.Frame(panel, bg="#0f172a")
        grid.pack()

        def ptz(row: int, col: int, label: str, cmd: str | None) -> None:
            if not cmd:
                tk.Frame(grid, width=64, height=36, bg="#0f172a").grid(row=row, column=col, padx=3, pady=3)
                return
            b = tk.Button(grid, text=label, width=7, bg="#0396FF", fg="white", relief="flat")
            b.grid(row=row, column=col, padx=3, pady=3)
            b.bind("<ButtonPress-1>", lambda _e, c=cmd: self._cmd(c))
            b.bind("<ButtonRelease-1>", lambda _e: self._cmd("ptz_stop"))

        ptz(0, 1, "UP", "ptz_up")
        ptz(1, 0, "LEFT", "ptz_left")
        stop = tk.Button(grid, text="STOP", width=7, bg="#334155", fg="white", relief="flat", command=lambda: self._cmd("ptz_stop"))
        stop.grid(row=1, column=1, padx=3, pady=3)
        ptz(1, 2, "RIGHT", "ptz_right")
        ptz(2, 1, "DOWN", "ptz_down")

        title("LIGHT")
        lights = tk.Frame(panel, bg="#0f172a")
        lights.pack(fill="x")
        for i, (label, cmd) in enumerate((("ON", "light_on"), ("OFF", "light_off"), ("AUTO", "light_auto"))):
            tk.Button(
                lights, text=label, width=7, bg="#48bb78", fg="white", relief="flat",
                command=lambda c=cmd: self._cmd(c),
            ).grid(row=0, column=i, padx=3, pady=2)

        title("IMAGE MODE")
        imgs = tk.Frame(panel, bg="#0f172a")
        imgs.pack(fill="x")
        for i, (label, cmd) in enumerate((("COLOR", "image_color"), ("B&W", "image_bw"))):
            tk.Button(
                imgs, text=label, width=10, bg="#ed8936", fg="white", relief="flat",
                command=lambda c=cmd: self._cmd(c),
            ).grid(row=0, column=i, padx=3, pady=2)
        for i, (label, cmd) in enumerate((("AUTO", "image_auto"), ("FLIP", "image_flip"))):
            tk.Button(
                imgs, text=label, width=10, bg="#ed8936", fg="white", relief="flat",
                command=lambda c=cmd: self._cmd(c),
            ).grid(row=1, column=i, padx=3, pady=2)

        tk.Label(
            panel,
            text="Connect first.\nPTZ: hold a direction, release to stop.",
            fg="#64748b",
            bg="#0f172a",
            justify="left",
        ).pack(anchor="w", pady=(16, 0))

    def _cmd(self, name: str) -> None:
        client = self._client
        if client is None:
            self._set_status("Connect first, then use controls")
            return
        ok = client.send_control(name)
        if ok:
            self._set_status(f"Sent {name.replace('_', ' ')}")
        else:
            self._set_status(f"Control failed: {name}")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

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
        quality = 1 if quality_name == "HD" else 0
        try:
            port = int(self.port_e.get().strip())
            device_id = int(self.id_e.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Port and Device ID must be numbers.")
            return
        if not ip or not user or not password:
            messagebox.showerror("Missing fields", "IP, username, and password are required.")
            return

        self._stop.clear()
        self._quality_name = quality_name
        self.connect_btn.configure(text="Disconnect")
        self._set_status(f"Connecting to {ip}:{port} ({quality_name}) …")

        def worker() -> None:
            while not self._stop.is_set():
                client = None
                decoder = None
                try:
                    client = V380SnapshotClient(ip, device_id, user, password, port, quality=quality)
                    self.after(0, lambda: self._set_status(f"Auth on {ip}:{port} …"))
                    client.connect()
                    self._client = client
                    self.after(
                        0,
                        lambda: self._set_status(
                            f"Live {quality_name}  {client.frame_width}x{client.frame_height}  "
                            f"ticket={client.auth_ticket}"
                        ),
                    )
                    decoder = LiveH264Decoder(self._frames)
                    self._decoder = decoder
                    got_key = False
                    for is_iframe, payload in client.iter_video_frames(self._stop):
                        if is_iframe:
                            got_key = True
                        if not got_key:
                            continue
                        decoder.write_frame(is_iframe, payload, client._sps, client._pps)
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
                self.after(0, lambda: self.connect_btn.configure(text="Connect"))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _fail(self, msg: str) -> None:
        self._stop.set()
        self.connect_btn.configure(text="Connect")
        self._set_status(f"Error: {msg}")
        messagebox.showerror("Login failed", msg)

    def _disconnect(self) -> None:
        self._stop.set()
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
        self.connect_btn.configure(text="Connect")
        self._set_status("Disconnected")

    def _drain_frames(self) -> None:
        latest = None
        while True:
            try:
                latest = self._frames.get_nowait()
            except queue.Empty:
                break
        if latest:
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
                self._set_status(f"Live preview  {self._quality_name}  {fps:.0f} fps")
        self.after(16, self._drain_frames)

    def _on_close(self) -> None:
        self._stop.set()
        if self._decoder is not None:
            self._decoder.close()
        if self._client is not None:
            self._client.close()
        self.destroy()


if __name__ == "__main__":
    SnapshotApp().mainloop()
