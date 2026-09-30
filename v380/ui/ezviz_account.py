"""Prompt for EZVIZ app account (needed for PTZ / light / siren)."""

from __future__ import annotations

import tkinter as tk

from v380.ui.theme import ACCENT, BG, BORDER, CARD, FONT_BODY, FONT_HEAD, FONT_SMALL, MUTED, TEXT, entry, primary_button


def ask_ezviz_account(master) -> tuple[str, str, str] | None:
    """Return (account, password, sms_code) or None if cancelled."""
    win = tk.Toplevel(master)
    win.title("EZVIZ account")
    win.configure(bg=BG)
    win.transient(master)
    win.grab_set()
    win.resizable(False, False)
    result: dict = {}

    box = tk.Frame(win, bg=CARD, padx=24, pady=20, highlightthickness=1, highlightbackground=BORDER)
    box.pack(padx=16, pady=16)
    tk.Label(box, text="EZVIZ app login", fg=TEXT, bg=CARD, font=FONT_HEAD).pack(anchor="w")
    tk.Label(
        box,
        text="PTZ, flash, and siren use the same account as the EZVIZ phone app.\nNot the camera verification code.",
        fg=MUTED,
        bg=CARD,
        font=FONT_SMALL,
        justify="left",
    ).pack(anchor="w", pady=(6, 14))

    tk.Label(box, text="Email or phone", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")
    acc = entry(box, width=36)
    acc.pack(fill="x", pady=(2, 8))
    tk.Label(box, text="App password", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")
    pw = entry(box, width=36, show="*")
    pw.pack(fill="x", pady=(2, 8))
    tk.Label(box, text="SMS code (only if the app asks for 2FA)", fg=MUTED, bg=CARD, font=FONT_SMALL).pack(anchor="w")
    sms = entry(box, width=36)
    sms.pack(fill="x", pady=(2, 12))

    try:
        from v380.client.ezviz_session import load_account

        saved = load_account()
        if saved:
            acc.insert(0, saved[0])
    except Exception:
        pass

    def ok() -> None:
        result["v"] = (acc.get().strip(), pw.get(), sms.get().strip())
        win.destroy()

    def cancel() -> None:
        result["v"] = None
        win.destroy()

    row = tk.Frame(box, bg=CARD)
    row.pack(fill="x")
    primary_button(row, "Sign in", ok).pack(side="right")
    tk.Button(row, text="Skip", command=cancel, relief="flat", bg="#243040", fg="white", font=FONT_BODY).pack(
        side="right", padx=(0, 8)
    )
    win.bind("<Return>", lambda _e: ok())
    win.bind("<Escape>", lambda _e: cancel())
    acc.focus_set()
    win.wait_window()
    return result.get("v")
