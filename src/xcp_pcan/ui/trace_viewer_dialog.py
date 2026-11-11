"""Offline + live CAN Trace Viewer (ASC / BLF / CSV and online bus)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTableView,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.can.error_frame import decode_pcan_error_frame
from xcp_pcan.can.frame_util import CanFrame
from xcp_pcan.can.transport import CanBusTransport
from xcp_pcan.trace_log.dbc_decode import DbcDatabase
from xcp_pcan.trace_log.types import TraceFrame
from xcp_pcan.trace_log.writers import save_trace, sniff_save_format
from xcp_pcan.ui.trace_signal_plot_dialog import TraceSignalPlotDialog
from xcp_pcan.ui.trace_tx_panel import TraceTxPanel
from xcp_pcan.ui.trace_viewer_model import TraceViewerModel
from xcp_pcan.ui.trace_viewer_worker import TraceLoadWorker
from xcp_pcan.ui.window_util import raise_window

logger = logging.getLogger(__name__)


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


class TraceViewerDialog(QWidget):
    """CAN Trace panel (embedded in the main window)."""

    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
        *,
        transport: CanBusTransport | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._transport = transport
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._pending_live_after_connect = False
        self._worker: TraceLoadWorker | None = None
        self._progress: QProgressDialog | None = None
        self._path = ""
        self._dbc: DbcDatabase | None = None
        self._signal_plot_dialog: TraceSignalPlotDialog | None = None
        self._load_cancelled = False
        self._live = False
        self._live_paused = False
        self._live_t0: float | None = None
        self._live_index = 0
        self._auto_scroll = True
        self._pending_live: list[CanFrame] = []
        self._live_dropped = 0
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
        # Ctrl+click / Shift+click multi-select for partial Save.
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)
        vh = self._table.verticalHeader()
        vh.setVisible(False)
        # Fixed row height — critical for large offline traces (100k+ rows).
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(20)
        self._table.setWordWrap(False)
        self._table.setFont(QFont("Consolas", 9))
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFont(QFont("Consolas", 10))
        # Fixed strip: avoid Expanding QTextEdit eating the table area when empty.
        self._detail.setFixedHeight(120)
        self._detail.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._detail.setPlaceholderText(
            "Select a frame to show ID / data / ASCII and DBC signal decode."
        )
        self._detail.hide()

        self.edt_text = QLineEdit()
        self.edt_text.setPlaceholderText("Search ID / data / name / channel / rx|tx|err")
        self.edt_text.setClearButtonEnabled(True)
        self.edt_text.setToolTip(
            "Filter as you type (ID / data / name / channel / rx|tx|err). Enter applies immediately."
        )
        self._text_filter_timer = QTimer(self)
        self._text_filter_timer.setSingleShot(True)
        self._text_filter_timer.setInterval(150)
        self._text_filter_timer.timeout.connect(self._apply_text_filter)
        self.edt_text.textChanged.connect(self._on_text_filter_edited)
        self.edt_text.returnPressed.connect(self._apply_text_filter_now)
        self.lbl_dbc = QLabel("DBC: (none)")
        self.lbl_dbc.setStyleSheet("color:#616161;")

        # Actions live under main CAN menu; Trace strip has shortcut icons + search.
        self.act_open = QAction("Log Replay", self)
        self.act_open.setToolTip("Open offline ASC / BLF / CSV")
        self.act_open.triggered.connect(self._open)
        self.act_live = QAction("Start Monitor", self)
        self.act_live.setToolTip(
            "Observe RX/TX on the open CAN bus. Connect opens the bus; XCP is not required."
        )
        self.act_live.triggered.connect(self._toggle_live)
        self.act_load_dbc = QAction("Load DBC", self)
        self.act_load_dbc.setToolTip("Load a .dbc to decode signals for the selected frame.")
        self.act_load_dbc.triggered.connect(self._load_dbc)
        self.act_clear_dbc = QAction("Remove DBC", self)
        self.act_clear_dbc.triggered.connect(self._clear_dbc)
        self.act_signal_plot = QAction("Graphic", self)
        self.act_signal_plot.setToolTip(
            "Pick a DBC signal and plot / list its values over the current Trace "
            "(offline log or captured Monitor buffer). Requires Load DBC."
        )
        self.act_signal_plot.triggered.connect(self._open_signal_plot)
        self.act_signal_plot.setEnabled(False)
        self.act_reset_filters = QAction("Reset filters", self)
        self.act_reset_filters.setToolTip("Clear text, ID▾ and Dir▾ filters.")
        self.act_reset_filters.triggered.connect(self._clear_filter)
        self.act_collapse = QAction("Collapse by ID", self)
        self.act_collapse.setCheckable(True)
        self.act_collapse.setToolTip(
            "Collapsed: one row per CAN-ID (PCAN Receive style, latest data + cycle/count).\n"
            "Expanded: chronological timeline log (PCAN Trace style)."
        )
        self.act_collapse.toggled.connect(self._on_collapse_toggled)
        self.act_auto_scroll = QAction("Auto-scroll", self)
        self.act_auto_scroll.setCheckable(True)
        self.act_auto_scroll.setChecked(True)
        self.act_auto_scroll.toggled.connect(self._on_auto_scroll_toggled)
        self.act_detail = QAction("Detail panel", self)
        self.act_detail.setCheckable(True)
        self.act_detail.setToolTip(
            "Show a fixed panel under the table with the selected frame's "
            "ID / data / ASCII and DBC signal decode (Load DBC first for signals)."
        )
        self.act_detail.toggled.connect(self._on_detail_toggled)

        style = self.style()
        assert style is not None
        self.btn_tb_connect = self._make_tool_button(
            self._bus_plug_icon(connected=True),
            "Connect",
            self._toolbar_connect,
        )
        self.btn_tb_disconnect = self._make_tool_button(
            self._bus_plug_icon(connected=False),
            "Disconnect",
            self._toolbar_disconnect,
        )
        self.btn_tb_start = self._make_tool_button(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
            "Start Monitor",
            self._toolbar_start,
        )
        self.btn_tb_pause = self._make_tool_button(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPause),
            "Pause Monitor",
            self._toolbar_pause,
        )
        self.btn_tb_stop = self._make_tool_button(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop),
            "Stop Monitor",
            self._toolbar_stop,
        )
        self.btn_tb_graphic = self._make_tool_button(
            self._line_chart_icon(),
            "Graphic",
            self._open_signal_plot,
        )
        self.btn_tb_graphic.setEnabled(False)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(2, 0, 4, 0)
        filter_row.setSpacing(2)
        for btn in (
            self.btn_tb_connect,
            self.btn_tb_disconnect,
            self.btn_tb_start,
            self.btn_tb_pause,
            self.btn_tb_stop,
            self.btn_tb_graphic,
        ):
            filter_row.addWidget(btn)
        filter_row.addWidget(self.edt_text, stretch=1)
        filter_row.addWidget(self.lbl_dbc)
        self.sync_toolbar_actions(bus_open=False, busy=False)

        upper = QWidget()
        upper_lay = QVBoxLayout(upper)
        upper_lay.setContentsMargins(0, 0, 0, 0)
        upper_lay.addWidget(self._table, stretch=1)
        upper_lay.addWidget(self._detail)

        self._tx_panel = TraceTxPanel(transport_getter=lambda: self._transport, parent=self)
        self._tx_panel.restore_from_settings(self._settings)
        # 2-row TX table (right-click Add/Remove); more rows scroll inside.
        self._tx_panel.setMinimumHeight(86)
        self._tx_panel.setMaximumHeight(102)

        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(upper)
        self._splitter.addWidget(self._tx_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setChildrenCollapsible(False)
        # Prefer a short Transmit strip; ignore older tall TX splitter sizes.
        self._splitter.setSizes([580, 90])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        root.addLayout(filter_row)
        root.addWidget(self._splitter, stretch=1)
        self._status(
            "Trace ready — Open a file, or Start Monitor after Connect opens the CAN bus."
        )

        self._apply_column_widths()
        collapsed_raw = self._settings.value("trace_viewer/collapsed", False)
        if isinstance(collapsed_raw, str):
            collapsed = collapsed_raw.strip().lower() in {"1", "true", "yes"}
        else:
            collapsed = bool(collapsed_raw)
        if collapsed:
            self.act_collapse.blockSignals(True)
            self.act_collapse.setChecked(True)
            self.act_collapse.blockSignals(False)
            self._model.set_collapsed(True)
            self.act_collapse.setText("Expand timeline")
            self._apply_column_widths()

        # Defer last-DBC restore so the window can paint before cantools parses
        # a large .dbc (otherwise first Trace Viewer open feels stuck).
        last_dbc = self._settings.value("trace_viewer/dbc", "", str)
        if last_dbc and Path(last_dbc).is_file():
            self._pending_dbc_path = last_dbc
            QTimer.singleShot(0, self._restore_pending_dbc)
        else:
            self._pending_dbc_path = None

    def notify_bus_open(self) -> None:
        """Main window finished Connect — start Monitor if user asked from the prompt."""
        if not self._pending_live_after_connect:
            return
        self._pending_live_after_connect = False
        if self._transport is not None and self._transport.is_open and not self._live:
            self._start_live()

    def clear_pending_live_after_connect(self) -> None:
        self._pending_live_after_connect = False

    def set_on_connect(self, on_connect: Callable[[], None] | None) -> None:
        self._on_connect = on_connect

    def set_on_disconnect(self, on_disconnect: Callable[[], None] | None) -> None:
        self._on_disconnect = on_disconnect

    @staticmethod
    def _make_tool_button(icon, tip: str, slot) -> QToolButton:
        btn = QToolButton()
        btn.setIcon(icon)
        btn.setToolTip(tip)
        btn.setAutoRaise(True)
        btn.setFixedSize(28, 28)
        btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _bus_plug_icon(*, connected: bool, size: int = 20) -> QIcon:
        """Side-view plug: green mated = Connect; red unplugged+X = Disconnect."""

        def _paint(color: QColor, *, mated: bool, badge: bool, badge_fill: QColor) -> QPixmap:
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Socket face (left vertical bar)
            sock = QPen(color, 1.8)
            sock.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(sock)
            painter.drawLine(QPointF(3.0, 5.0), QPointF(3.0, 15.0))
            painter.drawLine(QPointF(3.0, 5.0), QPointF(5.2, 5.0))
            painter.drawLine(QPointF(3.0, 15.0), QPointF(5.2, 15.0))
            # Prongs
            prong = QPen(color, 1.7)
            prong.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(prong)
            if mated:
                painter.drawLine(QPointF(4.2, 8.0), QPointF(10.0, 8.0))
                painter.drawLine(QPointF(4.2, 12.0), QPointF(10.0, 12.0))
                body_x = 9.5
            else:
                painter.drawLine(QPointF(7.0, 8.0), QPointF(11.2, 8.0))
                painter.drawLine(QPointF(7.0, 12.0), QPointF(11.2, 12.0))
                body_x = 10.8
            # Plug body
            painter.setPen(QPen(color, 1.3))
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(body_x, 5.5, 6.2, 9.0), 1.6, 1.6)
            if badge:
                cx = float(size) - 4.8
                cy = float(size) - 4.8
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(badge_fill)
                painter.drawEllipse(QPointF(cx, cy), 4.2, 4.2)
                xpen = QPen(QColor("#FFFFFF"), 1.3)
                xpen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(xpen)
                d = 2.0
                painter.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
                painter.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))
            painter.end()
            return pm

        icon = QIcon()
        if connected:
            icon.addPixmap(
                _paint(QColor("#2E7D32"), mated=True, badge=False, badge_fill=QColor()),
                QIcon.Mode.Normal,
            )
            icon.addPixmap(
                _paint(QColor("#BDBDBD"), mated=True, badge=False, badge_fill=QColor()),
                QIcon.Mode.Disabled,
            )
        else:
            icon.addPixmap(
                _paint(QColor("#C62828"), mated=False, badge=True, badge_fill=QColor("#C62828")),
                QIcon.Mode.Normal,
            )
            icon.addPixmap(
                _paint(QColor("#BDBDBD"), mated=False, badge=True, badge_fill=QColor("#9E9E9E")),
                QIcon.Mode.Disabled,
            )
        return icon

    @staticmethod
    def _line_chart_icon(size: int = 20) -> QIcon:
        """Simple polyline chart icon (closer to a signal plot than file/list icons)."""
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        axis = QPen(QColor("#616161"), 1.2)
        painter.setPen(axis)
        painter.drawLine(3, size - 3, size - 2, size - 3)
        painter.drawLine(3, 2, 3, size - 3)
        line = QPen(QColor("#1565C0"), 1.6)
        line.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        line.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(line)
        pts = [
            QPoint(4, size - 6),
            QPoint(7, size - 11),
            QPoint(10, size - 8),
            QPoint(13, size - 14),
            QPoint(16, size - 10),
            QPoint(size - 3, 4),
        ]
        for a, b in zip(pts, pts[1:], strict=False):
            painter.drawLine(a, b)
        painter.end()
        return QIcon(pm)

    def sync_toolbar_actions(self, *, bus_open: bool, busy: bool = False) -> None:
        """Enable connect / disconnect / monitor icons from main-window link state."""
        self.btn_tb_connect.setEnabled(not busy and not bus_open)
        # Only when bus is open — already disconnected stays grey / unclickable.
        self.btn_tb_disconnect.setEnabled(bool(bus_open) and not busy)
        live = self._live
        paused = self._live_paused
        self.btn_tb_start.setEnabled(not live or paused)
        self.btn_tb_pause.setEnabled(live and not paused)
        self.btn_tb_stop.setEnabled(live)

    def _toolbar_connect(self) -> None:
        if self._on_connect is not None:
            self._on_connect()
        else:
            self._prompt_open_bus()

    def _toolbar_disconnect(self) -> None:
        if self._on_disconnect is not None:
            self._on_disconnect()

    def _toolbar_start(self) -> None:
        if self._live and self._live_paused:
            self._live_paused = False
            self._status("Monitor resumed.")
            self._sync_live_actions()
            return
        if not self._live:
            self._start_live()

    def _toolbar_pause(self) -> None:
        if not self._live or self._live_paused:
            return
        self._live_paused = True
        self._pending_live.clear()
        self._status("Monitor paused.")
        self._sync_live_actions()

    def _toolbar_stop(self) -> None:
        if self._live:
            self._stop_live()
            self._status("Monitor stopped.")

    def _sync_live_actions(self) -> None:
        if self._live and self._live_paused:
            self.act_live.setText("Resume Monitor")
        elif self._live:
            self.act_live.setText("Stop Monitor")
        else:
            self.act_live.setText("Start Monitor")
        bus_open = bool(self._transport is not None and self._transport.is_open)
        self.sync_toolbar_actions(bus_open=bus_open)

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
            self._status("Monitor stopped (CAN disconnected).")

    def persist_settings(self) -> None:
        self._tx_panel.save_to_settings(self._settings)
        self._settings.setValue("trace_viewer/splitter", self._splitter.sizes())
        self._settings.setValue("trace_viewer/collapsed", self._model.is_collapsed())

    def menu_actions(self) -> list[QAction]:
        """Actions for the main-window CAN menu (Trace tools)."""
        return [
            self.act_open,
            self.act_live,
            self.act_load_dbc,
            self.act_clear_dbc,
            self.act_signal_plot,
            self.act_reset_filters,
            self.act_collapse,
            self.act_auto_scroll,
            self.act_detail,
        ]

    def _apply_column_widths(self) -> None:
        """Data stretches; other columns size to header text (PCAN-like default)."""
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        # Collapsed/Timeline: Dir before Type; Name hidden without DBC.
        data_col = self._model.data_column()
        name_col = self._model.name_column()
        show_name = self._model.has_dbc_names()
        # Mode switch changes Name index — clear leftover hide flags first.
        for col in range(self._model.columnCount()):
            self._table.setColumnHidden(col, False)
        self._table.setColumnHidden(name_col, not show_name)
        fm = self._table.fontMetrics()
        # Fit names already seen in the buffer — not the longest name in the whole DBC.
        longest_name = self._model.longest_appeared_dbc_name() if show_name else ""
        for col in range(self._model.columnCount()):
            if col == name_col and not show_name:
                continue
            if col == data_col:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
                continue
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            title = self._model.headerData(
                col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
            )
            text = str(title) if title is not None else ""
            # Padding for ▾ filter affordance / cell margins; keep narrow cols readable.
            width = max(36, fm.horizontalAdvance(text) + 24)
            if col == 0:
                width = max(width, 40)
            if col == name_col and longest_name:
                width = max(width, fm.horizontalAdvance(longest_name) + 28)
            self._table.setColumnWidth(col, width)

    def _maybe_widen_name_column(self) -> None:
        """Grow Name column when a longer appeared DBC name shows up (live / load)."""
        name_col = self._model.name_column()
        if self._table.isColumnHidden(name_col):
            return
        longest = self._model.longest_appeared_dbc_name()
        if not longest:
            return
        need = self._table.fontMetrics().horizontalAdvance(longest) + 28
        if need > self._table.columnWidth(name_col):
            self._table.setColumnWidth(name_col, need)

    def _on_auto_scroll_toggled(self, enabled: bool) -> None:
        self._auto_scroll = bool(enabled)
        if enabled and not self._model.is_collapsed():
            self._scroll_trace_to_bottom()

    def _ensure_timeline_auto_scroll(self) -> None:
        """Timeline (PCAN Trace style) should chase the latest frame."""
        if self._model.is_collapsed():
            return
        if not self.act_auto_scroll.isChecked():
            # setChecked fires toggled → _on_auto_scroll_toggled → scroll
            self.act_auto_scroll.setChecked(True)
            return
        self._scroll_trace_to_bottom()

    def _scroll_trace_to_bottom(self) -> None:
        """Scroll after the view has applied row changes (Qt needs a tick)."""
        if self._model.rowCount() <= 0:
            return

        def _go() -> None:
            last = self._model.rowCount() - 1
            if last < 0:
                return
            # scrollToBottom alone can lag behind insertRows/modelReset;
            # pin the scrollbar max as well.
            self._table.scrollTo(
                self._model.index(last, 0),
                QAbstractItemView.ScrollHint.PositionAtBottom,
            )
            bar = self._table.verticalScrollBar()
            bar.setValue(bar.maximum())

        _go()
        QTimer.singleShot(0, _go)

    def _on_collapse_toggled(self, collapsed: bool) -> None:
        self._model.set_collapsed(collapsed)
        self.act_collapse.setText("Expand timeline" if collapsed else "Collapse by ID")
        self._apply_column_widths()
        if not collapsed:
            self._ensure_timeline_auto_scroll()
        self._refresh_status()
        self._on_selection()

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
            # Windows native dialog needs ';' between extensions in one group.
            "Trace (*.asc;*.blf;*.csv);;ASC (*.asc);;BLF (*.blf);;CSV (*.csv);;All (*.*)",
        )
        if path:
            self.load_path(path)

    def _on_table_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        act_clear = menu.addAction("Clear")
        act_save_all = menu.addAction("Export all")
        act_save_all.setToolTip("Export the whole buffer as ASC / BLF / CSV.")
        act_save_sel = menu.addAction("Export selected")
        act_save_sel.setToolTip(
            "Export selected rows (Ctrl+click / Shift+click). ASC / BLF / CSV."
        )
        sel_n = len(self._table.selectionModel().selectedRows())
        act_save_sel.setEnabled(sel_n > 0)
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is act_clear:
            self._clear_table()
        elif chosen is act_save_all:
            self._save(selected_only=False)
        elif chosen is act_save_sel:
            self._save(selected_only=True)

    def _selected_frames(self) -> list[TraceFrame]:
        """Frames for selected table rows (timeline rows, or all msgs of selected IDs)."""
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return []
        rows = sorted({idx.row() for idx in indexes})
        if self._model.is_collapsed():
            ids: set[int] = set()
            for row in rows:
                fr = self._model.frame_at(row)
                if fr is not None:
                    ids.add(fr.arbitration_id)
            if not ids:
                return []
            return [fr for fr in self._model.all_frames() if fr.arbitration_id in ids]
        out: list[TraceFrame] = []
        for row in rows:
            fr = self._model.frame_at(row)
            if fr is not None:
                out.append(fr)
        return out

    def _save(self, *, selected_only: bool = False) -> None:
        # Flush pending live frames so Export matches what is on screen.
        if self._pending_live:
            self._flush_live_frames()
        if selected_only:
            frames = self._selected_frames()
            if not frames:
                QMessageBox.information(
                    self,
                    self._title(),
                    "No rows selected.\n"
                    "Use Ctrl+click or Shift+click to select, then Export selected.",
                )
                return
        else:
            frames = self._model.all_frames()
            if not frames:
                QMessageBox.information(
                    self,
                    self._title(),
                    "No frames to export. Start Monitor or Open a trace first.",
                )
                return
        start = self._settings.value("trace_viewer/save_dir", "", str)
        if not start:
            start = self._settings.value("trace_viewer/dir", "", str)
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export selected trace" if selected_only else "Export trace",
            start,
            "ASC (*.asc);;BLF (*.blf);;CSV (*.csv);;All (*.*)",
        )
        if not path:
            return
        # Ensure suffix matches the chosen filter when the user omits it.
        lower = path.lower()
        if not (lower.endswith(".asc") or lower.endswith(".blf") or lower.endswith(".csv")):
            if "BLF" in selected:
                path += ".blf"
            elif "CSV" in selected:
                path += ".csv"
            else:
                path += ".asc"
        try:
            fmt = sniff_save_format(path)
        except ValueError as exc:
            QMessageBox.warning(self, self._title(), str(exc))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = save_trace(path, frames, fmt=fmt)
        except Exception as exc:  # noqa: BLE001 — show path + reason in UI
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, self._title(), f"Failed to export:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self._settings.setValue("trace_viewer/save_dir", str(Path(path).parent))
        kind = "selected" if selected_only else "all"
        self._status(f"Saved {n} frames ({kind}) → {Path(path).name}")

    def _restore_pending_dbc(self) -> None:
        path = getattr(self, "_pending_dbc_path", None)
        self._pending_dbc_path = None
        if not path or not Path(path).is_file():
            return
        try:
            self._apply_dbc(path, remember=False)
        except Exception:  # noqa: BLE001
            pass

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
        self.act_signal_plot.setEnabled(False)
        self.btn_tb_graphic.setEnabled(False)
        self._settings.setValue("trace_viewer/dbc", "")
        self._apply_column_widths()
        self._refresh_status()
        self._on_selection()

    def _open_signal_plot(self) -> None:
        if self._dbc is None:
            QMessageBox.information(
                self, self._title(), "Load a DBC first, then open Graphic."
            )
            return
        # configure_tool_window() clears the Qt parent — must keep a Python ref
        # or the dialog is GC'd right after show() (flash and vanish).
        frames = self._model.all_frames()
        old = self._signal_plot_dialog
        if old is not None:
            old.close()
        dlg = TraceSignalPlotDialog(self._dbc, frames, parent=self)
        self._signal_plot_dialog = dlg
        raise_window(dlg)

    def _apply_dbc(self, path: str, *, remember: bool) -> None:
        dbc = DbcDatabase(path)
        self._dbc = dbc
        self._model.set_dbc_names(dbc.message_names())
        self.lbl_dbc.setText(f"DBC: {Path(path).name} ({dbc.message_count} msgs)")
        self.act_signal_plot.setEnabled(True)
        self.btn_tb_graphic.setEnabled(True)
        if remember:
            self._settings.setValue("trace_viewer/dbc", path)
            self._settings.setValue("trace_viewer/dbc_dir", str(Path(path).parent))
        self._apply_column_widths()
        self._refresh_status()
        self._on_selection()

    def _clear_table(self) -> None:
        self._pending_live.clear()
        self._live_dropped = 0
        self._model.clear()
        self._detail.clear()
        self._live_index = 0
        self._live_t0 = None
        self._path = ""
        self._status("Trace cleared.")

    def _toggle_live(self) -> None:
        if self._live and self._live_paused:
            self._toolbar_start()
            return
        if self._live:
            self._toolbar_stop()
            return
        self._start_live()

    def _on_detail_toggled(self, checked: bool) -> None:
        self._detail.setVisible(checked)
        if not checked:
            return
        # Populate immediately; empty Detail looks like wasted whitespace.
        sel = self._table.selectionModel()
        if not sel.selectedRows() and self._model.rowCount() > 0:
            self._table.selectRow(0)
        self._on_selection()

    def _prompt_open_bus(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(self._title())
        box.setText(
            "CAN bus is not open.\n"
            "XCP session is optional — Trace only needs the hardware link."
        )
        btn_connect = box.addButton("Connect", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_connect)
        box.exec()
        if box.clickedButton() is not btn_connect:
            return
        if self._on_connect is None:
            QMessageBox.information(
                self,
                self._title(),
                "Connect is not available here — use Connect on the main window.",
            )
            return
        self._pending_live_after_connect = True
        self._on_connect()

    def _start_live(self) -> None:
        if self._transport is None or not self._transport.is_open:
            self._prompt_open_bus()
            return
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, self._title(), "Wait for the file load to finish.")
            return
        self._pending_live_after_connect = False
        self._model.clear()
        self._detail.clear()
        self._path = ""
        self._live_index = 0
        self._live_t0 = None
        self._pending_live.clear()
        self._live_dropped = 0
        self._live = True
        self._live_paused = False
        self._transport.add_listener(self._on_can_frame)
        self._status("Monitor started — waiting for CAN frames…")
        self._sync_live_actions()
        # Live timeline should open already following the newest frames.
        self._ensure_timeline_auto_scroll()

    def _stop_live(self) -> None:
        if not self._live:
            return
        self._live = False
        self._live_paused = False
        self._live_flush.stop()
        self._pending_live.clear()
        self._live_dropped = 0
        if self._transport is not None:
            try:
                self._transport.remove_listener(self._on_can_frame)
            except Exception:  # noqa: BLE001
                pass
        self._sync_live_actions()

    def _on_can_frame(self, frame: CanFrame) -> None:
        # Called from CAN RX thread — hop to GUI via queued signal.
        if self._live and not self._live_paused:
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
            self._live_dropped += overflow
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
                    bitrate_switch=bool(getattr(frame, "bitrate_switch", False)),
                    is_rx=bool(getattr(frame, "is_rx", True)),
                    is_error=bool(getattr(frame, "is_error", False)),
                    channel="",
                    name=name,
                )
            )
            self._live_index += 1
        self._model.append_live_batch(out)
        self._maybe_widen_name_column()
        if self._auto_scroll and not self._model.is_collapsed():
            self._scroll_trace_to_bottom()
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
        self._status(f"Loading {Path(path).name}…")

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
        # Prefer worker-held list (avoids huge QueuedConnection payloads on some builds).
        worker = self._worker
        held = getattr(worker, "result_frames", None) if worker is not None else None
        if isinstance(held, list):
            frames = held
        if not isinstance(frames, list):
            self._on_failed("Internal error: trace loader returned no frame list.")
            return
        try:
            # Large timeline freezes the view; collapse-by-ID stays usable (PCAN Receive style).
            large = len(frames) >= 80_000
            if large and not self._model.is_collapsed():
                self.act_collapse.blockSignals(True)
                self.act_collapse.setChecked(True)
                self.act_collapse.blockSignals(False)
                self._model.set_collapsed(True)
                self.act_collapse.setText("Expand timeline")
            self._model.set_frames(frames)
            if self._dbc is not None:
                self._model.set_dbc_names(self._dbc.message_names())
            self._apply_column_widths()
            if self.edt_text.text().strip():
                self._apply_text_filter()
            else:
                self._refresh_status()
            if large and self._model.is_collapsed():
                self._status(
                    f"Loaded {Path(self._path).name}: {len(frames)} frames "
                    "(opened collapsed for performance)"
                )
            else:
                self._status(f"Loaded {Path(self._path).name}: {len(frames)} frames")
        except Exception as exc:  # noqa: BLE001
            self._on_failed(f"Failed to display trace ({len(frames)} frames):\n{exc}")

    def _on_load_cancelled(self) -> None:
        self._load_cancelled = True
        self._status("Load cancelled.")

    def _on_failed(self, message: str) -> None:
        if self._load_cancelled:
            return
        self._status("Load failed.")
        QMessageBox.critical(self, self._title(), message)

    def _on_finished(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._worker = None

    def _on_header_clicked(self, section: int) -> None:
        if section == self._model.time_column():
            self._model.toggle_time_delta_mode()
            self._apply_column_widths()
            self._on_selection()
            return
        if section == self._model.id_column():
            self._show_id_filter()
        elif section == self._model.dir_column():
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
        dlg.move(self._header_menu_pos(self._model.id_column()))
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

        chosen = menu.exec(self._header_menu_pos(self._model.dir_column()))
        if chosen is None:
            return
        if chosen is act_err:
            self._model.set_errors_only(act_err.isChecked())
        else:
            self._model.set_dir_filter(chosen.data())
        self._refresh_status()

    def _clear_filter(self) -> None:
        self._text_filter_timer.stop()
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

    def _on_text_filter_edited(self, _text: str = "") -> None:
        # Debounce: avoid full-table reset on every keystroke for large traces.
        self._text_filter_timer.start()

    def _apply_text_filter_now(self) -> None:
        self._text_filter_timer.stop()
        self._apply_text_filter()

    def _apply_text_filter(self) -> None:
        self._model.set_text_filter(self.edt_text.text())
        self._refresh_status()

    def _status(self, message: str) -> None:
        """Trace events go to the Status panel (via package logging handler)."""
        text = (message or "").strip()
        if not text:
            return
        logger.info("%s", text)

    def _refresh_status(self) -> None:
        """Was a footer label; now only logs bus-error / drop warnings once."""
        notes: list[str] = []
        recent = self._model.all_frames()[-200:]
        recent_err = sum(1 for fr in recent if fr.is_error)
        if recent and recent_err * 2 >= len(recent):
            transport = self._transport
            if transport is not None and transport.is_open and not transport.fd:
                notes.append(
                    f"bus errors {recent_err}/{len(recent)} — open as CAN FD "
                    "(match PCAN-View), not Classic"
                )
            else:
                notes.append(f"bus errors {recent_err}/{len(recent)} — bitrate/wiring/ACK?")
        if self._live and self._live_dropped:
            notes.append(f"dropped {self._live_dropped} (UI overload)")
        if not notes:
            return
        key = "|".join(notes)
        if key == getattr(self, "_last_status_note", None):
            return
        self._last_status_note = key
        self._status("; ".join(notes))

    def _on_selection(self, *_args) -> None:
        if not self.act_detail.isChecked():
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
        row = indexes[0].row()
        if self._model.time_delta_mode():
            dt = self._model.delta_ms_at(row)
            t_txt = "Δt=—" if dt is None else f"Δt={dt:.3f}ms"
        else:
            t_txt = f"t={fr.timestamp:.6f}s"
        lines = [
            f"#{fr.index + 1}  {t_txt}  {fr.id_hex()}  "
            f"{'RX' if fr.is_rx else 'TX'}  Length={fr.dlc}  Type={fr.type_label()}",
            f"Data: {fr.data_hex()}",
        ]
        if fr.channel:
            lines.append(f"Channel: {fr.channel}")
        if disp_name:
            lines.append(f"Name: {disp_name}")
        if fr.is_error:
            err_txt = decode_pcan_error_frame(fr.arbitration_id, fr.data)
            if err_txt:
                lines.append(f"CAN error: {err_txt}")
                if self._transport is not None and self._transport.is_open and not self._transport.fd:
                    lines.append(
                        "Hint: this session is Classic CAN. PCAN-View FD traffic "
                        "(500k/2M + BRS) will show as stuff/bit ERR — enable CAN FD "
                        "in Connection, Disconnect, then Connect again."
                    )
                else:
                    lines.append(
                        "Hint: bitrate mismatch, missing 120Ω termination, wiring, "
                        "or no other node to ACK (alone on bus)."
                    )
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in fr.data)
        lines.append(f"ASCII: {ascii_part}")
        if self._dbc is not None and not fr.is_error:
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
