"""Polling / DAQ watch tabs."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QSettings, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.project import DaqSettings, PollingSettings
from xcp_pcan.ui.column_layout import PersistedColumnWidths
from xcp_pcan.ui.models import WatchTableModel
from xcp_pcan.ui.window_util import bind_copy_shortcut, copy_selected_rows

# Name Address Raw Physical Unit Write Updated Error — user-tunable; saved in QSettings.
_WATCH_COL_WIDTHS_KEY = "xcp_watch/column_widths"
_DEFAULT_WATCH_COL_WIDTHS = (280, 100, 72, 80, 48, 48, 90, 72)


class WatchTabs(QTabWidget):
    """Right-hand Polling + DAQ watch lists."""

    tab_changed = Signal(int)
    poll_start = Signal()
    poll_stop = Signal()
    poll_write = Signal()
    poll_curves = Signal()
    poll_remove = Signal()
    poll_clear = Signal()
    daq_start = Signal()
    daq_stop = Signal()
    daq_write = Signal()
    daq_curves = Signal()
    daq_remove = Signal()
    daq_clear = Signal()

    def __init__(
        self,
        poll_model: WatchTableModel,
        daq_model: WatchTableModel,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._poll_model = poll_model
        self._daq_model = daq_model
        self._fold_acts: dict[QTreeView, QAction] = {}
        self._folded: dict[QTreeView, bool] = {}
        # While Poll/DAQ is running, that view's collapsed groups stay locked
        # (cannot expand mid-run — acquire set is fixed at Start).
        self._expand_locked: dict[QTreeView, bool] = {}
        self._col_widths = PersistedColumnWidths(
            settings,
            _WATCH_COL_WIDTHS_KEY,
            _DEFAULT_WATCH_COL_WIDTHS,
            parent=self,
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.addTab(self._build_daq_tab(), "DAQ")
        self.addTab(self._build_poll_tab(), "Polling")
        self.currentChanged.connect(self.tab_changed.emit)
        self._col_widths.attach(self.tbl_daq)
        self._col_widths.attach(self.tbl_poll)

    def _build_poll_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        ctrl = QHBoxLayout()
        self.spn_poll = QSpinBox()
        self.spn_poll.setRange(10, 10_000)
        self.spn_poll.setValue(100)
        self.spn_poll.setSuffix(" ms")
        ctrl.addWidget(QLabel("Interval"))
        ctrl.addWidget(self.spn_poll)
        self.btn_poll_start = QPushButton("Start poll")
        self.btn_poll_start.clicked.connect(self.poll_start.emit)
        self.btn_poll_stop = QPushButton("Stop poll")
        self.btn_poll_stop.clicked.connect(self.poll_stop.emit)
        self.btn_write = QPushButton("Write value")
        self.btn_write.clicked.connect(self.poll_write.emit)
        ctrl.addWidget(self.btn_poll_start)
        ctrl.addWidget(self.btn_poll_stop)
        ctrl.addWidget(self.btn_write)

        self.tbl_poll = self._make_watch_view(self._poll_model)
        self.act_poll_fold = self._make_fold_action(self.tbl_poll)
        ctrl.addStretch()
        layout.addLayout(ctrl)
        layout.addWidget(self.tbl_poll, stretch=1)
        return page

    def _build_daq_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        ctrl = QHBoxLayout()
        self.spn_event = QSpinBox()
        self.spn_event.setRange(0, 65535)
        self.spn_event.setValue(0)
        ctrl.addWidget(QLabel("Event"))
        ctrl.addWidget(self.spn_event)

        self.spn_prescaler = QSpinBox()
        self.spn_prescaler.setRange(1, 255)
        self.spn_prescaler.setValue(1)
        ctrl.addWidget(QLabel("Prescaler"))
        ctrl.addWidget(self.spn_prescaler)

        self.chk_daq_timestamp = QCheckBox("DTO timestamp")
        self.chk_daq_timestamp.setToolTip(
            "Request SET_DAQ_LIST_MODE TIMESTAMP and parse ECU ticks from each DTO."
        )
        ctrl.addWidget(self.chk_daq_timestamp)

        self.btn_daq_start = QPushButton("Start DAQ")
        self.btn_daq_start.clicked.connect(self.daq_start.emit)
        self.btn_daq_stop = QPushButton("Stop DAQ")
        self.btn_daq_stop.clicked.connect(self.daq_stop.emit)
        self.btn_daq_write = QPushButton("Write value")
        self.btn_daq_write.clicked.connect(self.daq_write.emit)
        ctrl.addWidget(self.btn_daq_start)
        ctrl.addWidget(self.btn_daq_stop)
        ctrl.addWidget(self.btn_daq_write)

        self.tbl_daq = self._make_watch_view(self._daq_model)
        self.act_daq_fold = self._make_fold_action(self.tbl_daq)
        ctrl.addStretch()
        layout.addLayout(ctrl)
        layout.addWidget(self.tbl_daq, stretch=1)
        return page

    def set_connected(
        self,
        connected: bool,
        *,
        poll_running: bool,
        daq_running: bool,
    ) -> None:
        self.btn_poll_start.setEnabled(connected and not poll_running and not daq_running)
        self.btn_poll_stop.setEnabled(poll_running)
        # Writes need a free CTO path — disable while DAQ owns the bus.
        self.btn_write.setEnabled(connected and not daq_running)
        self.btn_daq_start.setEnabled(connected and not daq_running and not poll_running)
        self.btn_daq_stop.setEnabled(daq_running)
        self.btn_daq_write.setEnabled(connected and not daq_running)
        self._set_expand_locked(self.tbl_poll, poll_running)
        self._set_expand_locked(self.tbl_daq, daq_running)

    def _make_watch_view(self, model) -> QTreeView:
        view = QTreeView()
        view.setModel(model)
        view.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        view.setRootIsDecorated(True)
        view.setItemsExpandable(True)
        view.setUniformRowHeights(True)
        view.setIndentation(16)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(
            lambda pos, v=view: self._show_watch_context_menu(v, pos)
        )
        bind_copy_shortcut(view)
        model.modelReset.connect(lambda v=view, m=model: self._after_watch_reset(v, m))
        view.expanded.connect(lambda index, v=view: self._on_view_expanded(v, index))
        self._expand_locked[view] = False
        return view

    def _show_watch_context_menu(self, view: QTreeView, pos: QPoint) -> None:
        """Right-click: Copy / Remove / Clear / Expand|Fold all."""
        index = view.indexAt(pos)
        if index.isValid():
            # Click outside current selection → select that row/group first.
            sm = view.selectionModel()
            if sm is not None and not sm.isSelected(index):
                sm.select(
                    index,
                    sm.SelectionFlag.ClearAndSelect | sm.SelectionFlag.Rows,
                )
        is_daq = view is self.tbl_daq
        has_sel = index.isValid() or bool(view.selectionModel().selectedRows())
        fold_act = self._fold_acts.get(view)
        menu = QMenu(view)
        act_copy = menu.addAction("Copy")
        act_copy.setEnabled(has_sel)
        act_graphic = menu.addAction("Graphic")
        act_graphic.setToolTip(
            "Add selected signal(s) / list group(s) to the shared Graphic window. "
            "No curve buffer until Graphic is open."
        )
        act_graphic.setEnabled(has_sel)
        menu.addSeparator()
        act_remove = menu.addAction("Remove")
        act_remove.setToolTip(
            "Remove the selected list group(s) or signal(s) from this watch."
        )
        act_remove.setEnabled(has_sel)
        act_clear = menu.addAction("Clear")
        act_clear.setToolTip("Remove every row from this watch list.")
        if fold_act is not None:
            menu.addSeparator()
            menu.addAction(fold_act)
        chosen = menu.exec(view.viewport().mapToGlobal(pos))
        if chosen is act_copy:
            copy_selected_rows(view)
        elif chosen is act_graphic:
            (self.daq_curves if is_daq else self.poll_curves).emit()
        elif chosen is act_remove:
            (self.daq_remove if is_daq else self.poll_remove).emit()
        elif chosen is act_clear:
            (self.daq_clear if is_daq else self.poll_clear).emit()
        # Expand/Fold all: QMenu already fires the shared QAction.

    def _set_expand_locked(self, view: QTreeView, locked: bool) -> None:
        """While observing, keep collapsed list groups from being opened."""
        self._expand_locked[view] = bool(locked)
        view.setItemsExpandable(not locked)
        view.setExpandsOnDoubleClick(not locked)
        act = self._fold_acts.get(view)
        if act is not None:
            act.setEnabled(not locked)
            if locked:
                act.setToolTip(
                    "Fold/expand is locked while measuring — Stop first, then "
                    "expand the groups you want and Start again."
                )
            else:
                act.setToolTip(
                    "Collapse every list group. Collapsed groups are not acquired "
                    "on Start (no XCP bus use) until you expand them and Start again."
                )

    def _on_view_expanded(self, view: QTreeView, index: QModelIndex) -> None:
        if not self._expand_locked.get(view, False):
            return
        # Belt-and-suspenders: block expand of collapsed tags mid-run.
        view.collapse(index)

    def _make_fold_action(self, view: QTreeView) -> QAction:
        act = QAction("Fold all", self)
        act.setToolTip(
            "Collapse every list group. Collapsed groups are not acquired on Start "
            "(no XCP bus use) until you expand them and Start again."
        )
        act.triggered.connect(lambda _checked=False, v=view: self._toggle_fold(v))
        self._fold_acts[view] = act
        self._folded[view] = False
        return act

    def _toggle_fold(self, view: QTreeView) -> None:
        if self._expand_locked.get(view, False):
            return
        self._folded[view] = not self._folded.get(view, False)
        self._apply_fold(view)

    def _apply_fold(self, view: QTreeView) -> None:
        act = self._fold_acts.get(view)
        view.setUpdatesEnabled(False)
        try:
            if self._folded.get(view, False):
                view.collapseAll()
                if act is not None:
                    act.setText("Expand all")
            else:
                if self._expand_locked.get(view, False):
                    return
                view.expandAll()
                if act is not None:
                    act.setText("Fold all")
        finally:
            view.setUpdatesEnabled(True)

    def _after_watch_reset(self, view: QTreeView, model) -> None:
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            view.setFirstColumnSpanned(row, QModelIndex(), model.is_group(index))
        # Fresh lists default to folded (acquire only after user expands).
        if not self._expand_locked.get(view, False):
            self._apply_fold(view)

    def set_folded(self, view: QTreeView, folded: bool) -> None:
        """Collapse (True) or expand (False) all list groups in ``view``."""
        if not folded and self._expand_locked.get(view, False):
            return
        self._folded[view] = bool(folded)
        self._apply_fold(view)

    def fold_active_watch(self, folded: bool = True) -> None:
        view = self.tbl_daq if self.active_is_daq() else self.tbl_poll
        self.set_folded(view, folded)

    def acquire_variables(self, *, daq: bool | None = None) -> list:
        """Variables that will be polled/DAQ'd: expanded groups + untagged rows.

        Collapsed list groups stay on the watch for organization only and do not
        consume XCP bus bandwidth until expanded and Start is pressed again.
        """
        use_daq = self.active_is_daq() if daq is None else bool(daq)
        model = self._daq_model if use_daq else self._poll_model
        view = self.tbl_daq if use_daq else self.tbl_poll
        out = []
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if model.is_group(index):
                if view.isExpanded(index):
                    out.extend(model.variables_under(index))
                continue
            var = model.variable_at(index)
            if var is not None:
                out.append(var)
        return out

    def active_is_daq(self) -> bool:
        return self.tabText(self.currentIndex()) == "DAQ"

    def poll_selected_rows(self) -> list[int]:
        return [i.row() for i in self.tbl_poll.selectionModel().selectedRows()]

    def daq_selected_rows(self) -> list[int]:
        return [i.row() for i in self.tbl_daq.selectionModel().selectedRows()]

    def collect_polling(self, watch_names: list[str]) -> PollingSettings:
        return PollingSettings(interval_ms=self.spn_poll.value(), watch=watch_names)

    def collect_daq(self, watch_names: list[str]) -> DaqSettings:
        return DaqSettings(
            event_channel=self.spn_event.value(),
            prescaler=self.spn_prescaler.value(),
            timestamp=self.chk_daq_timestamp.isChecked(),
            watch=watch_names,
        )

    def apply_polling(self, poll: PollingSettings) -> None:
        self.spn_poll.setValue(poll.interval_ms)

    def apply_daq(self, daq: DaqSettings) -> None:
        self.spn_event.setValue(daq.event_channel)
        self.spn_prescaler.setValue(daq.prescaler)
        self.chk_daq_timestamp.setChecked(daq.timestamp)
