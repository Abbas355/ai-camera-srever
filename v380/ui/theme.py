"""Shared UI theme — calm charcoal studio look (not default purple/gray kitsch)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# Surfaces
BG = "#0e1419"
CARD = "#171e26"
CARD_ALT = "#1c2530"
TILE = "#0a0f14"
BAR = "#12181f"
INPUT = "#0f161c"
BORDER = "#2a3542"

# Text
TEXT = "#e8eef4"
MUTED = "#8b9aab"
DIM = "#5c6b7a"

# Accents — teal / signal colors (CCTV control-room feel)
ACCENT = "#0d9488"
ACCENT_HOVER = "#14b8a6"
GREEN = "#22c55e"
ORANGE = "#f59e0b"
RED = "#ef4444"
BTN_GHOST = "#243040"
BTN_GHOST_HOVER = "#2f3f52"

FONT = "Segoe UI"
FONT_TITLE = (FONT, 22, "bold")
FONT_HEAD = (FONT, 15, "bold")
FONT_SUB = (FONT, 10)
FONT_BODY = (FONT, 10)
FONT_BTN = (FONT, 10, "bold")
FONT_SMALL = (FONT, 9)


def apply_root_style(root: tk.Misc) -> None:
    try:
        root.configure(bg=BG)
    except Exception:
        pass
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TCombobox", fieldbackground=INPUT, background=CARD, foreground=TEXT, arrowcolor=TEXT)
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", INPUT)],
        foreground=[("readonly", TEXT)],
        selectbackground=[("readonly", ACCENT)],
        selectforeground=[("readonly", "white")],
    )
    style.configure("Vertical.TScrollbar", background=CARD, troughcolor=BG, arrowcolor=TEXT)


def primary_button(parent, text: str, command, **pack_kw) -> tk.Button:
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        bg=ACCENT,
        fg="white",
        activebackground=ACCENT_HOVER,
        activeforeground="white",
        relief="flat",
        bd=0,
        padx=14,
        pady=7,
        cursor="hand2",
        font=FONT_BTN,
    )
    if pack_kw:
        btn.pack(**pack_kw)
    return btn


def ghost_button(parent, text: str, command, **pack_kw) -> tk.Button:
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        bg=BTN_GHOST,
        fg=TEXT,
        activebackground=BTN_GHOST_HOVER,
        activeforeground="white",
        relief="flat",
        bd=0,
        padx=12,
        pady=7,
        cursor="hand2",
        font=FONT_BTN,
    )
    if pack_kw:
        btn.pack(**pack_kw)
    return btn


def danger_button(parent, text: str, command, **pack_kw) -> tk.Button:
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        bg=RED,
        fg="white",
        activebackground="#dc2626",
        activeforeground="white",
        relief="flat",
        bd=0,
        padx=12,
        pady=7,
        cursor="hand2",
        font=FONT_BTN,
    )
    if pack_kw:
        btn.pack(**pack_kw)
    return btn


def entry(parent, *, width: int = 28, show: str = "") -> tk.Entry:
    return tk.Entry(
        parent,
        width=width,
        show=show,
        bg=INPUT,
        fg=TEXT,
        insertbackground=TEXT,
        relief="flat",
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=ACCENT,
        font=FONT_BODY,
    )


def labeled_entry(parent, label: str, *, show: str = "", width: int = 28) -> tk.Entry:
    tk.Label(parent, text=label, fg=MUTED, bg=parent.cget("bg"), font=FONT_SMALL).pack(anchor="w")
    e = entry(parent, width=width, show=show)
    e.pack(fill="x", pady=(4, 10), ipady=7)
    return e


def status_bar(parent) -> tk.Label:
    return tk.Label(
        parent,
        text="",
        anchor="w",
        fg=TEXT,
        bg=BAR,
        padx=18,
        pady=8,
        font=FONT_BODY,
    )
