"""Symbol browser panel (filter + table)."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSettings, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.symbols.types import Variable
from xcp_pcan.ui.column_layout import PersistedColumnWidths
from xcp_pcan.ui.models import SymbolTableModel
from xcp_pcan.ui.window_util import bind_copy_shortcut, copy_selected_rows


class SymbolPanel(QGroupBox):
    add_selected = Signal()
    add_all_filtered = Signal()
    load_lists = Signal()
    clear_filter = Signal()
    filter_changed = Signal(str)

    def __init__(
        self,
        model: SymbolTableModel,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Symbols", parent)
        self._model = model
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        filter_row = QHBoxLayout()
        self.btn_files = QToolButton()
        self.btn_files.setText("A2L / ELF")
        self.btn_files.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_files.setToolTip("Load / unload / resolve / update / create A2L and ELF for XCP")
        self.btn_files.setEnabled(False)
        filter_row.addWidget(self.btn_files)
        filter_row.addWidget(QLabel("Filter"))
        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("Name, 0xFE, fe04…")
        self.edt_search.setToolTip(
            "Filter by name substring, or fuzzy address (e.g. 0xFE / fe04). "
            "Paste several names for exact match."
        )
        self.edt_search.setClearButtonEnabled(True)
        self.edt_search.textChanged.connect(self.filter_changed.emit)
        filter_row.addWidget(self.edt_search, stretch=1)
        btn_lists = QPushButton("Load lists")
        btn_lists.setToolTip(
            "Load a folder of .txt symbol lists into the watch view, grouped by file name."
        )
        btn_lists.clicked.connect(self.load_lists.emit)
        filter_row.addWidget(btn_lists)
        layout.addLayout(filter_row)

        self.lbl_filter_status = QLabel("")
        self.lbl_filter_status.setStyleSheet("color: gray;")
        self.lbl_filter_status.setWordWrap(True)
        layout.addWidget(self.lbl_filter_status)

        self.tbl_symbols = QTableView()
        self.tbl_symbols.setModel(model)
        self.tbl_symbols.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.tbl_symbols.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.tbl_symbols.verticalHeader().setDefaultSectionSize(22)
        self.tbl_symbols.verticalHeader().setVisible(False)
        self.tbl_symbols.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl_symbols.customContextMenuRequested.connect(self._show_symbol_context_menu)
        bind_copy_shortcut(self.tbl_symbols)
        PersistedColumnWidths(
            settings,
            "xcp_symbols/column_widths",
            (240, 100, 48, 100, 64, 48, 48, 72),
            stretch_cols=(7,),  # Source
            parent=self,
        ).attach(self.tbl_symbols)
        layout.addWidget(self.tbl_symbols, stretch=1)

    def set_file_actions(self, actions: list[QAction]) -> None:
        """Attach Load A2L/ELF / Resolve / Update / Create under A2L / ELF."""
        menu = QMenu(self)
        for act in actions:
            menu.addAction(act)
        self.btn_files.setMenu(menu)
        self.btn_files.setEnabled(bool(actions))

    def filter_text(self) -> str:
        return self.edt_search.text()

    def set_filter_status(self, text: str, *, error: bool = False) -> None:
        self.lbl_filter_status.setText(text)
        self.lbl_filter_status.setStyleSheet("color: red;" if error else "color: gray;")

    def selected_rows(self) -> list[int]:
        return [i.row() for i in self.tbl_symbols.selectionModel().selectedRows()]

    def selected_variables(self) -> list[Variable]:
        out: list[Variable] = []
        for row in self.selected_rows():
            var = self._model.variable_at(row)
            if var is not None:
                out.append(var)
        return out

    def visible_variables(self) -> list[Variable]:
        out: list[Variable] = []
        for row in range(self._model.rowCount()):
            var = self._model.variable_at(row)
            if var is not None:
                out.append(var)
        return out

    def _on_clear_filter(self) -> None:
        self.edt_search.clear()
        self.clear_filter.emit()

    def _show_symbol_context_menu(self, pos: QPoint) -> None:
        """Right-click: select row under cursor, then Add / Clear filter."""
        view = self.tbl_symbols
        index = view.indexAt(pos)
        if index.isValid():
            sm = view.selectionModel()
            if sm is not None and not sm.isSelected(index):
                sm.select(
                    index,
                    sm.SelectionFlag.ClearAndSelect | sm.SelectionFlag.Rows,
                )
        has_sel = bool(self.selected_rows())
        menu = QMenu(view)
        act_copy = menu.addAction("Copy")
        act_copy.setEnabled(has_sel)
        menu.addSeparator()
        act_add = menu.addAction("Add selected")
        act_add.setEnabled(has_sel)
        act_add_all = menu.addAction("Add all filtered")
        act_add_all.setEnabled(self._model.rowCount() > 0)
        act_clear = menu.addAction("Clear filter")
        chosen = menu.exec(view.viewport().mapToGlobal(pos))
        if chosen is act_copy:
            copy_selected_rows(view)
        elif chosen is act_add:
            self.add_selected.emit()
        elif chosen is act_add_all:
            self.add_all_filtered.emit()
        elif chosen is act_clear:
            self._on_clear_filter()
