"""First screens: choose this PC or remote server, then username / password."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from v380.client.api_client import (
    LOCAL_HOST,
    SERVER_FILE,
    StudioAPI,
    ensure_local_server,
    restart_local_server,
)
from v380.ui.theme import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    CARD_ALT,
    FONT_BODY,
    FONT_BTN,
    FONT_HEAD,
    FONT_SMALL,
    FONT_SUB,
    FONT_TITLE,
    MUTED,
    TEXT,
    entry,
    ghost_button,
    labeled_entry,
    primary_button,
)


class LoginFrame(tk.Frame):
    def __init__(self, master, on_ok):
        super().__init__(master, bg=BG)
        self._on_ok = on_ok
        self._api: StudioAPI | None = None
        self._mode = tk.StringVar(value="local")
        self._build()

    def _build(self) -> None:
        # Soft side panel atmosphere without images
        left = tk.Frame(self, bg=CARD_ALT, width=320)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="V380", fg=ACCENT, bg=CARD_ALT, font=FONT_TITLE).pack(anchor="w", padx=36, pady=(72, 0))
        tk.Label(left, text="Studio", fg=TEXT, bg=CARD_ALT, font=FONT_TITLE).pack(anchor="w", padx=36)
        tk.Label(
            left,
            text="Camera control, live view,\nand recordings — one place.",
            fg=MUTED,
            bg=CARD_ALT,
            font=FONT_SUB,
            justify="left",
        ).pack(anchor="w", padx=36, pady=(18, 0))

        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        card = tk.Frame(right, bg=CARD, padx=36, pady=32, highlightthickness=1, highlightbackground=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="Sign in", fg=TEXT, bg=CARD, font=FONT_HEAD).pack(anchor="w")
        tk.Label(card, text="Choose where the backend runs", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(
            anchor="w", pady=(4, 18)
        )

        modes = tk.Frame(card, bg=CARD)
        modes.pack(fill="x", pady=(0, 14))
        self._local_btn = tk.Button(
            modes,
            text="This PC",
            command=lambda: self._set_mode("local"),
            relief="flat",
            bd=0,
            padx=16,
            pady=10,
            cursor="hand2",
            font=FONT_BTN,
        )
        self._local_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._remote_btn = tk.Button(
            modes,
            text="Remote server",
            command=lambda: self._set_mode("remote"),
            relief="flat",
            bd=0,
            padx=16,
            pady=10,
            cursor="hand2",
            font=FONT_BTN,
        )
        self._remote_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))

        self._remote_box = tk.Frame(card, bg=CARD)
        tk.Label(self._remote_box, text="Server IP", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")
        self.ip_e = entry(self._remote_box, width=32)
        saved = ""
        if SERVER_FILE.is_file():
            saved = SERVER_FILE.read_text(encoding="utf-8").strip()
        saved_host = saved.split(":")[0].lower() if saved else ""
        if saved and saved_host not in (LOCAL_HOST, "localhost"):
            self.ip_e.insert(0, saved)
            self._mode.set("remote")
        else:
            self.ip_e.insert(0, "192.168.1.68" if not saved or saved_host in (LOCAL_HOST, "localhost") else saved)
            self._mode.set("local")
        self.ip_e.pack(fill="x", pady=(4, 0), ipady=7)

        actions = tk.Frame(card, bg=CARD)
        self._actions = actions
        actions.pack(fill="x", pady=(16, 8))
        self.connect_btn = primary_button(actions, "Connect", self._connect)
        self.connect_btn.pack(side="right")
        self._restart_btn = ghost_button(actions, "Restart backend", self._restart_local)
        self._restart_btn.pack(side="right", padx=(0, 8))

        self._login_box = tk.Frame(card, bg=CARD)
        self.user_e = labeled_entry(self._login_box, "Username")
        self.user_e.insert(0, "admin")
        self.pass_e = labeled_entry(self._login_box, "Password", show="*")
        self.pass_e.bind("<Return>", lambda _e: self._login())
        primary_button(self._login_box, "Sign in", self._login).pack(anchor="e", pady=(4, 0))

        self.status = tk.Label(card, text="", fg=MUTED, bg=CARD, wraplength=340, justify="left", font=FONT_BODY)
        self.status.pack(anchor="w", pady=(16, 0))
        self._set_mode(self._mode.get())

    def _set_mode(self, mode: str) -> None:
        self._mode.set(mode)
        active = {"bg": ACCENT, "fg": "white", "activebackground": ACCENT, "activeforeground": "white"}
        idle = {"bg": CARD_ALT, "fg": TEXT, "activebackground": BORDER, "activeforeground": TEXT}
        self._remote_box.pack_forget()
        self._restart_btn.pack_forget()
        if mode == "local":
            self._local_btn.configure(**active)
            self._remote_btn.configure(**idle)
            self._restart_btn.pack(side="right", padx=(0, 8), before=self.connect_btn)
            self.status.configure(text="Uses this PC’s backend (starts automatically if needed).")
        else:
            self._remote_btn.configure(**active)
            self._local_btn.configure(**idle)
            self._remote_box.pack(fill="x", before=self._actions)
            self.status.configure(text="Enter the server IP, then Connect.")

    def _restart_local(self) -> None:
        self.status.configure(text="Restarting local backend…")
        self.update_idletasks()
        try:
            host = restart_local_server()
        except Exception as exc:
            self.status.configure(text=str(exc))
            messagebox.showerror("Backend", str(exc))
            return
        self.status.configure(text=f"Backend restarted on {host}. Click Connect.")

    def _connect(self) -> None:
        self._api = None
        self._login_box.pack_forget()
        self.connect_btn.configure(text="Connect", bg=ACCENT)

        if self._mode.get() == "local":
            self.status.configure(text="Starting / connecting to this PC…")
            self.update_idletasks()
            try:
                host = ensure_local_server()
            except Exception as exc:
                self.status.configure(text=str(exc))
                messagebox.showerror("This PC", str(exc))
                return
        else:
            host = self.ip_e.get().strip()
            if not host:
                messagebox.showinfo("Server", "Type the server IP, then Connect.")
                return
            self.status.configure(text=f"Connecting to {host}…")
            self.update_idletasks()

        api = StudioAPI(host)
        try:
            ping = api.ping()
        except Exception as exc:
            self.status.configure(text=str(exc))
            messagebox.showerror("Server", f"Cannot reach {host}\n{exc}")
            return
        if not ping.get("ok") or (ping.get("app") and ping.get("app") != "v380-studio"):
            self.status.configure(text="That address is not the V380 Studio backend.")
            messagebox.showerror("Server", "That address is not the V380 Studio backend.")
            return
        self._api = api
        self._login_box.pack(fill="x", pady=(8, 0))
        self.connect_btn.configure(text="Connected", bg="#0f766e")
        where = "this PC" if self._mode.get() == "local" else host
        self.status.configure(text=f"Connected to {where}. Enter your studio login.")
        self.pass_e.focus_set()

    def _login(self) -> None:
        if self._api is None:
            messagebox.showinfo("Server", "Connect first.")
            return
        user = self.user_e.get().strip()
        password = self.pass_e.get()
        if not user or not password:
            messagebox.showerror("Login", "Username and password are required.")
            return
        try:
            self._api.login(user, password)
        except Exception as exc:
            msg = str(exc)
            if "wrong" in msg.lower() or "password" in msg.lower():
                msg = "Wrong username or password"
            messagebox.showerror("Login", msg)
            return
        self._on_ok(self._api)
