"""GUI log filter unit tests."""

from __future__ import annotations

import logging

from xcp_pcan.ui.app_log import (
    GuiLogBuffer,
    LogCategory,
    LogEntry,
    LogFilterMode,
    category_for_logger,
    entry_matches,
)


def test_category_for_logger():
    assert category_for_logger("xcp_pcan.can.transport", logging.DEBUG) == LogCategory.CAN
    assert category_for_logger("xcp_pcan.xcp.master", logging.DEBUG) == LogCategory.XCP
    assert category_for_logger("xcp_pcan.ui.main_window", logging.INFO) == LogCategory.APP
    assert category_for_logger("xcp_pcan.xcp.master", logging.ERROR) == LogCategory.ERROR


def test_filter_modes():
    can = LogEntry(1.0, LogCategory.CAN, logging.DEBUG, "TX id=0x1")
    xcp = LogEntry(1.0, LogCategory.XCP, logging.DEBUG, "CTO TX")
    app = LogEntry(1.0, LogCategory.APP, logging.INFO, "Poll started")
    err = LogEntry(1.0, LogCategory.ERROR, logging.ERROR, "boom", "xcp_pcan.xcp.master")

    assert entry_matches(can, LogFilterMode.ALL)
    assert entry_matches(can, LogFilterMode.CAN)
    assert not entry_matches(can, LogFilterMode.XCP)
    assert not entry_matches(can, LogFilterMode.ERRORS)

    assert entry_matches(xcp, LogFilterMode.XCP)
    assert not entry_matches(xcp, LogFilterMode.CAN)

    assert entry_matches(app, LogFilterMode.APP)
    assert not entry_matches(app, LogFilterMode.ERRORS)

    assert entry_matches(err, LogFilterMode.ERRORS)
    assert entry_matches(err, LogFilterMode.XCP)


def test_buffer_filter_and_clear():
    lines: list[str] = []
    rebuilds = {"n": 0}

    buf = GuiLogBuffer(
        capacity=100,
        on_rebuild=lambda: rebuilds.__setitem__("n", rebuilds["n"] + 1),
        on_line=lines.append,
    )
    buf.append("hello app", category=LogCategory.APP)
    buf.append("TX frame", category=LogCategory.CAN, level=logging.DEBUG)
    buf.append("ERROR: fail", category=LogCategory.APP)

    assert len(lines) == 3  # all mode
    buf.set_filter(LogFilterMode.CAN)
    assert rebuilds["n"] >= 1
    assert len(buf.filtered_entries()) == 1
    assert buf.filtered_entries()[0].category == LogCategory.CAN

    buf.set_filter(LogFilterMode.ERRORS)
    assert len(buf.filtered_entries()) == 1
    assert "fail" in buf.filtered_entries()[0].message

    buf.clear()
    assert buf.filtered_text() == ""
