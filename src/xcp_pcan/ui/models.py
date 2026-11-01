"""Qt models for symbol browser and watch list."""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from xcp_pcan.symbols.types import Variable
from xcp_pcan.xcp.poller import PollSample


class SymbolTableModel(QAbstractTableModel):
    HEADERS = ["Name", "Address", "Size", "Type", "Kind", "Write", "Unit", "Source"]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[Variable] = []

    def set_variables(self, variables: list[Variable]) -> None:
        self.beginResetModel()
        self._rows = list(variables)
        self.endResetModel()

    def variable_at(self, row: int) -> Variable | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return section + 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        var = self._rows[index.row()]
        col = index.column()
        if col == 0:
            return var.name
        if col == 1:
            return f"0x{var.address:08X}"
        if col == 2:
            return var.size
        if col == 3:
            return var.datatype.value
        if col == 4:
            return var.kind.value
        if col == 5:
            return "yes" if var.writable else "no"
        if col == 6:
            return var.unit
        if col == 7:
            return var.source
        return None


class _WatchNode:
    def __init__(self) -> None:
        self.parent: _WatchNode | None = None
        self.row = 0
        self.kind = "var"  # "var" | "group"
        self.label = ""
        self.var: Variable | None = None
        self.children: list[_WatchNode] = []


