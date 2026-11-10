"""Symbol browser panel (filter + table)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.symbols.types import Variable
from xcp_pcan.ui.models import SymbolTableModel


class SymbolPanel(QGroupBox):
    add_selected = Signal()
    add_all_filtered = Signal()
    load_lists = Signal()
    clear_filter = Signal()
    filter_changed = Signal(str)

    def __init__(
        self,
        model: SymbolTableModel,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Symbols", parent)
        self._model = model
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter"))
        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("Symbol1, Symbol2, Symbol3")
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
        self.tbl_symbols.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.tbl_symbols, stretch=1)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add selected")
        btn_add.clicked.connect(self.add_selected.emit)
        btn_add_all = QPushButton("Add all filtered")
        btn_add_all.clicked.connect(self.add_all_filtered.emit)
        btn_clear = QPushButton("Clear filter")
        btn_clear.clicked.connect(self._on_clear_filter)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_add_all)
        btn_row.addWidget(btn_clear)
        layout.addLayout(btn_row)

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
