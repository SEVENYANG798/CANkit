"""Shared helpers for QThread workers and progress dialogs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QProgressDialog, QWidget

logger = logging.getLogger(__name__)


def make_progress_dialog(
    parent: QWidget | None,
    *,
    title: str,
    label: str,
    cancel_text: str | None = "Cancel",
    maximum: int = 0,
    tool_window: bool = False,
) -> QProgressDialog:
    """Create a non-modal progress dialog used by background jobs."""
    progress = QProgressDialog(label, cancel_text, 0, maximum, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModality.NonModal)
    if tool_window:
        progress.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    if maximum > 0:
        progress.setValue(0)
    progress.show()
    return progress


def mark_progress_cancelling(progress: QProgressDialog | None, label: str = "Cancelling…") -> None:
    if progress is None:
        return
    progress.setLabelText(label)
    progress.setCancelButton(None)
    progress.show()


def close_progress(progress: QProgressDialog | None) -> None:
    if progress is None:
        return
    try:
        progress.close()
    except Exception:  # noqa: BLE001
        logger.debug("Progress dialog close failed", exc_info=True)


def abort_qthread(
    worker: QThread | None,
    *,
    timeout_ms: int = 5000,
    abort: Callable[[], None] | None = None,
) -> None:
    """Request stop, block signals, and wait for a QThread to finish."""
    if worker is None:
        return
    try:
        if abort is not None:
            abort()
        elif worker.isRunning():
            worker.requestInterruption()
    except Exception:  # noqa: BLE001
        logger.debug("Worker abort request failed", exc_info=True)
    try:
        worker.blockSignals(True)
    except Exception:  # noqa: BLE001
        logger.debug("Worker blockSignals failed", exc_info=True)
    if worker.isRunning():
        worker.wait(timeout_ms)


def abort_workers(
    workers: list[Any],
    *,
    timeout_ms: int = 5000,
) -> None:
    """Abort a list of QThread workers (or objects with ``abort()``)."""
    for worker in workers:
        if worker is None:
            continue
        abort_fn = getattr(worker, "abort", None)
        if callable(abort_fn) and isinstance(worker, QThread):
            abort_qthread(worker, timeout_ms=timeout_ms, abort=abort_fn)
        elif isinstance(worker, QThread):
            abort_qthread(worker, timeout_ms=timeout_ms)
        elif callable(abort_fn):
            try:
                abort_fn()
            except Exception:  # noqa: BLE001
                logger.debug("abort() failed on %r", worker, exc_info=True)
