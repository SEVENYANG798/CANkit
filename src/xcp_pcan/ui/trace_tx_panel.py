"""Single-frame CAN transmit panel for Trace Viewer."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_VISIBLE_TX_ROWS = 2
_TX_ROW_HEIGHT = 28

from xcp_pcan.can.frame_util import fit_can_payload, next_can_fd_length
from xcp_pcan.can.transport import CanBusTransport

_COL_ID = 0
_COL_LEN = 1
_COL_DATA = 2
_COL_PERIOD = 3
_COL_FD = 4
_COL_EXT = 5
_COL_CYCLE = 6
_COL_SEND = 7

_HEADERS = ("ID", "Len", "Data (hex)", "Period(ms)", "FD", "Ext", "Cycle", "Send")


def _parse_can_id(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty ID")
    if raw.lower().startswith("0x") or any(c in "ABCDEFabcdef" for c in raw):
        arb = int(raw, 16)
    else:
        # Short bare values are treated as hex (common in CAN tools): "100" → 0x100.
        arb = int(raw, 16) if len(raw) <= 3 else int(raw, 10)
    if arb < 0 or arb > 0x1FFFFFFF:
        raise ValueError(f"CAN ID out of range: {arb:#x}")
    return arb


def _parse_hex_data(text: str) -> bytes:
    raw = (text or "").strip()
    if not raw:
        return b""
    raw = raw.replace(",", " ").replace(";", " ")
    # Allow continuous hex: AABBCCDD
    if " " not in raw and re.fullmatch(r"[0-9A-Fa-f]+", raw):
        if len(raw) % 2:
            raise ValueError("hex data length must be even")
        return bytes.fromhex(raw)
    parts = raw.split()
    out: list[int] = []
    for p in parts:
        if not re.fullmatch(r"[0-9A-Fa-f]{1,2}", p):
            raise ValueError(f"bad byte: {p!r}")
        out.append(int(p, 16))
    return bytes(out)


def _safe_int(value: object, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class TraceTxPanel(QWidget):
    """Editable TX rows: ID / length / data / period / FD / cycle send."""

    def __init__(
        self,
        *,
        transport_getter: Callable[[], CanBusTransport | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._transport_getter = transport_getter
        self._cycle_timers: dict[int, QTimer] = {}  # id(checkbox) -> timer

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(list(_HEADERS))
        self._table.setFont(QFont("Consolas", 9))
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(True)
        self._table.verticalHeader().setDefaultSectionSize(_TX_ROW_HEIGHT)
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        self._table.setColumnWidth(_COL_ID, 80)
        self._table.setColumnWidth(_COL_LEN, 48)
        self._table.setColumnWidth(_COL_DATA, 280)
        self._table.setColumnWidth(_COL_PERIOD, 80)
        self._table.setColumnWidth(_COL_FD, 44)
        self._table.setColumnWidth(_COL_EXT, 44)
        self._table.setColumnWidth(_COL_CYCLE, 52)
        self._table.setColumnWidth(_COL_SEND, 64)
        hdr.setSectionResizeMode(_COL_DATA, QHeaderView.ResizeMode.Stretch)
        # Viewport shows 2 rows; extra TX rows scroll inside the table.
        header_h = max(hdr.sizeHint().height(), 24)
        table_h = header_h + _VISIBLE_TX_ROWS * _TX_ROW_HEIGHT + 4
        self._table.setFixedHeight(table_h)
        self._table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        btn_add = QPushButton("Add")
        btn_add.clicked.connect(lambda: self.add_row())
        btn_del = QPushButton("Remove")
        btn_del.clicked.connect(self._remove_selected)
        btn_stop = QPushButton("Stop all cycles")
        btn_stop.clicked.connect(self.stop_all_cycles)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Transmit"))
        bar.addWidget(btn_add)
        bar.addWidget(btn_del)
        bar.addWidget(btn_stop)
        bar.addStretch(1)
        self._lbl_bus = QLabel("")
        self._lbl_bus.setStyleSheet("color: #a0a0a0;")
        bar.addWidget(self._lbl_bus)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.addLayout(bar)
        root.addWidget(self._table)

        self._bus_timer = QTimer(self)
        self._bus_timer.setInterval(500)
        self._bus_timer.timeout.connect(self.refresh_bus_hint)
        self._bus_timer.start()
        self.refresh_bus_hint()

        # Starter rows similar to common tooling.
        self.add_row(can_id="0x100", length=8, data="00 00 00 00 00 00 00 00", period=100)
        self.add_row(can_id="0x200", length=8, data="AA BB CC DD EE FF 00 11", period=50)

    def refresh_bus_hint(self) -> None:
        transport = self._transport_getter()
        open_bus = transport is not None and transport.is_open
        if open_bus:
            self._lbl_bus.setText("Bus open · Data e.g. 11 22 33 44")
            self._lbl_bus.setStyleSheet("color: #3d9a5f;")
        else:
            self._lbl_bus.setText("Connect first (main window) · Data e.g. 11 22 33 44")
            self._lbl_bus.setStyleSheet("color: #b07020;")

    def add_row(
        self,
        *,
        can_id: str = "0x100",
        length: int = 8,
        data: str = "00 00 00 00 00 00 00 00",
        period: int = 100,
        fd: bool = False,
        extended: bool = False,
    ) -> int:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, _COL_ID, QTableWidgetItem(can_id))
        self._table.setItem(row, _COL_LEN, QTableWidgetItem(str(length)))
        self._table.setItem(row, _COL_DATA, QTableWidgetItem(data))
        self._table.setItem(row, _COL_PERIOD, QTableWidgetItem(str(period)))

        chk_fd = QCheckBox()
        chk_fd.setChecked(fd)
        chk_fd.setStyleSheet("margin-left:12px;")
        self._table.setCellWidget(row, _COL_FD, self._center_widget(chk_fd))

        chk_ext = QCheckBox()
        chk_ext.setChecked(extended)
        chk_ext.setStyleSheet("margin-left:12px;")
        self._table.setCellWidget(row, _COL_EXT, self._center_widget(chk_ext))

        chk_cycle = QCheckBox()
        chk_cycle.setStyleSheet("margin-left:12px;")
        chk_cycle.toggled.connect(self._on_cycle_checkbox_toggled)
        self._table.setCellWidget(row, _COL_CYCLE, self._center_widget(chk_cycle))

        btn = QPushButton("Send")
        btn.clicked.connect(self._on_send_clicked)
        self._table.setCellWidget(row, _COL_SEND, btn)
        return row

    @staticmethod
    def _center_widget(inner: QWidget) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        lay.addWidget(inner)
        lay.addStretch(1)
        return wrap

    def _checkbox_at(self, row: int, col: int) -> QCheckBox | None:
        wrap = self._table.cellWidget(row, col)
        if wrap is None:
            return None
        return wrap.findChild(QCheckBox)

    def _row_of_widget(self, widget: QWidget | None) -> int:
        if widget is None:
            return -1
        for r in range(self._table.rowCount()):
            for c in (_COL_CYCLE, _COL_SEND, _COL_FD, _COL_EXT):
                cell = self._table.cellWidget(r, c)
                if cell is widget or (cell is not None and cell.isAncestorOf(widget)):
                    return r
        return -1

    def _on_send_clicked(self) -> None:
        row = self._row_of_widget(self.sender())  # type: ignore[arg-type]
        if row >= 0:
            self._send_row(row)

    def _on_cycle_checkbox_toggled(self, enabled: bool) -> None:
        row = self._row_of_widget(self.sender())  # type: ignore[arg-type]
        if row < 0:
            return
        self._on_cycle_toggled(row, enabled)

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            chk = self._checkbox_at(row, _COL_CYCLE)
            self._stop_cycle_for_checkbox(chk)
            self._table.removeRow(row)

    def stop_all_cycles(self) -> None:
        for key in list(self._cycle_timers):
            self._stop_cycle_key(key)
        for row in range(self._table.rowCount()):
            chk = self._checkbox_at(row, _COL_CYCLE)
            if chk is not None and chk.isChecked():
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)

    def _stop_cycle_key(self, key: int) -> None:
        timer = self._cycle_timers.pop(key, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _stop_cycle_for_checkbox(self, chk: QCheckBox | None) -> None:
        if chk is None:
            return
        self._stop_cycle_key(id(chk))

    def _uncheck_cycle(self, chk: QCheckBox | None) -> None:
        self._stop_cycle_for_checkbox(chk)
        if chk is not None and chk.isChecked():
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)

    def _on_cycle_toggled(self, row: int, enabled: bool) -> None:
        chk = self._checkbox_at(row, _COL_CYCLE)
        if not enabled:
            self._stop_cycle_for_checkbox(chk)
            return
        try:
            period = int((self._table.item(row, _COL_PERIOD).text() or "0").strip())
        except (AttributeError, ValueError):
            period = 0
        if period < 1:
            QMessageBox.warning(self, "Transmit", "Period(ms) must be >= 1 for cycle send.")
            self._uncheck_cycle(chk)
            return
        transport = self._transport_getter()
        if transport is None or not transport.is_open:
            QMessageBox.information(self, "Transmit", "Open the CAN bus first (Connect).")
            self._uncheck_cycle(chk)
            return
        # First send not quiet so parse/ID errors are visible.
        if not self._send_row(row, quiet=False):
            self._uncheck_cycle(chk)
            return
        self._stop_cycle_for_checkbox(chk)
        timer = QTimer(self)
        timer.setInterval(period)

        def _tick(box: QCheckBox | None = chk) -> None:
            r = self._row_of_widget(box)
            if r < 0 or box is None or not box.isChecked():
                self._uncheck_cycle(box)
                return
            self._send_row(r, quiet=True)

        timer.timeout.connect(_tick)
        if chk is not None:
            self._cycle_timers[id(chk)] = timer
        timer.start()

    def _read_row(self, row: int) -> tuple[int, bytes, bool, bool]:
        id_item = self._table.item(row, _COL_ID)
        len_item = self._table.item(row, _COL_LEN)
        data_item = self._table.item(row, _COL_DATA)
        arb = _parse_can_id(id_item.text() if id_item else "")
        try:
            length = int((len_item.text() if len_item else "8").strip())
        except ValueError as exc:
            raise ValueError("Len must be an integer") from exc
        if length < 0:
            raise ValueError("Len must be >= 0")
        data = _parse_hex_data(data_item.text() if data_item else "")
        fd_chk = self._checkbox_at(row, _COL_FD)
        ext_chk = self._checkbox_at(row, _COL_EXT)
        fd = bool(fd_chk is not None and fd_chk.isChecked())
        ext = bool(ext_chk is not None and ext_chk.isChecked())
        if fd:
            # Honor requested Len; round up to a legal FD DLC (also cover data size).
            length = next_can_fd_length(max(length, len(data)))
            data = fit_can_payload(data[:length], fd=True, max_len=length)
            if len(data) < length:
                data = data + bytes(length - len(data))
            elif len(data) > length:
                data = data[:length]
        else:
            length = max(0, min(8, length))
            data = data[:length]
            if len(data) < length:
                data = data + bytes(length - len(data))
        if arb > 0x7FF:
            ext = True
        return arb, data, fd, ext

    def _send_row(self, row: int, *, quiet: bool = False) -> bool:
        if row < 0 or row >= self._table.rowCount():
            return False
        transport = self._transport_getter()
        if transport is None or not transport.is_open:
            if not quiet:
                QMessageBox.information(self, "Transmit", "Open the CAN bus first (Connect).")
            else:
                self._uncheck_cycle(self._checkbox_at(row, _COL_CYCLE))
            return False
        try:
            arb, data, fd, ext = self._read_row(row)
            if fd and not transport.fd:
                if not quiet:
                    QMessageBox.warning(
                        self,
                        "Transmit",
                        "CAN bus is Classic — uncheck FD on the TX row, "
                        "or Disconnect and Connect with CAN FD enabled.",
                    )
                else:
                    self._uncheck_cycle(self._checkbox_at(row, _COL_CYCLE))
                return False
            transport.send(
                arb,
                data,
                is_extended_id=ext,
                fd=fd,
                bitrate_switch=fd,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                QMessageBox.warning(self, "Transmit failed", str(exc))
            else:
                self._uncheck_cycle(self._checkbox_at(row, _COL_CYCLE))
            return False

    def export_rows(self) -> list[dict]:
        rows: list[dict] = []
        for r in range(self._table.rowCount()):
            fd_chk = self._checkbox_at(r, _COL_FD)
            ext_chk = self._checkbox_at(r, _COL_EXT)
            rows.append(
                {
                    "id": self._table.item(r, _COL_ID).text() if self._table.item(r, _COL_ID) else "",
                    "len": self._table.item(r, _COL_LEN).text() if self._table.item(r, _COL_LEN) else "8",
                    "data": self._table.item(r, _COL_DATA).text() if self._table.item(r, _COL_DATA) else "",
                    "period": self._table.item(r, _COL_PERIOD).text()
                    if self._table.item(r, _COL_PERIOD)
                    else "100",
                    "fd": bool(fd_chk is not None and fd_chk.isChecked()),
                    "ext": bool(ext_chk is not None and ext_chk.isChecked()),
                }
            )
        return rows

    def load_rows(self, rows: list[dict]) -> None:
        self.stop_all_cycles()
        self._table.setRowCount(0)
        if not rows:
            self.add_row()
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            self.add_row(
                can_id=str(row.get("id", "0x100")),
                length=_safe_int(row.get("len", 8), 8),
                data=str(row.get("data", "00 00 00 00 00 00 00 00")),
                period=_safe_int(row.get("period", 100), 100),
                fd=bool(row.get("fd", False)),
                extended=bool(row.get("ext", False)),
            )
        if self._table.rowCount() == 0:
            self.add_row()

    def save_to_settings(self, settings, key: str = "trace_viewer/tx_rows") -> None:
        settings.setValue(key, json.dumps(self.export_rows()))

    def restore_from_settings(self, settings, key: str = "trace_viewer/tx_rows") -> None:
        raw = settings.value(key, "", str)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            self.load_rows(data)
