"""Studio login profile — change username / password (not camera credentials)."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

from v380.ui.theme import (
    BG,
    BORDER,
    CARD,
    FONT_HEAD,
    FONT_SMALL,
    MUTED,
    ORANGE,
    RED,
    TEXT,
    entry,
    ghost_button,
    primary_button,
)


class ProfileDialog(tk.Toplevel):
    """Edit studio login username / password."""

    def __init__(self, master, api, on_saved=None):
        super().__init__(master)
        self.title("Profile")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._api = api
        self._on_saved = on_saved
        self.transient(master)
        self.grab_set()

        wrap = tk.Frame(self, bg=CARD, padx=24, pady=20, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(padx=16, pady=16)

        tk.Label(wrap, text="Your profile", fg=TEXT, bg=CARD, font=FONT_HEAD).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        tk.Label(
            wrap,
            text="Studio login for this server — not a camera password.",
            fg=MUTED,
            bg=CARD,
            font=FONT_SMALL,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        def row(r, label, value="", show=""):
            tk.Label(wrap, text=label, fg=MUTED, bg=CARD, font=FONT_SMALL).grid(row=r, column=0, sticky="w", pady=5)
            e = entry(wrap, width=28, show=show)
            if value:
                e.insert(0, value)
            e.grid(row=r, column=1, pady=5, padx=(12, 0), ipady=5)
            return e

        current = str(getattr(api, "user", "") or "")
        self.user_e = row(2, "Username", current)
        self.cur_pass_e = row(3, "Current password", show="*")
        self.new_pass_e = row(4, "New password", show="*")
        self.new_pass2_e = row(5, "Confirm new password", show="*")

        self._hint = tk.Label(
            wrap,
            text="Leave new password blank to keep the current one.",
            fg=MUTED,
            bg=CARD,
            font=FONT_SMALL,
        )
        self._hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        actions = tk.Frame(wrap, bg=CARD)
        actions.grid(row=7, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ghost_button(actions, "Cancel", self.destroy).pack(side="right", padx=(8, 0))
        self._save_btn = primary_button(actions, "Save", self._save)
        self._save_btn.pack(side="right")
        self.cur_pass_e.focus_set()

    def _save(self) -> None:
        username = self.user_e.get().strip()
        current = self.cur_pass_e.get()
        new_pass = self.new_pass_e.get()
        confirm = self.new_pass2_e.get()
        if not username:
            messagebox.showerror("Profile", "Username is required.")
            return
        if not current:
            messagebox.showerror("Profile", "Enter your current password to save changes.")
            return
        if new_pass or confirm:
            if new_pass != confirm:
                messagebox.showerror("Profile", "New password and confirmation do not match.")
                return
            if len(new_pass) < 4:
                messagebox.showerror("Profile", "New password must be at least 4 characters.")
                return
        self._save_btn.configure(state="disabled", text="Saving…")
        self._hint.configure(text="Updating profile…", fg=ORANGE)

        def work() -> None:
            try:
                out = self._api.update_profile(
                    current_password=current,
                    username=username,
                    new_password=new_pass or None,
                )
                user = str(out.get("user") or username)
                self.after(0, lambda: self._ok(user))
            except Exception as exc:
                self.after(0, lambda: self._fail(str(exc)))

        threading.Thread(target=work, daemon=True, name="profile-save").start()

    def _fail(self, err: str) -> None:
        self._save_btn.configure(state="normal", text="Save")
        self._hint.configure(text=err, fg=RED)
        messagebox.showerror("Profile", err)

    def _ok(self, user: str) -> None:
        if self._on_saved is not None:
            try:
                self._on_saved(user)
            except Exception:
                pass
        messagebox.showinfo("Profile", f"Saved. You are signed in as {user}.")
        self.destroy()


def open_profile(master, api, status_label=None) -> None:
    """Open the profile dialog; updates window title when saved."""
    if api is None:
        messagebox.showinfo("Profile", "Not connected to a server.")
        return

    def on_saved(user: str) -> None:
        root = master.winfo_toplevel()
        try:
            base = getattr(api, "base", "") or ""
            root.title(f"V380 Studio  ·  {user}@{base}")
        except Exception:
            pass
        if status_label is not None:
            try:
                status_label.configure(text=f"Profile updated — signed in as {user}")
            except Exception:
                pass

    ProfileDialog(master, api, on_saved)
