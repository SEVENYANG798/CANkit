"""GUI log buffer with category filters (App / XCP / CAN / Errors)."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class LogCategory(str, Enum):
    APP = "app"
    XCP = "xcp"
    CAN = "can"
    ERROR = "error"


class LogFilterMode(str, Enum):
    ALL = "all"
    APP = "app"
    XCP = "xcp"
    CAN = "can"
    ERRORS = "errors"


FILTER_LABELS: list[tuple[LogFilterMode, str]] = [
    (LogFilterMode.ALL, "All"),
    (LogFilterMode.APP, "App"),
    (LogFilterMode.XCP, "XCP"),
    (LogFilterMode.CAN, "CAN frames"),
    (LogFilterMode.ERRORS, "Errors only"),
]


@dataclass(frozen=True)
class LogEntry:
    timestamp: float
    category: LogCategory
    level: int
    message: str
    logger_name: str = ""

    def format_line(self) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        ms = int((self.timestamp % 1) * 1000)
        tag = self.category.value.upper()
        return f"{ts}.{ms:03d} [{tag}] {self.message}"


def category_for_logger(name: str, level: int) -> LogCategory:
    if level >= logging.ERROR:
        return LogCategory.ERROR
    lower = (name or "").lower()
    if lower.startswith("xcp_pcan.can") or ".can." in lower or lower.endswith(".can"):
        return LogCategory.CAN
    if lower.startswith("xcp_pcan.xcp") or ".xcp." in lower or lower.endswith(".xcp"):
        return LogCategory.XCP
    if lower.startswith("xcp_pcan.ui") or lower.endswith("main_window"):
        return LogCategory.APP
    if level >= logging.WARNING:
        # Treat warnings from unknown loggers as errors-ish for filtering
        return LogCategory.ERROR
    return LogCategory.APP


def entry_matches(entry: LogEntry, mode: LogFilterMode) -> bool:
    if mode == LogFilterMode.ALL:
        return True
    if mode == LogFilterMode.ERRORS:
        return entry.category == LogCategory.ERROR or entry.level >= logging.ERROR
    if mode == LogFilterMode.APP:
        return entry.category == LogCategory.APP
    if mode == LogFilterMode.XCP:
        if entry.category == LogCategory.XCP:
            return True
        # Protocol errors / reconnect from xcp.* loggers
        return entry.category == LogCategory.ERROR and (
            entry.logger_name.startswith("xcp_pcan.xcp")
            or "xcp" in entry.message.lower()
            or "cto" in entry.message.lower()
            or "reconnect" in entry.message.lower()
        )
    if mode == LogFilterMode.CAN:
        return entry.category == LogCategory.CAN
    return True


class GuiLogBuffer:
    """Ring buffer of log entries with a display filter."""

    def __init__(
        self,
        capacity: int = 3000,
        *,
        on_rebuild: Callable[[], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=max(100, capacity))
        self._filter = LogFilterMode.ALL
        self._on_rebuild = on_rebuild
        self._on_line = on_line

    @property
    def filter_mode(self) -> LogFilterMode:
        return self._filter

    def set_filter(self, mode: LogFilterMode) -> None:
        if mode == self._filter:
            return
        self._filter = mode
        self._rebuild()

    def clear(self) -> None:
        self._entries.clear()
        self._rebuild()

    def add_entry(self, entry: LogEntry) -> None:
        self._entries.append(entry)
        if self._on_line and entry_matches(entry, self._filter):
            self._on_line(entry.format_line())

    def append(
        self,
        message: str,
        *,
        category: LogCategory = LogCategory.APP,
        level: int = logging.INFO,
        logger_name: str = "",
        timestamp: float | None = None,
    ) -> None:
        text = (message or "").rstrip()
        if not text:
            return
        # Promote obvious error lines from the UI
        cat = category
        if cat != LogCategory.ERROR and (
            level >= logging.ERROR
            or text.upper().startswith("ERROR")
            or " ERROR" in text.upper()
            or text.upper().startswith("WRITE ERROR")
            or text.upper().startswith("DAQ ERROR")
        ):
            cat = LogCategory.ERROR
            level = max(level, logging.ERROR)
        self.add_entry(
            LogEntry(
                timestamp=time.time() if timestamp is None else timestamp,
                category=cat,
                level=level,
                message=text,
                logger_name=logger_name,
            )
        )

    def filtered_entries(self) -> list[LogEntry]:
        return [e for e in self._entries if entry_matches(e, self._filter)]

    def filtered_text(self) -> str:
        return "\n".join(e.format_line() for e in self.filtered_entries())

    def _rebuild(self) -> None:
        if self._on_rebuild:
            self._on_rebuild()


class GuiLogHandler(logging.Handler):
    """Forward stdlib logging records into a GuiLogBuffer (thread-safe via callback)."""

    def __init__(
        self,
        emit_entry: Callable[[LogEntry], None],
        *,
        level: int = logging.DEBUG,
    ) -> None:
        super().__init__(level=level)
        self._emit_entry = emit_entry
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            cat = category_for_logger(record.name, record.levelno)
            entry = LogEntry(
                timestamp=record.created,
                category=cat,
                level=record.levelno,
                message=msg,
                logger_name=record.name,
            )
            self._emit_entry(entry)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def apply_verbose_levels(*, verbose: bool) -> None:
    """Raise/lower package loggers so CAN/XCP frame detail is available."""
    root_pkg = logging.getLogger("xcp_pcan")
    can_log = logging.getLogger("xcp_pcan.can")
    xcp_log = logging.getLogger("xcp_pcan.xcp")
    if verbose:
        # Keep package INFO for noise; detail loggers at DEBUG
        if root_pkg.level == logging.NOTSET or root_pkg.level > logging.DEBUG:
            pass
        can_log.setLevel(logging.DEBUG)
        xcp_log.setLevel(logging.DEBUG)
    else:
        can_log.setLevel(logging.INFO)
        xcp_log.setLevel(logging.INFO)


def ensure_filter_capture(mode: LogFilterMode) -> None:
    """When filtering to CAN/XCP, ensure those loggers emit DEBUG detail."""
    if mode == LogFilterMode.CAN:
        logging.getLogger("xcp_pcan.can").setLevel(logging.DEBUG)
    elif mode == LogFilterMode.XCP:
        logging.getLogger("xcp_pcan.xcp").setLevel(logging.DEBUG)
