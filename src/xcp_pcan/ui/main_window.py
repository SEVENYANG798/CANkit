"""Main application window — orchestrates panels and XCP session."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.can.transport import CanBusTransport
from xcp_pcan.project import default_project_path, save_project
from xcp_pcan.symbols.symbol_lists import SymbolListGroup
from xcp_pcan.symbols.types import SymbolStore
from xcp_pcan.ui.app_log import (
    FILTER_LABELS,
    GuiLogBuffer,
    GuiLogHandler,
    LogCategory,
    LogEntry,
    LogFilterMode,
    apply_verbose_levels,
    ensure_filter_capture,
)
from xcp_pcan.ui.connection_panel import ConnectionPanel
from xcp_pcan.ui.models import SymbolTableModel, WatchTableModel
from xcp_pcan.ui.mw_connect import ConnectMixin
from xcp_pcan.ui.mw_measure import MeasureMixin
from xcp_pcan.ui.mw_project import ProjectMixin
from xcp_pcan.ui.mw_symbols import SymbolToolsMixin
from xcp_pcan.ui.security_panel import SecurityPanel
from xcp_pcan.ui.symbol_panel import SymbolPanel
from xcp_pcan.ui.trace_buffer import TraceBuffer
from xcp_pcan.ui.trace_viewer_dialog import TraceViewerDialog
from xcp_pcan.ui.watch_tabs import WatchTabs
from xcp_pcan.ui.worker_util import abort_qthread, abort_workers, close_progress
from xcp_pcan.xcp.daq import DaqEngine
from xcp_pcan.xcp.master import XcpMaster, XcpSessionConfig
from xcp_pcan.xcp.poller import Poller

logger = logging.getLogger(__name__)
SETTINGS_ORG = "CANkit"
SETTINGS_APP = "CANkit"


class _Bridge(QObject):
    sample = Signal(object)
    daq_sample = Signal(object)
    log_line = Signal(str)
    log_entry = Signal(object)
    error = Signal(str)


class MainWindow(ConnectMixin, SymbolToolsMixin, ProjectMixin, MeasureMixin, QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CANkit — PCAN / Vector")
        # Free edge / corner resize; children use Expanding so panes fill the frame.
        self.setMinimumSize(720, 480)
        self.resize(1280, 820)
        self._closing = False

        self._transport = CanBusTransport()
        self._master = XcpMaster(self._transport, XcpSessionConfig())
        self._store = SymbolStore()
        self._symbol_groups: list[SymbolListGroup] = []
        self._bridge = _Bridge()
        self._bridge.sample.connect(self._on_poll_sample)
        self._bridge.daq_sample.connect(self._on_daq_sample)
        self._bridge.log_line.connect(self._append_log)
        self._bridge.log_entry.connect(self._on_log_entry)
        self._bridge.error.connect(self._on_error)

        self._log_buffer = GuiLogBuffer(
            on_rebuild=self._rebuild_log_view,
            on_line=self._append_log_line,
        )
        self._log_handler: GuiLogHandler | None = None

        self._poller = Poller(
            self._master,
            on_sample=lambda s: self._bridge.sample.emit(s),
            on_error=lambda m: self._bridge.error.emit(m),
        )
        self._daq = DaqEngine(
            self._master,
            on_sample=lambda s: self._bridge.daq_sample.emit(s),
            on_error=lambda m: self._bridge.error.emit(m),
        )

        self._symbol_model = SymbolTableModel()
        self._poll_watch_model = WatchTableModel()
        self._daq_watch_model = WatchTableModel()
        # Keep ~10 min / up to 300k pts per series so the left side does not
        # vanish after a few seconds at typical DAQ rates.
        self._poll_traces = TraceBuffer(maxlen=300_000, max_age_s=600.0)
        self._daq_traces = TraceBuffer(maxlen=300_000, max_age_s=600.0)
        self._poll_chart = None
        self._daq_chart = None
        self._project_path = None
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self._load_worker = None
        self._load_cancelled = False
        self._load_apply_transport = True
        self._load_on_done = None
        self._load_progress = None
        self._a2l_update_worker = None
        self._a2l_update_progress = None
        self._a2l_update_cancelled = False
        self._elf2a2l_worker = None
        self._elf2a2l_progress = None
        self._connect_worker = None
        self._connect_progress = None
        self._connect_meta = None
        self._connect_keep_front = None
        self._pending_after_xcp = None
        self._trace_viewer = None
        self._xcp_viewer = None
        self._diag_console = None
        self._help_dialog = None

        self._build_ui()
        self._install_log_handler()
        self._set_connected_ui(False)
        self._restore_last_project()
        # After first paint + project restore, open Connect like CAN → Connect...
        QTimer.singleShot(0, self._open_connect_dialog)

    # --- UI construction -------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setStyleSheet("QToolBar { spacing: 2px; padding: 0px; margin: 0px; }")
        self.addToolBar(toolbar)

        project_menu = QMenu(self)
        for text, shortcut, slot in (
            ("Open", QKeySequence.StandardKey.Open, self._open_project),
            ("Save", QKeySequence.StandardKey.Save, self._save_project),
            ("Save as", QKeySequence.StandardKey.SaveAs, self._save_project_as),
        ):
            act = QAction(text, self)
            act.setShortcut(shortcut)
            act.triggered.connect(slot)
            project_menu.addAction(act)
        btn_project = QToolButton()
        btn_project.setText("Project")
        btn_project.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn_project.setMenu(project_menu)
        btn_project.setToolTip("Open, save, or save the project as")
        toolbar.addWidget(btn_project)

        self._can_menu = QMenu(self)
        self._act_can_connect = QAction("Connect", self)
        self._act_can_connect.setShortcut(QKeySequence("Ctrl+B"))
        self._act_can_connect.setToolTip("Configure hardware / XCP and open the CAN bus")
        self._act_can_connect.triggered.connect(self._open_connect_dialog)
        self._act_can_disconnect = QAction("Disconnect", self)
        self._act_can_disconnect.setShortcut(QKeySequence("Ctrl+D"))
        self._act_can_disconnect.setToolTip("Close the CAN bus")
        self._act_can_disconnect.triggered.connect(self._disconnect)
        self._can_menu.addAction(self._act_can_connect)
        self._can_menu.addAction(self._act_can_disconnect)
        self._act_log = QAction("Status", self)
        self._act_log.setCheckable(True)
        self._act_log.setToolTip("Show or hide the Status panel")
        self._act_log.toggled.connect(self._on_log_visible)
        self._can_menu.addAction(self._act_log)
        btn_can = QToolButton()
        btn_can.setText("CAN")
        btn_can.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn_can.setMenu(self._can_menu)
        btn_can.setToolTip("CAN bus connect and Trace tools")
        toolbar.addWidget(btn_can)
        toolbar.addSeparator()

        self._act_load_symbols = QAction("Load A2L/ELF", self)
        self._act_load_symbols.triggered.connect(self._load_symbols)
        self._act_resolve_elf = QAction("Resolve addresses from ELF", self)
        self._act_resolve_elf.setToolTip(
            "Fill A2L symbols that still have address 0 using an ELF symbol table "
            "(in-memory only; does not rewrite the A2L file)."
        )
        self._act_resolve_elf.triggered.connect(self._resolve_from_elf)
        self._act_update_a2l = QAction("Update A2L from ELF", self)
        self._act_update_a2l.setToolTip(
            "ASAP2-style: rewrite ECU_ADDRESS in an A2L file from ELF symbols "
            "(SYMBOL_LINK or object name). Runs in the background — UI stays usable."
        )
        self._act_update_a2l.triggered.connect(self._update_a2l_from_elf)
        self._act_elf2a2l = QAction("Create A2L from ELF", self)
        self._act_elf2a2l.setToolTip(
            "Create an A2L from ELF global variables (symbol table + optional DWARF types)."
        )
        self._act_elf2a2l.triggered.connect(self._elf_to_a2l)

        data_menu = QMenu(self)
        data_menu.addAction(self._act_load_symbols)
        data_menu.addAction(self._act_resolve_elf)
        data_menu.addAction(self._act_update_a2l)
        data_menu.addAction(self._act_elf2a2l)
        btn_data = QToolButton()
        btn_data.setText("Data Editor")
        btn_data.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn_data.setMenu(data_menu)
        btn_data.setToolTip("A2L / ELF load, resolve, update, and create")
        toolbar.addWidget(btn_data)
        toolbar.addSeparator()

        self._act_xcp_viewer = QAction("XCP Viewer", self)
        self._act_xcp_viewer.setToolTip(
            "Symbols list and Polling / DAQ watch (measurement & calibration)."
        )
        self._act_xcp_viewer.triggered.connect(self._open_xcp_viewer)
        toolbar.addAction(self._act_xcp_viewer)
        self._act_diag = QAction("Diagnostic Console", self)
        self._act_diag.setToolTip(
            "UDS over ISO-TP (e.g. 0x22 Read DID / 0x2E Write DID) on the open CAN bus."
        )
        self._act_diag.triggered.connect(self._open_diag_console)
        toolbar.addAction(self._act_diag)
        help_menu = QMenu(self)
        act_help = QAction("Help", self)
        act_help.setToolTip("Features and how to use this tool")
        act_help.triggered.connect(self._show_help)
        act_about = QAction("About", self)
        act_about.setToolTip("Show CANkit version and check for updates")
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_help)
        help_menu.addAction(act_about)
        btn_help = QToolButton()
        btn_help.setText("Help")
        btn_help.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn_help.setMenu(help_menu)
        btn_help.setToolTip("Help and About")
        toolbar.addWidget(btn_help)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Owned off-layout; shown inside Connect dialog (CAN → Connect...).
        self.connection = ConnectionPanel(self)
        self.connection.hide()
        self.security = SecurityPanel()
        self.security.unlock_clicked.connect(self._unlock_now)
        self.security.set_cal_page_clicked.connect(self._apply_cal_page)
        self.security.get_cal_page_clicked.connect(self._query_cal_page)
        self.connection.advanced_extra.addWidget(self.security)

        self._splitter = QSplitter(Qt.Orientation.Vertical)

        # Trace is the main workspace; Symbols/DAQ live in XCP Viewer tool window.
        self._trace_viewer = TraceViewerDialog(
            self._settings,
            self,
            transport=self._transport,
            on_connect=self._connect,
            on_disconnect=self._disconnect,
        )
        self._trace_viewer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._can_menu.addSeparator()
        for act in self._trace_viewer.menu_actions():
            self._can_menu.addAction(act)

        self._mid = QSplitter(Qt.Orientation.Horizontal)
        self.symbols = SymbolPanel(self._symbol_model)
        self.symbols.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.symbols.add_selected.connect(self._add_selected_to_watch)
        self.symbols.add_all_filtered.connect(self._add_all_filtered_to_watch)
        self.symbols.load_lists.connect(self._load_symbol_lists)
        self.symbols.filter_changed.connect(lambda _t: self._filter_symbols())
        self.symbols.clear_filter.connect(self._filter_symbols)

        self.watch = WatchTabs(self._poll_watch_model, self._daq_watch_model)
        self.watch.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.watch.tab_changed.connect(self._on_tab_changed)
        self.watch.poll_start.connect(self._start_poll)
        self.watch.poll_stop.connect(self._stop_poll)
        self.watch.poll_write.connect(
            lambda: self._write_selected(self._poll_watch_model, self.watch.tbl_poll)
        )
        self.watch.poll_curves.connect(self._open_poll_chart)
        self.watch.poll_export.connect(lambda: self._export_csv(self._poll_traces, "polling"))
        self.watch.poll_remove.connect(
            lambda: self._remove_watch(self._poll_watch_model, self.watch.tbl_poll)
        )
        self.watch.poll_clear.connect(self._clear_poll_watch)
        self.watch.daq_start.connect(self._start_daq)
        self.watch.daq_stop.connect(self._stop_daq)
        self.watch.daq_write.connect(
            lambda: self._write_selected(self._daq_watch_model, self.watch.tbl_daq)
        )
        self.watch.daq_curves.connect(self._open_daq_chart)
        self.watch.daq_export.connect(lambda: self._export_csv(self._daq_traces, "daq"))
        self.watch.daq_remove.connect(
            lambda: self._remove_watch(self._daq_watch_model, self.watch.tbl_daq)
        )
        self.watch.daq_clear.connect(self._clear_daq_watch)

        self._mid.addWidget(self.symbols)
        self._mid.addWidget(self.watch)
        self._configure_splitter(
            self._mid,
            stretch=(1, 3),
            default_sizes=[320, 960],
            settings_key="ui_mid_splitter",
        )

        self._log_panel = self._build_log_panel()
        self._splitter.addWidget(self._trace_viewer)
        self._splitter.addWidget(self._log_panel)
        self._configure_splitter(
            self._splitter,
            stretch=(5, 1),
            default_sizes=[620, 160],
            # v3: Trace|log (XCP Viewer is a separate tool window)
            settings_key="ui_main_splitter_v3",
        )
        root.addWidget(self._splitter, stretch=1)

        log_open = bool(self._settings.value("ui_log_open", True, type=bool))
        self._act_log.blockSignals(True)
        self._act_log.setChecked(log_open)
        self._act_log.blockSignals(False)
        self._apply_log_visible(log_open, persist=False)

        status = QStatusBar()
        status.setSizeGripEnabled(True)
        self.setStatusBar(status)
        self.statusBar().showMessage("Ready")

    def _configure_splitter(
        self,
        splitter: QSplitter,
        *,
        stretch: tuple[int, ...],
        default_sizes: list[int],
        settings_key: str,
    ) -> None:
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        splitter.setOpaqueResize(True)
        for i, factor in enumerate(stretch):
            splitter.setStretchFactor(i, factor)
        restored = self._settings.value(settings_key)
        if isinstance(restored, QByteArray) and not restored.isEmpty():
            splitter.restoreState(restored)
        else:
            splitter.setSizes(default_sizes)
        splitter.splitterMoved.connect(
            lambda _pos, _idx, s=splitter, key=settings_key: self._settings.setValue(
                key, s.saveState()
            )
        )

    def _apply_log_visible(self, visible: bool, *, persist: bool = True) -> None:
        sizes = self._splitter.sizes()
        total = sum(sizes) or 780
        if visible:
            self._log_panel.setVisible(True)
            log_h = int(self._settings.value("ui_log_height", 160) or 160)
            log_h = max(80, min(log_h, total - 200))
            self._splitter.setSizes([max(total - log_h, 200), log_h])
        else:
            if sizes and sizes[-1] > 48:
                self._settings.setValue("ui_log_height", sizes[-1])
            self._log_panel.setVisible(False)
            self._splitter.setSizes([total, 0])
        if persist:
            self._settings.setValue("ui_log_open", visible)

    def _on_log_visible(self, visible: bool) -> None:
        self._apply_log_visible(bool(visible))

    def _build_log_panel(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Filter:"))
        self.cmb_log_filter = QComboBox()
        for mode, label in FILTER_LABELS:
            self.cmb_log_filter.addItem(label, mode)
        self.cmb_log_filter.currentIndexChanged.connect(self._on_log_filter_changed)
        bar.addWidget(self.cmb_log_filter)
        self.chk_log_verbose = QCheckBox("Verbose (CAN/XCP DEBUG)")
        self.chk_log_verbose.toggled.connect(self._on_log_verbose_toggled)
        bar.addWidget(self.chk_log_verbose)
        bar.addStretch(1)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear_log)
        bar.addWidget(btn_clear)
        layout.addLayout(bar)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.txt_log.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self.txt_log.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self.txt_log, stretch=1)

        saved_filter = self._settings.value("log_filter", LogFilterMode.ALL.value)
        try:
            mode = LogFilterMode(str(saved_filter))
        except ValueError:
            mode = LogFilterMode.ALL
        idx = self.cmb_log_filter.findData(mode)
        self.cmb_log_filter.blockSignals(True)
        if idx >= 0:
            self.cmb_log_filter.setCurrentIndex(idx)
        self.cmb_log_filter.blockSignals(False)
        verbose = self._settings.value("log_verbose", False, type=bool)
        self.chk_log_verbose.blockSignals(True)
        self.chk_log_verbose.setChecked(bool(verbose))
        self.chk_log_verbose.blockSignals(False)
        self._log_buffer.set_filter(mode)
        apply_verbose_levels(verbose=bool(verbose))
        ensure_filter_capture(mode)
        return body

    def _install_log_handler(self) -> None:
        handler = GuiLogHandler(lambda e: self._bridge.log_entry.emit(e), level=logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg = logging.getLogger("xcp_pcan")
        pkg.addHandler(handler)
        pkg.setLevel(logging.DEBUG)
        apply_verbose_levels(verbose=self.chk_log_verbose.isChecked())
        self._log_handler = handler

    # --- Status panel / shutdown ------------------------------------------------

    @Slot(object)
    def _on_log_entry(self, entry: object) -> None:
        if isinstance(entry, LogEntry):
            self._log_buffer.add_entry(entry)

    @Slot(str)
    def _append_log(
        self,
        line: str,
        category: LogCategory = LogCategory.APP,
        level: int = logging.INFO,
    ) -> None:
        self._log_buffer.append(line, category=category, level=level, logger_name=__name__)

    def _append_log_line(self, line: str) -> None:
        self._write_log_line(line, error="[ERROR]" in line)

    def _write_log_line(self, line: str, *, error: bool) -> None:
        cursor = self.txt_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.txt_log.toPlainText():
            cursor.insertText("\n")
        fmt = QTextCharFormat()
        if error:
            fmt.setForeground(QColor("#c0392b"))
        else:
            fmt.setForeground(self.txt_log.palette().color(QPalette.ColorRole.Text))
        cursor.insertText(line, fmt)
        self.txt_log.setTextCursor(cursor)
        self.txt_log.ensureCursorVisible()

    def _rebuild_log_view(self) -> None:
        if not hasattr(self, "txt_log"):
            return
        self.txt_log.clear()
        for entry in self._log_buffer.filtered_entries():
            error = entry.category == LogCategory.ERROR or entry.level >= logging.ERROR
            self._write_log_line(entry.format_line(), error=error)
        bar = self.txt_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _clear_log(self) -> None:
        self._log_buffer.clear()

    @Slot(int)
    def _on_log_filter_changed(self, _index: int) -> None:
        mode = self.cmb_log_filter.currentData()
        if not isinstance(mode, LogFilterMode):
            mode = LogFilterMode.ALL
        self._log_buffer.set_filter(mode)
        apply_verbose_levels(verbose=self.chk_log_verbose.isChecked())
        ensure_filter_capture(mode)
        self._settings.setValue("log_filter", mode.value)

    @Slot(bool)
    def _on_log_verbose_toggled(self, checked: bool) -> None:
        apply_verbose_levels(verbose=checked)
        mode = self.cmb_log_filter.currentData()
        if isinstance(mode, LogFilterMode):
            ensure_filter_capture(mode)
        self._settings.setValue("log_verbose", checked)
        self._append_log(f"Verbose logging {'ON' if checked else 'OFF'}", category=LogCategory.APP)

    def _abort_background_jobs(self) -> None:
        """Stop loaders before the window is destroyed (avoids QThread crashes)."""
        if self._connect_busy() and self._connect_worker is not None:
            abort_qthread(
                self._connect_worker,
                timeout_ms=5000,
                abort=self._connect_worker.abort,
            )
            self._connect_worker = None
            close_progress(self._connect_progress)
            self._connect_progress = None

        self._load_cancelled = True
        self._a2l_update_cancelled = True
        self._load_on_done = None
        abort_workers(
            [
                getattr(self, "_load_worker", None),
                getattr(self, "_a2l_update_worker", None),
                getattr(self, "_elf2a2l_worker", None),
            ],
            timeout_ms=5000,
        )
        for attr in ("_load_progress", "_a2l_update_progress", "_elf2a2l_progress"):
            close_progress(getattr(self, attr, None))
            setattr(self, attr, None)
        self._load_worker = None
        self._a2l_update_worker = None
        self._elf2a2l_worker = None
        if self._trace_viewer is not None:
            try:
                self._trace_viewer.abort_loading()
            except Exception:  # noqa: BLE001
                logger.debug("Trace viewer abort failed", exc_info=True)

    def _close_tool_windows(self) -> None:
        """Tool windows are top-level (no transient parent); close them with the app."""
        for attr in (
            "_xcp_viewer",
            "_diag_console",
            "_help_dialog",
            "_poll_chart",
            "_daq_chart",
        ):
            win = getattr(self, attr, None)
            if win is None:
                continue
            try:
                win.close()
            except Exception:  # noqa: BLE001
                logger.debug("Closing tool window %s failed", attr, exc_info=True)
            setattr(self, attr, None)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._stop_poll()
        self._stop_daq()
        self._abort_background_jobs()
        if self._trace_viewer is not None:
            try:
                self._trace_viewer.persist_settings()
            except Exception:  # noqa: BLE001
                logger.debug("Trace persist failed", exc_info=True)
        self._close_tool_windows()
        if self._log_handler is not None:
            logging.getLogger("xcp_pcan").removeHandler(self._log_handler)
            self._log_handler = None
        try:
            path = self._project_path or default_project_path()
            save_project(path, self._collect_project())
            self._settings.setValue("last_project", str(path))
        except Exception:  # noqa: BLE001
            logger.exception("Auto-save project failed")
        try:
            if self._master.is_connected:
                self._master.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("DISCONNECT during close failed", exc_info=True)
        self._master.close_listener()
        self._safe_shutdown_bus()
        super().closeEvent(event)
