"""Main application window — orchestrates panels and XCP session."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QObject, QSettings, Qt, Signal, Slot
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
from xcp_pcan.ui.collapsible import CollapsibleSection
from xcp_pcan.ui.connection_panel import ConnectionPanel
from xcp_pcan.ui.models import SymbolTableModel, WatchTableModel
from xcp_pcan.ui.mw_connect import ConnectMixin
from xcp_pcan.ui.mw_measure import MeasureMixin
from xcp_pcan.ui.mw_project import ProjectMixin
from xcp_pcan.ui.mw_symbols import SymbolToolsMixin
from xcp_pcan.ui.security_panel import SecurityPanel
from xcp_pcan.ui.symbol_panel import SymbolPanel
from xcp_pcan.ui.trace_buffer import TraceBuffer
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
        self._diag_console = None
        self._help_dialog = None

        self._build_ui()
        self._install_log_handler()
        self._set_connected_ui(False)
        self._restore_last_project()

    # --- UI construction -------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
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

        self._act_trace = QAction("Trace Viewer", self)
        self._act_trace.setToolTip(
            "Offline ASC/BLF/CSV and live CAN bus observation (DBC decode optional)."
        )
        self._act_trace.triggered.connect(self._open_trace_viewer)
        toolbar.addAction(self._act_trace)
        self._act_diag = QAction("Diagnostic Console", self)
        self._act_diag.setToolTip(
            "UDS over ISO-TP (e.g. 0x22 Read DID / 0x2E Write DID) on the open CAN bus."
        )
        self._act_diag.triggered.connect(self._open_diag_console)
        toolbar.addAction(self._act_diag)
        self._act_help = QAction("Help", self)
        self._act_help.setToolTip("Features and how to use this tool")
        self._act_help.triggered.connect(self._show_help)
        toolbar.addAction(self._act_help)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.connection = ConnectionPanel()
        self.connection.connect_clicked.connect(self._connect)
        self.connection.disconnect_clicked.connect(self._disconnect)
        self.security = SecurityPanel()
        self.security.unlock_clicked.connect(self._unlock_now)
        self.security.set_cal_page_clicked.connect(self._apply_cal_page)
        self.security.get_cal_page_clicked.connect(self._query_cal_page)
        self.connection.advanced_extra.addWidget(self.security)
        adv_open = self._settings.value("ui_advanced_open", False, type=bool)
        self.connection.advanced.set_expanded(bool(adv_open))
        self.connection.advanced.toggled.connect(
            lambda open_: self._settings.setValue("ui_advanced_open", open_)
        )
        self.connection.setTitle("")
        self.connection.setFlat(True)
        # Scope to this widget only — a bare "QGroupBox {…}" stylesheet cascades
        # to Bus/timing / Session / Security and collapses their title margins.
        self.connection.setObjectName("connectionPanel")
        self.connection.setStyleSheet(
            "#connectionPanel {"
            " border: none;"
            " background: transparent;"
            " margin-top: 0px;"
            "}"
        )
        self._fold_connection = CollapsibleSection(
            "Connection",
            expanded=self._settings.value("ui_connection_open", True, type=bool),
            indent=0,
            framed=True,
        )
        self._fold_connection.add_widget(self.connection)
        self._fold_connection.toggled.connect(
            lambda open_: self._settings.setValue("ui_connection_open", open_)
        )
        # Outside the mid|log splitter: expands by a bounded Advanced scroll area
        # only, so Symbols/Watch are never covered.
        root.addWidget(self._fold_connection)

        self._splitter = QSplitter(Qt.Orientation.Vertical)
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

        self._fold_log = self._build_log_panel()
        self._splitter.addWidget(self._mid)
        self._splitter.addWidget(self._fold_log)
        self._configure_splitter(
            self._splitter,
            stretch=(5, 1),
            default_sizes=[620, 160],
            # v2: 2-pane (mid|log); ignore older 3-pane Connection|mid|log state
            settings_key="ui_main_splitter_v2",
        )
        root.addWidget(self._splitter, stretch=1)

        if not self._fold_log.is_expanded():
            self._apply_log_height(False)

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

    def _apply_log_height(self, expanded: bool) -> None:
        sizes = self._splitter.sizes()
        total = sum(sizes) or 780
        if expanded:
            log_h = int(self._settings.value("ui_log_height", 160) or 160)
            log_h = max(80, min(log_h, total - 200))
        else:
            if sizes and sizes[-1] > 48:
                self._settings.setValue("ui_log_height", sizes[-1])
            log_h = max(28, self._fold_log.sizeHint().height())
        self._splitter.setSizes([max(total - log_h, 200), log_h])

    def _build_log_panel(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
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
        fold = CollapsibleSection(
            "Log",
            expanded=self._settings.value("ui_log_open", True, type=bool),
            indent=0,
            framed=True,
        )
        fold.add_widget(body)
        fold.toggled.connect(self._on_log_folded)
        return fold

    def _on_log_folded(self, expanded: bool) -> None:
        self._settings.setValue("ui_log_open", expanded)
        self._apply_log_height(expanded)

    def _install_log_handler(self) -> None:
        handler = GuiLogHandler(lambda e: self._bridge.log_entry.emit(e), level=logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        pkg = logging.getLogger("xcp_pcan")
        pkg.addHandler(handler)
        pkg.setLevel(logging.DEBUG)
        apply_verbose_levels(verbose=self.chk_log_verbose.isChecked())
        self._log_handler = handler

    # --- Log / shutdown ---------------------------------------------------------

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
            "_trace_viewer",
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
