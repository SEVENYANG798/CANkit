"""Bundled static assets (app icon, etc.)."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "xcp_pcan" / "resources"
    return Path(__file__).resolve().parent


def app_icon_path() -> Path:
    return resource_dir() / "cankit.ico"
