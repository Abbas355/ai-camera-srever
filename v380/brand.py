"""Product name shown in the UI and used by the backend ping check."""

from __future__ import annotations

import os
import sys

APP_NAME = "Optiview"
APP_ID = "optiview"
TAGLINE = "Camera control, live view, and recordings."
TAGLINE_LOGIN = "Camera control, live view,\nand recordings — one place."
SUPPORTED_CAMERAS = "V380 + EZVIZ cameras"


def is_debug_build() -> bool:
    """True when running from source, or OPTIVIEW_DEBUG=1.

    Installed / PyInstaller builds are release: remote server only (no This PC).
    """
    if os.environ.get("OPTIVIEW_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    # PyInstaller / frozen EXE = release
    if getattr(sys, "frozen", False):
        return False
    return True
