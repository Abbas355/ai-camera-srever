"""First screens: server IP, then username / password."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from pathlib import Path

from v380.client.api_client import SERVER_FILE, StudioAPI
from v380.ui.theme import ACCENT, BG, CARD, MUTED, TEXT


class LoginFrame(tk.Frame):
    def __init__(self, master, on_ok):
        super().__init__(master, bg=BG)
        self._on_ok = on_ok
        self._api: StudioAPI | None = None
        self._build()

    def _build(self) -> None:
        card = tk.Frame(self, bg=CARD, padx=28, pady=24)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="V380 Studio", fg="white", bg=CARD, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(card, text="Connect to the Linux server, then sign in", fg=MUTED, bg=CARD).pack(anchor="w", pady=(0, 16))

        tk.Label(card, text="Server IP", fg=MUTED, bg=CARD).pack(anchor="w")
        self.ip_e = tk.Entry(card, width=28, bg="#1f2937", fg=TEXT, insertbackground=TEXT, relief="flat")
        if SERVER_FILE.is_file():
            self.ip_e.insert(0, SERVER_FILE.read_text(encoding="utf-8").strip())
        else:
            self.ip_e.insert(0, "192.168.1.68")
        self.ip_e.pack(fill="x", pady=(4, 10), ipady=6)

        self.connect_btn = tk.Button(card, text="Connect", bg=ACCENT, fg="white", relief="flat", command=self._connect)
        self.connect_btn.pack(anchor="e", pady=(0, 14))

        self._login_box = tk.Frame(card, bg=CARD)
        tk.Label(self._login_box, text="Username", fg=MUTED, bg=CARD).pack(anchor="w")
        self.user_e = tk.Entry(self._login_box, width=28, bg="#1f2937", fg=TEXT, insertbackground=TEXT, relief="flat")
        self.user_e.insert(0, "admin")
        self.user_e.pack(fill="x", pady=(4, 10), ipady=6)
        tk.Label(self._login_box, text="Password", fg=MUTED, bg=CARD).pack(anchor="w")
        self.pass_e = tk.Entry(self._login_box, width=28, show="*", bg="#1f2937", fg=TEXT, insertbackground=TEXT, relief="flat")
        self.pass_e.pack(fill="x", pady=(4, 10), ipady=6)
        self.pass_e.bind("<Return>", lambda _e: self._login())
        tk.Button(self._login_box, text="Sign in", bg=ACCENT, fg="white", relief="flat", command=self._login).pack(anchor="e")

        self.status = tk.Label(card, text="Type the Ubuntu IP, then Connect.", fg=TEXT, bg=CARD, wraplength=280, justify="left")
        self.status.pack(anchor="w", pady=(16, 0))

    def _connect(self) -> None:
        host = self.ip_e.get().strip()
        if not host:
            messagebox.showinfo("Server", "Type the Ubuntu IP, then Connect.")
            return
        self.status.configure(text=f"Connecting to {host} …")
        self.update_idletasks()
        api = StudioAPI(host)
        try:
            ping = api.ping()
        except Exception as exc:
            self.status.configure(text=str(exc))
            messagebox.showerror("Server", f"Cannot reach {host}\n{exc}")
            return
        if not ping.get("ok"):
            self.status.configure(text="Server answered, but it is not V380 Studio.")
            return
        self._api = api
        self._login_box.pack(fill="x", pady=(8, 0))
        self.connect_btn.configure(text="Connected", bg="#166534")
        self.status.configure(text="Server OK. Sign in with your studio username.")
        self.pass_e.focus_set()

    def _login(self) -> None:
        if self._api is None:
            messagebox.showinfo("Server", "Connect to the server first.")
            return
        user = self.user_e.get().strip()
        password = self.pass_e.get()
        if not user or not password:
            messagebox.showerror("Login", "Username and password are required.")
            return
        try:
            self._api.login(user, password)
        except Exception as exc:
            messagebox.showerror("Login", str(exc))
            return
        self._on_ok(self._api)
