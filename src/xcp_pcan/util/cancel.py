"""Cancellation for long-running background work."""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class OperationCancelled(Exception):
    """Raised when the user cancels a background load/update."""


ProgressCallback = Callable[[str, float], None]


def report_progress(
    progress: ProgressCallback | None,
    message: str,
    fraction: float,
) -> None:
    """Invoke a progress callback; never swallow ``OperationCancelled``."""
    if progress is None:
        return
    try:
        progress(message, max(0.0, min(1.0, fraction)))
    except OperationCancelled:
        raise
    except Exception:  # noqa: BLE001 — ignore UI/signal quirks only
        logger.debug("progress callback failed", exc_info=True)
