"""Shared helpers for tool windows."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget


def configure_tool_window(widget: QWidget) -> None:
    """Independent top-level window with minimize / maximize / close / resize.

    Child QDialogs otherwise stay transient to the main window on Windows
    (owned HWND always stays above the owner), so the main UI cannot come
    forward while Trace Viewer / Help / Charts are open. Clear the parent
    after construction; callers must keep a Python reference (WA_DeleteOnClose
    is off).

    WindowTitleHint + WindowSystemMenuHint are required on Windows so the
    frame gets WS_THICKFRAME — without them only vertical resize may work.
    """
    # Drop transient owner before applying Window flags (order matters on Win).
    widget.setParent(None)
    widget.setWindowFlags(
        Qt.WindowType.Window
        | Qt.WindowType.WindowTitleHint
        | Qt.WindowType.WindowSystemMenuHint
        | Qt.WindowType.WindowMinimizeButtonHint
        | Qt.WindowType.WindowMaximizeButtonHint
        | Qt.WindowType.WindowCloseButtonHint
    )
    widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    # Allow free resize in all directions (overrides QDialog fixed-size habits).
    widget.setMinimumSize(320, 240)


def enable_raise_on_activate(widget: QWidget) -> None:
    """Kept for API compatibility; activation raise was removed (focus fights)."""
    _ = widget


def raise_window(widget: QWidget) -> None:
    """Show a tool window and bring it above siblings."""
    state = widget.windowState()
    if state & Qt.WindowState.WindowMinimized:
        widget.setWindowState(state & ~Qt.WindowState.WindowMinimized)
    widget.show()
    widget.raise_()
    widget.activateWindow()
