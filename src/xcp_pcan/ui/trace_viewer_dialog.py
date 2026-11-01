"""Offline + live CAN Trace Viewer (ASC / BLF / CSV and online bus)."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.can.frame_util import CanFrame
from xcp_pcan.can.transport import CanBusTransport
from xcp_pcan.trace_log.dbc_decode import DbcDatabase
from xcp_pcan.trace_log.types import TraceFrame
from xcp_pcan.ui.trace_tx_panel import TraceTxPanel
from xcp_pcan.ui.trace_viewer_model import DIR_COLUMN, ID_COLUMN, TraceViewerModel
from xcp_pcan.ui.trace_viewer_worker import TraceLoadWorker
from xcp_pcan.ui.window_util import configure_tool_window


def _fmt_id(arb: int) -> str:
    width = 8 if arb > 0x7FF else 3
    return f"0x{arb:0{width}X}"


class _LiveBridge(QObject):
    """Marshal CAN RX-thread frames onto the GUI thread."""

    frame = Signal(object)


class IdFilterDialog(QDialog):
    """Multi-select CAN IDs; opened from the ID column header."""

    def __init__(
        self,
        counts: list[tuple[int, int]],
        selected: set[int] | None,
        dbc_names: dict[int, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter by CAN ID")
        self.resize(320, 420)
        self._list = QListWidget()
        self._list.setFont(QFont("Consolas", 9))
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        selected = selected or set()
        for arb, count in counts:
            name = dbc_names.get(arb, "")
            label = f"{_fmt_id(arb)}  ({count})"
            if name:
                label = f"{_fmt_id(arb)}  {name}  ({count})"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if arb in selected else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, arb)
            self._list.addItem(item)

        btn_all = QPushButton("Select all")
        btn_none = QPushButton("Select none")
        btn_all.clicked.connect(self._select_all)
        btn_none.clicked.connect(self._select_none)
        row = QHBoxLayout()
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Check IDs to show (none checked = show all):"))
        root.addLayout(row)
        root.addWidget(self._list, stretch=1)
        root.addWidget(buttons)

    def _select_all(self) -> None:
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self) -> None:
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def checked_ids(self) -> set[int]:
        ids: set[int] = set()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                arb = item.data(Qt.ItemDataRole.UserRole)
                if arb is not None:
                    ids.add(int(arb))
        return ids


class TraceViewerDialog(QDialog):
    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
        *,
        transport: CanBusTransport | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Trace Viewer")
        self.resize(1100, 640)
        configure_tool_window(self)
        self._settings = settings
        self._transport = transport
        self._worker: TraceLoadWorker | None = None
        self._progress: QProgressDialog | None = None
        self._path = ""
        self._dbc: DbcDatabase | None = None
        self._load_cancelled = False
        self._live = False
        self._live_t0: float | None = None
        self._live_index = 0
        self._auto_scroll = True
        self._pending_live: list[CanFrame] = []
        self._bridge = _LiveBridge(self)
        # Queued: RX thread must never touch widgets directly.
        self._bridge.frame.connect(
            self._queue_live_frame, Qt.ConnectionType.QueuedConnection
        )
        self._live_flush = QTimer(self)
        self._live_flush.setInterval(50)
        self._live_flush.timeout.connect(self._flush_live_frames)

        self._model = TraceViewerModel(self)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.setFont(QFont("Consolas", 9))
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFont(QFont("Consolas", 10))
        self._detail.setMaximumHeight(160)
        self._detail.hide()

        self.edt_text = QLineEdit()
        self.edt_text.setPlaceholderText("Search ID / data / name / channel / rx|tx|err")
        self.edt_text.setClearButtonEnabled(True)
        self.edt_text.returnPressed.connect(self._apply_text_filter)
        self.edt_text.editingFinished.connect(self._apply_text_filter)
        self.chk_scroll = QCheckBox("Auto-scroll")
        self.chk_scroll.setChecked(True)
        self.chk_scroll.toggled.connect(lambda v: setattr(self, "_auto_scroll", v))
        self.chk_detail = QCheckBox("Detail")
        self.chk_detail.setToolTip("Show selected-frame detail / DBC decode below the table.")
        self.chk_detail.toggled.connect(self._on_detail_toggled)
        self.lbl_status = QLabel(
            "Open a file, or Start Monitor after Connect opens the CAN bus (XCP optional)."
        )
        self.lbl_dbc = QLabel("DBC: (none)")

        btn_open = QPushButton("Open")
        btn_open.clicked.connect(self._open)
        btn_dbc = QPushButton("Load DBC")
        btn_dbc.setToolTip("Load a .dbc to decode signals for the selected frame.")
        btn_dbc.clicked.connect(self._load_dbc)
        btn_dbc_clear = QPushButton("Clear DBC")
        btn_dbc_clear.clicked.connect(self._clear_dbc)
        self.btn_live = QPushButton("Start Monitor")
        self.btn_live.setToolTip(
            "Observe RX/TX on the open CAN bus. Connect opens the bus; XCP is not required."
        )
        self.btn_live.clicked.connect(self._toggle_live)
        btn_clear = QPushButton("Clear")
        btn_clear.setToolTip("Clear the frame table.")
        btn_clear.clicked.connect(self._clear_table)
        btn_reset_f = QPushButton("Reset filters")
        btn_reset_f.setToolTip("Clear text, ID▾ and Dir▾ filters.")
        btn_reset_f.clicked.connect(self._clear_filter)

        top = QHBoxLayout()
        top.addWidget(btn_open)
        top.addWidget(self.btn_live)
        top.addWidget(btn_clear)
        top.addWidget(btn_dbc)
        top.addWidget(btn_dbc_clear)
        top.addWidget(self.edt_text, stretch=1)
        top.addWidget(btn_reset_f)
        top.addWidget(self.chk_scroll)
        top.addWidget(self.chk_detail)

        upper = QWidget()
        upper_lay = QVBoxLayout(upper)
        upper_lay.setContentsMargins(0, 0, 0, 0)
        upper_lay.addWidget(self._table, stretch=1)
        upper_lay.addWidget(self._detail)

        self._tx_panel = TraceTxPanel(transport_getter=lambda: self._transport, parent=self)
        self._tx_panel.restore_from_settings(self._settings)

        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(upper)
        self._splitter.addWidget(self._tx_panel)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setChildrenCollapsible(False)
        sizes = self._settings.value("trace_viewer/splitter")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([int(sizes[0]), int(sizes[1])])
            except (TypeError, ValueError):
                self._splitter.setSizes([420, 180])
        else:
            self._splitter.setSizes([420, 180])

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self._splitter, stretch=1)
        status_row = QHBoxLayout()
        status_row.addWidget(self.lbl_status, stretch=1)
        status_row.addWidget(self.lbl_dbc)
        root.addLayout(status_row)

        last_dbc = self._settings.value("trace_viewer/dbc", "", str)
        if last_dbc and Path(last_dbc).is_file():
            try:
                self._apply_dbc(last_dbc, remember=False)
            except Exception:  # noqa: BLE001
                pass

        for w, width in ((0, 70), (1, 100), (2, 100), (3, 72), (4, 40), (5, 420), (6, 60)):
            self._table.setColumnWidth(w, width)

    def set_transport(self, transport: CanBusTransport | None) -> None:
        if self._live:
            self._stop_live()
        if transport is not self._transport:
            self._tx_panel.stop_all_cycles()
        self._transport = transport

    def notify_bus_closed(self) -> None:
        self._tx_panel.stop_all_cycles()
        if self._live:
            self._stop_live()
            self.lbl_status.setText("Monitor stopped (CAN disconnected).")

    def persist_settings(self) -> None:
        self._tx_panel.save_to_settings(self._settings)
        self._settings.setValue("trace_viewer/splitter", self._splitter.sizes())

    def abort_loading(self) -> None:
        """Called when the main window is closing."""
        self._tx_panel.stop_all_cycles()
        try:
            self.persist_settings()
        except Exception:  # noqa: BLE001
            pass
        self._stop_live()
        self._load_cancelled = True
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.blockSignals(True)
            self._worker.wait(3000)
        self._worker = None
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _title(self) -> str:
        return "Trace Viewer"

    def _open(self) -> None:
        if self._live:
            self._stop_live()
        start = self._settings.value("trace_viewer/dir", "", str)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open trace",
            start,
            "Trace (*.asc *.blf *.csv);;ASC (*.asc);;BLF (*.blf);;CSV (*.csv);;All (*.*)",
        )
        if path:
            self.load_path(path)

    def _load_dbc(self) -> None:
        start = self._settings.value("trace_viewer/dbc_dir", "", str)
        if not start:
            start = self._settings.value("trace_viewer/dbc", "", str)
            start = str(Path(start).parent) if start else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load DBC",
            start,
            "DBC (*.dbc);;All (*.*)",
        )
        if not path:
            return
        try:
            self._apply_dbc(path, remember=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, self._title(), f"Failed to load DBC:\n{exc}")

    def _clear_dbc(self) -> None:
        self._dbc = None
        self._model.set_dbc_names(None)
        self.lbl_dbc.setText("DBC: (none)")
        self._settings.setValue("trace_viewer/dbc", "")
        self._refresh_status()
        self._on_selection()

    def _apply_dbc(self, path: str, *, remember: bool) -> None:
        dbc = DbcDatabase(path)
        self._dbc = dbc
        self._model.set_dbc_names(dbc.message_names())
        self.lbl_dbc.setText(f"DBC: {Path(path).name} ({dbc.message_count} msgs)")
        if remember:
            self._settings.setValue("trace_viewer/dbc", path)
            self._settings.setValue("trace_viewer/dbc_dir", str(Path(path).parent))
        self._refresh_status()
        self._on_selection()

    def _clear_table(self) -> None:
        self._pending_live.clear()
        self._model.clear()
        self._detail.clear()
        self._live_index = 0
        self._live_t0 = None
        self._path = ""
        self.lbl_status.setText("Cleared.")

    def _toggle_live(self) -> None:
        if self._live:
            self._stop_live()
            self.lbl_status.setText("Monitor stopped.")
            return
        self._start_live()

    def _on_detail_toggled(self, checked: bool) -> None:
        self._detail.setVisible(checked)
        if checked:
            self._on_selection()

    def _start_live(self) -> None:
        if self._transport is None or not self._transport.is_open:
            QMessageBox.information(
                self,
                self._title(),
                "Open the CAN bus first (main window Connect).\n"
                "XCP session is optional — Trace only needs the hardware link.",
            )
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, self._title(), "Wait for the file load to finish.")
            return
        self._model.clear()
        self._detail.clear()
        self._path = ""
        self._live_index = 0
        self._live_t0 = None
        self._pending_live.clear()
        self._live = True
        self._transport.add_listener(self._on_can_frame)
        self.btn_live.setText("Stop Monitor")
        self.lbl_status.setText("Monitor: waiting for CAN frames…")

    def _stop_live(self) -> None:
        if not self._live:
            return
        self._live = False
        self._live_flush.stop()
        self._pending_live.clear()
        if self._transport is not None:
            try:
                self._transport.remove_listener(self._on_can_frame)
            except Exception:  # noqa: BLE001
                pass
        self.btn_live.setText("Start Monitor")

    def _on_can_frame(self, frame: CanFrame) -> None:
        # Called from CAN RX thread — hop to GUI via queued signal.
        if self._live:
            self._bridge.frame.emit(frame)

    @Slot(object)
    def _queue_live_frame(self, frame: object) -> None:
        """GUI thread: buffer frames; flush on a timer so high bus load can't freeze UI."""
        if not self._live or not isinstance(frame, CanFrame):
            return
        self._pending_live.append(frame)
        # Cap backlog under error-frame storms / bus floods.
        if len(self._pending_live) > 4000:
            overflow = len(self._pending_live) - 2000
            self._pending_live = self._pending_live[overflow:]
        if not self._live_flush.isActive():
            self._live_flush.start()

    def _flush_live_frames(self) -> None:
        if not self._live:
            self._live_flush.stop()
            self._pending_live.clear()
            return
        batch = self._pending_live
        self._pending_live = []
        if not batch:
            self._live_flush.stop()
            return
        out: list[TraceFrame] = []
        now_wall = time.time()
        for frame in batch:
            ts = float(frame.timestamp) if frame.timestamp is not None else now_wall
            # Guard against HW timestamp / wall-clock mix (was showing ~-1.7e9 s).
            if self._live_t0 is not None and abs(ts - self._live_t0) > 1.0e6:
                ts = now_wall
            if self._live_t0 is None:
                self._live_t0 = ts
            rel = max(0.0, ts - self._live_t0)
            name = ""
            if self._dbc is not None:
                try:
                    name = self._dbc.message_name(frame.arbitration_id) or ""
                except Exception:  # noqa: BLE001
                    name = ""
            try:
                data = bytes(frame.data)
            except Exception:  # noqa: BLE001
                data = b""
            out.append(
                TraceFrame(
                    index=self._live_index,
                    timestamp=rel,
                    arbitration_id=int(frame.arbitration_id) & 0x1FFFFFFF,
                    data=data,
                    is_extended=bool(frame.is_extended_id),
                    is_fd=bool(frame.is_fd),
                    is_rx=bool(getattr(frame, "is_rx", True)),
                    is_error=bool(getattr(frame, "is_error", False)),
                    channel="",
                    name=name,
                )
            )
            self._live_index += 1
        self._model.append_live_batch(out)
        if self._auto_scroll and self._model.rowCount() > 0:
            self._table.scrollToBottom()
        self._refresh_status()

    def load_path(self, path: str) -> None:
        if self._live:
            self._stop_live()
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, self._title(), "A trace file is already loading.")
            return
        self._load_cancelled = False
        self._path = path
        self._settings.setValue("trace_viewer/dir", str(Path(path).parent))
        self._model.clear()
        self._detail.clear()
        self.lbl_status.setText(f"Loading {Path(path).name}…")

        progress = QProgressDialog("Loading trace…", "Cancel", 0, 0, self)
        progress.setWindowTitle(self._title())
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._cancel_load)
        progress.show()
        self._progress = progress

        worker = TraceLoadWorker(path, self)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_loaded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_load_cancelled)
        worker.finished.connect(self._on_finished)
        self._worker = worker
        worker.start()

    def _cancel_load(self) -> None:
        if self._load_cancelled:
            return
        self._load_cancelled = True
        if self._worker is not None:
            self._worker.requestInterruption()
        if self._progress is not None:
            self._progress.setLabelText("Cancelling…")
            self._progress.setCancelButton(None)
            self._progress.show()

    def _on_progress(self, count: int, note: str) -> None:
        if self._load_cancelled or self._progress is None:
            return
        self._progress.setLabelText(f"Loading {note}: {count} frames…")

    def _on_loaded(self, frames: object, summary: str) -> None:
        if self._load_cancelled:
            return
        assert isinstance(frames, list)
        self._model.set_frames(frames)
        if self._dbc is not None:
            self._model.set_dbc_names(self._dbc.message_names())
        self.lbl_status.setText(summary)
        self._apply_text_filter()

    def _on_load_cancelled(self) -> None:
        self._load_cancelled = True
        self.lbl_status.setText("Load cancelled.")

    def _on_failed(self, message: str) -> None:
        if self._load_cancelled:
            return
        self.lbl_status.setText("Load failed.")
        QMessageBox.critical(self, self._title(), message)

    def _on_finished(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._worker = None

    def _on_header_clicked(self, section: int) -> None:
        if section == ID_COLUMN:
            self._show_id_filter()
        elif section == DIR_COLUMN:
            self._show_dir_filter()

    def _header_menu_pos(self, section: int) -> QPoint:
        header = self._table.horizontalHeader()
        return header.mapToGlobal(
            QPoint(header.sectionViewportPosition(section), header.height())
        )

    def _show_id_filter(self) -> None:
        counts = self._model.id_counts()
        if not counts:
            QMessageBox.information(self, self._title(), "No frames loaded yet.")
            return
        dlg = IdFilterDialog(
            counts,
            self._model.id_filter(),
            self._model.dbc_names(),
            parent=self,
        )
        dlg.move(self._header_menu_pos(ID_COLUMN))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        checked = dlg.checked_ids()
        # None checked → show all; all checked → also show all (clear filter).
        if not checked or len(checked) == len(counts):
            self._model.set_id_filter(None)
        else:
            self._model.set_id_filter(checked)
        self._refresh_status()

    def _show_dir_filter(self) -> None:
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)

        def _add_dir(label: str, value: bool | None) -> QAction:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setData(value)
            if self._model.dir_filter() is value:
                act.setChecked(True)
            group.addAction(act)
            menu.addAction(act)
            return act

        _add_dir("All (RX + TX)", None)
        _add_dir("RX only", True)
        _add_dir("TX only", False)
        menu.addSeparator()
        act_err = QAction("Errors only", menu)
        act_err.setCheckable(True)
        act_err.setChecked(self._model.errors_only())
        menu.addAction(act_err)

        chosen = menu.exec(self._header_menu_pos(DIR_COLUMN))
        if chosen is None:
            return
        if chosen is act_err:
            self._model.set_errors_only(act_err.isChecked())
        else:
            self._model.set_dir_filter(chosen.data())
        self._refresh_status()

    def _clear_filter(self) -> None:
        self.edt_text.blockSignals(True)
        self.edt_text.clear()
        self.edt_text.blockSignals(False)
        self._model.apply_filter(
            id_values=None,
            id_mask=None,
            text="",
            rx_only=None,
            errors_only=False,
        )
        self._refresh_status()

    def _apply_text_filter(self) -> None:
        self._model.set_text_filter(self.edt_text.text())
        self._refresh_status()

    def _refresh_status(self) -> None:
        total = len(self._model.all_frames())
        shown = self._model.rowCount()
        notes: list[str] = []
        ids = self._model.id_filter()
        if ids:
            notes.append(f"{len(ids)} IDs")
        direction = self._model.dir_filter()
        if direction is True:
            notes.append("RX")
        elif direction is False:
            notes.append("TX")
        if self._model.errors_only():
            notes.append("ERR")
        note = f" [{', '.join(notes)}]" if notes else ""
        if self._live:
            self.lbl_status.setText(f"Monitor: showing {shown} / {total} frames{note}")
        elif self._path:
            self.lbl_status.setText(
                f"{Path(self._path).name}: showing {shown} / {total} frames{note}"
            )
        else:
            self.lbl_status.setText(f"Showing {shown} / {total} frames{note}")

    def _on_selection(self, *_args) -> None:
        if not self.chk_detail.isChecked():
            return
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            self._detail.clear()
            return
        fr = self._model.frame_at(indexes[0].row())
        if fr is None:
            self._detail.clear()
            return
        disp_name = self._model.display_name(fr)
        lines = [
            f"#{fr.index + 1}  t={fr.timestamp:.6f}s  {fr.id_hex()}  "
            f"{'RX' if fr.is_rx else 'TX'}  DLC={fr.dlc}",
            f"Data: {fr.data_hex()}",
        ]
        if fr.channel:
            lines.append(f"Channel: {fr.channel}")
        if disp_name:
            lines.append(f"Name: {disp_name}")
        flags = []
        if fr.is_fd:
            flags.append("CAN FD")
        if fr.is_extended:
            flags.append("Extended")
        if fr.is_error:
            flags.append("Error")
        if flags:
            lines.append("Flags: " + ", ".join(flags))
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in fr.data)
        lines.append(f"ASCII: {ascii_part}")
        if self._dbc is not None:
            decoded = self._dbc.format_detail(fr)
            if decoded:
                lines.append("")
                lines.append(decoded)
            else:
                lines.append("")
                lines.append("DBC: (no message for this ID)")
        self._detail.setPlainText("\n".join(lines))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._tx_panel.stop_all_cycles()
        try:
            self.persist_settings()
        except Exception:  # noqa: BLE001
            pass
        self._stop_live()
        super().closeEvent(event)
