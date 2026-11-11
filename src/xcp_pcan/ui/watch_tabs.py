"""Polling / DAQ watch tabs."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Signal
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
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.project import DaqSettings, PollingSettings
from xcp_pcan.ui.models import WatchTableModel


class WatchTabs(QTabWidget):
    """Right-hand Polling + DAQ watch lists."""

    tab_changed = Signal(int)
    poll_start = Signal()
    poll_stop = Signal()
    poll_write = Signal()
    poll_curves = Signal()
    poll_export = Signal()
    poll_remove = Signal()
    poll_clear = Signal()
    daq_start = Signal()
    daq_stop = Signal()
    daq_write = Signal()
    daq_curves = Signal()
    daq_export = Signal()
    daq_remove = Signal()
    daq_clear = Signal()

    def __init__(
        self,
        poll_model: WatchTableModel,
        daq_model: WatchTableModel,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._poll_model = poll_model
        self._daq_model = daq_model
        self._fold_acts: dict[QTreeView, QAction] = {}
        self._folded: dict[QTreeView, bool] = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.addTab(self._build_daq_tab(), "DAQ")
        self.addTab(self._build_poll_tab(), "Polling")
        self.currentChanged.connect(self.tab_changed.emit)

    @staticmethod
    def _actions_button(menu: QMenu) -> QToolButton:
        btn = QToolButton()
        btn.setText("Actions")
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn.setMenu(menu)
        btn.setToolTip("Graphic, export, remove, clear, fold")
        return btn

    def _build_poll_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Primary strip: timing + start/stop + write. Secondary → Actions menu.
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
        act_curves = QAction("Graphic", self)
        act_curves.triggered.connect(self.poll_curves.emit)
        act_export = QAction("Export CSV", self)
        act_export.setToolTip("Export buffered polling samples to a CSV file.")
        act_export.triggered.connect(self.poll_export.emit)
        act_remove = QAction("Remove", self)
        act_remove.triggered.connect(self.poll_remove.emit)
        act_clear = QAction("Clear", self)
        act_clear.triggered.connect(self.poll_clear.emit)
        self.act_poll_fold = self._make_fold_action(self.tbl_poll)
        menu = QMenu(self)
        menu.addAction(act_curves)
        menu.addAction(act_export)
        menu.addSeparator()
        menu.addAction(act_remove)
        menu.addAction(act_clear)
        menu.addSeparator()
        menu.addAction(self.act_poll_fold)
        ctrl.addWidget(self._actions_button(menu))
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
        act_curves = QAction("Graphic", self)
        act_curves.triggered.connect(self.daq_curves.emit)
        act_export = QAction("Export CSV", self)
        act_export.setToolTip("Export buffered DAQ samples to a CSV file.")
        act_export.triggered.connect(self.daq_export.emit)
        act_remove = QAction("Remove", self)
        act_remove.triggered.connect(self.daq_remove.emit)
        act_clear = QAction("Clear", self)
        act_clear.triggered.connect(self.daq_clear.emit)
        self.act_daq_fold = self._make_fold_action(self.tbl_daq)
        menu = QMenu(self)
        menu.addAction(act_curves)
        menu.addAction(act_export)
        menu.addSeparator()
        menu.addAction(act_remove)
        menu.addAction(act_clear)
        menu.addSeparator()
        menu.addAction(self.act_daq_fold)
        ctrl.addWidget(self._actions_button(menu))
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

    def _make_watch_view(self, model) -> QTreeView:
        view = QTreeView()
        view.setModel(model)
        view.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        view.setRootIsDecorated(True)
        view.setItemsExpandable(True)
        view.setUniformRowHeights(True)
        view.setIndentation(16)
        view.header().setStretchLastSection(True)
        view.header().resizeSection(0, 280)
        model.modelReset.connect(lambda v=view, m=model: self._after_watch_reset(v, m))
        return view

    def _make_fold_action(self, view: QTreeView) -> QAction:
        act = QAction("Fold all", self)
        act.setToolTip("Collapse every tag. Click again to expand them.")
        act.triggered.connect(lambda _checked=False, v=view: self._toggle_fold(v))
        self._fold_acts[view] = act
        self._folded[view] = False
        return act

    def _toggle_fold(self, view: QTreeView) -> None:
        self._folded[view] = not self._folded.get(view, False)
        self._apply_fold(view)

    def _apply_fold(self, view: QTreeView) -> None:
        act = self._fold_acts.get(view)
        if self._folded.get(view, False):
            view.collapseAll()
            if act is not None:
                act.setText("Expand all")
        else:
            view.expandAll()
            if act is not None:
                act.setText("Fold all")

    def _after_watch_reset(self, view: QTreeView, model) -> None:
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            view.setFirstColumnSpanned(row, QModelIndex(), model.is_group(index))
        self._apply_fold(view)

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