class WatchTableModel(QAbstractItemModel):
    """Watch list. Tagged variables sit under a collapsible group row."""

    HEADERS = ["Name", "Address", "Raw", "Physical", "Unit", "Write", "Updated", "Error"]

    def __init__(self) -> None:
        super().__init__()
        self._vars: list[Variable] = []
        self._tag_by_name: dict[str, str] = {}
        self._samples: dict[str, PollSample] = {}
        self._root = _WatchNode()
        self._var_nodes: dict[str, _WatchNode] = {}

    def variables(self) -> list[Variable]:
        return list(self._vars)

    def add_variable(self, var: Variable, tag: str = "") -> None:
        self.add_variables([(var, tag)])

    def add_variables(self, items: list[tuple[Variable, str]]) -> int:
        from xcp_pcan.symbols.symbol_lists import tag_label

        existing = {v.name for v in self._vars}
        pending: list[tuple[Variable, str]] = []
        retag = False
        for var, tag in items:
            label = tag_label(tag) if tag else ""
            if var.name in existing:
                # Already on the watch (for example restored from a project):
                # still attach the list tag so the row moves under that group.
                if label and self._tag_by_name.get(var.name) != label:
                    self._tag_by_name[var.name] = label
                    retag = True
                continue
            existing.add(var.name)
            pending.append((var, label))
        if not pending and not retag:
            return 0
        self.beginResetModel()
        for var, label in pending:
            self._vars.append(var)
            if label:
                self._tag_by_name[var.name] = label
        self._rebuild()
        self.endResetModel()
        return len(pending)

    def apply_tags(self, items: list[tuple[Variable, str]]) -> None:
        """Attach list labels to variables that are already on this watch."""
        from xcp_pcan.symbols.symbol_lists import tag_label

        have = {v.name for v in self._vars}
        changed = False
        for var, tag in items:
            if var.name not in have:
                continue
            label = tag_label(tag) if tag else ""
            if label and self._tag_by_name.get(var.name) != label:
                self._tag_by_name[var.name] = label
                changed = True
        if not changed:
            return
        self.beginResetModel()
        self._rebuild()
        self.endResetModel()

    def remove_variables(self, variables: list[Variable]) -> None:
        names = {v.name for v in variables}
        if not names:
            return
        self.beginResetModel()
        self._vars = [v for v in self._vars if v.name not in names]
        for name in names:
            self._samples.pop(name, None)
            self._tag_by_name.pop(name, None)
        self._rebuild()
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._vars.clear()
        self._tag_by_name.clear()
        self._samples.clear()
        self._rebuild()
        self.endResetModel()

    def update_sample(self, sample: PollSample) -> None:
        self._samples[sample.name] = sample
        node = self._var_nodes.get(sample.name)
        if node is None:
            return
        parent = self._index_of(node.parent) if node.parent is not self._root else QModelIndex()
        top = self.index(node.row, 2, parent)
        bottom = self.index(node.row, 7, parent)
        self.dataChanged.emit(top, bottom)

    def is_group(self, index: QModelIndex) -> bool:
        node = index.internalPointer() if index.isValid() else None
        return isinstance(node, _WatchNode) and node.kind == "group"

    def variable_at(self, index: QModelIndex) -> Variable | None:
        node = index.internalPointer() if index.isValid() else None
        if not isinstance(node, _WatchNode):
            return None
        if node.kind == "var":
            return node.var
        return None

    def variables_under(self, index: QModelIndex) -> list[Variable]:
        node = index.internalPointer() if index.isValid() else None
        if not isinstance(node, _WatchNode):
            return []
        if node.kind == "var" and node.var is not None:
            return [node.var]
        return [child.var for child in node.children if child.var is not None]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.column() > 0:
            return 0
        node = parent.internalPointer() if parent.isValid() else self._root
        if not isinstance(node, _WatchNode):
            return 0
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.HEADERS)

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:  # noqa: N802
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node = parent.internalPointer() if parent.isValid() else self._root
        if not isinstance(parent_node, _WatchNode) or row >= len(parent_node.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_node.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:  # noqa: N802
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        if not isinstance(node, _WatchNode) or node.parent is None or node.parent is self._root:
            return QModelIndex()
        return self.createIndex(node.parent.row, 0, node.parent)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None
        node = index.internalPointer()
        if not isinstance(node, _WatchNode):
            return None
        if node.kind == "group":
            if role == Qt.ItemDataRole.DisplayRole and index.column() == 0:
                count = len(node.children)
                return f"{node.label}  ({count})"
            if role == Qt.ItemDataRole.FontRole and index.column() == 0:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ItemDataRole.BackgroundRole:
                return QBrush(QColor("#e8eef5"))
            return None
        var = node.var
        if var is None or role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._var_text(var, index.column())

    def _var_text(self, var: Variable, col: int):
        sample = self._samples.get(var.name)
        if col == 0:
            return var.name
        if col == 1:
            return f"0x{var.address:08X}"
        if col == 2:
            if sample is None or not sample.raw:
                return ""
            return sample.raw.hex(" ").upper()
        if col == 3:
            if sample is None or sample.physical is None:
                return ""
            if isinstance(sample.physical, float):
                return f"{sample.physical:.6g}"
            return str(sample.physical)
        if col == 4:
            return var.unit or (sample.unit if sample else "")
        if col == 5:
            return "yes" if var.writable else "no"
        if col == 6:
            if sample is None:
                return ""
            import datetime as _dt

            wall = _dt.datetime.fromtimestamp(sample.timestamp).strftime("%H:%M:%S.%f")[:-3]
            if sample.ecu_timestamp is not None:
                return f"{wall} (ecu={sample.ecu_timestamp})"
            return wall
        if col == 7:
            return sample.error if sample else ""
        return None

    def _rebuild(self) -> None:
        root = _WatchNode()
        root.kind = "group"
        loose: list[Variable] = []
        grouped: dict[str, list[Variable]] = {}
        order: list[str] = []
        for var in self._vars:
            tag = self._tag_by_name.get(var.name, "")
            if not tag:
                loose.append(var)
                continue
            if tag not in grouped:
                order.append(tag)
                grouped[tag] = []
            grouped[tag].append(var)
        for var in loose:
            root.children.append(self._var_node(root, var))
        for tag in order:
            group = _WatchNode()
            group.parent = root
            group.row = len(root.children)
            group.kind = "group"
            group.label = tag
            root.children.append(group)
            for var in grouped[tag]:
                group.children.append(self._var_node(group, var))
        self._root = root
        self._var_nodes = {}

        def walk(node: _WatchNode) -> None:
            if node.kind == "var" and node.var is not None:
                self._var_nodes[node.var.name] = node
            for child in node.children:
                walk(child)

        walk(root)

    def _var_node(self, parent: _WatchNode, var: Variable) -> _WatchNode:
        node = _WatchNode()
        node.parent = parent
        node.row = len(parent.children)
        node.kind = "var"
        node.var = var
        return node

    def _index_of(self, node: _WatchNode | None) -> QModelIndex:
        if node is None or node is self._root:
            return QModelIndex()
        return self.createIndex(node.row, 0, node)
