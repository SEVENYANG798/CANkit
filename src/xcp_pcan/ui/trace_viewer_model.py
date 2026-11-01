"""Table model for offline / live CAN trace frames."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from xcp_pcan.trace_log.types import TraceFrame

_COLUMNS = (
    "#",
    "Time (s)",
    "ID",
    "Dir",
    "DLC",
    "Data",
    "Ch",
    "Name",
    "Flags",
)

ID_COLUMN = 2
DIR_COLUMN = 3


class TraceViewerModel(QAbstractTableModel):
    def __init__(self, parent=None, *, live_maxlen: int = 50_000) -> None:
        super().__init__(parent)
        self._all: list[TraceFrame] = []
        self._rows: list[TraceFrame] = []
        self._dbc_names: dict[int, str] = {}
        self._id_filter: set[int] | None = None
        self._id_mask: int | None = None
        self._text = ""
        self._rx_only: bool | None = None  # True=RX, False=TX, None=all
        self._errors_only = False
        self._live_maxlen = max(1000, live_maxlen)

    def _emit_filter_headers(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, ID_COLUMN, ID_COLUMN)
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, DIR_COLUMN, DIR_COLUMN)

    def set_frames(self, frames: list[TraceFrame]) -> None:
        self.beginResetModel()
        self._all = frames
        self._rows = self._filtered(frames)
        self.endResetModel()
        self._emit_filter_headers()

    def append_live(self, frame: TraceFrame) -> None:
        """Append one live frame (ring-buffered). Re-filters if needed."""
        self.append_live_batch([frame])

    def append_live_batch(self, frames: list[TraceFrame]) -> None:
        """Append many live frames with at most one model reset / insert range."""
        if not frames:
            return
        self._all.extend(frames)
        trimmed = False
        if len(self._all) > self._live_maxlen:
            drop = len(self._all) - self._live_maxlen
            self._all = self._all[drop:]
            trimmed = True
        if trimmed:
            self.beginResetModel()
            self._rows = self._filtered(self._all)
            self.endResetModel()
            return
        accepted = [fr for fr in frames if self._passes(fr)]
        if not accepted:
            return
        first = len(self._rows)
        last = first + len(accepted) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._rows.extend(accepted)
        self.endInsertRows()

    def set_dbc_names(self, names: dict[int, str] | None) -> None:
        self.beginResetModel()
        self._dbc_names = dict(names or {})
        self._rows = self._filtered(self._all)
        self.endResetModel()

    def dbc_names(self) -> dict[int, str]:
        return self._dbc_names

    def display_name(self, fr: TraceFrame) -> str:
        return self._dbc_names.get(fr.arbitration_id) or fr.name

    def id_filter(self) -> set[int] | None:
        return None if self._id_filter is None else set(self._id_filter)

    def set_id_filter(self, ids: set[int] | None, mask: int | None = None) -> int:
        self._id_filter = None if not ids else set(ids)
        self._id_mask = mask
        return self.reapply_filter()

    def dir_filter(self) -> bool | None:
        """True = RX only, False = TX only, None = both."""
        return self._rx_only

    def set_dir_filter(self, rx_only: bool | None) -> int:
        self._rx_only = rx_only
        return self.reapply_filter()

    def errors_only(self) -> bool:
        return self._errors_only

    def set_errors_only(self, enabled: bool) -> int:
        self._errors_only = bool(enabled)
        return self.reapply_filter()

    def set_text_filter(self, text: str) -> int:
        self._text = text
        return self.reapply_filter()

    def apply_filter(
        self,
        *,
        id_values: set[int] | None = None,
        id_mask: int | None = None,
        text: str = "",
        rx_only: bool | None = None,
        errors_only: bool = False,
        update_id_filter: bool = True,
    ) -> int:
        if update_id_filter:
            self._id_filter = None if not id_values else set(id_values)
            self._id_mask = id_mask
        self._text = text
        self._rx_only = rx_only
        self._errors_only = errors_only
        return self.reapply_filter()

    def reapply_filter(self) -> int:
        self.beginResetModel()
        self._rows = self._filtered(self._all)
        self.endResetModel()
        self._emit_filter_headers()
        return len(self._rows)

    def id_counts(self) -> list[tuple[int, int]]:
        counts = Counter(fr.arbitration_id for fr in self._all)
        return sorted(counts.items(), key=lambda x: (-x[1], x[0]))

    def clear(self) -> None:
        self.beginResetModel()
        self._all = []
        self._rows = []
        self.endResetModel()

    def frame_at(self, row: int) -> TraceFrame | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def all_frames(self) -> list[TraceFrame]:
        return self._all

    def _passes(self, fr: TraceFrame) -> bool:
        if self._errors_only and not fr.is_error:
            return False
        if self._rx_only is True and not fr.is_rx:
            return False
        if self._rx_only is False and fr.is_rx:
            return False
        ids = self._id_filter
        if ids:
            if self._id_mask is not None and len(ids) == 1:
                only = next(iter(ids))
                if (fr.arbitration_id & self._id_mask) != (only & self._id_mask):
                    return False
            elif fr.arbitration_id not in ids:
                return False
        needle = self._text.strip().lower()
        if needle:
            dbc = self._dbc_names.get(fr.arbitration_id, "")
            direction = "rx" if fr.is_rx else "tx"
            flags = []
            if fr.is_fd:
                flags.append("fd")
            if fr.is_extended:
                flags.append("xtd")
            if fr.is_error:
                flags.append("err")
            blob = (
                f"{fr.id_hex()} {fr.data_hex()} {fr.name} {dbc} {fr.channel} "
                f"{direction} {' '.join(flags)}"
            ).lower()
            if needle not in blob:
                return False
        return True

    def _filtered(self, frames: list[TraceFrame]) -> list[TraceFrame]:
        return [fr for fr in frames if self._passes(fr)]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if section == ID_COLUMN:
                n = len(self._id_filter) if self._id_filter else 0
                return f"ID▾({n})" if n else "ID▾"
            if section == DIR_COLUMN:
                if self._errors_only and self._rx_only is None:
                    return "Dir▾(ERR)"
                if self._rx_only is True:
                    return "Dir▾(RX)" + ("+E" if self._errors_only else "")
                if self._rx_only is False:
                    return "Dir▾(TX)" + ("+E" if self._errors_only else "")
                return "Dir▾"
            if 0 <= section < len(_COLUMNS):
                return _COLUMNS[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            if section == ID_COLUMN:
                return "Click to multi-select CAN IDs for filtering"
            if section == DIR_COLUMN:
                return "Click to filter RX / TX / errors"
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        fr = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == 0:
                return fr.index + 1
            if col == 1:
                return f"{fr.timestamp:.6f}"
            if col == 2:
                return fr.id_hex()
            if col == 3:
                return "RX" if fr.is_rx else "TX"
            if col == 4:
                return fr.dlc
            if col == 5:
                return fr.data_hex()
            if col == 6:
                return fr.channel
            if col == 7:
                return self.display_name(fr)
            if col == 8:
                flags = []
                if fr.is_fd:
                    flags.append("FD")
                if fr.is_extended:
                    flags.append("XTD")
                if fr.is_error:
                    flags.append("ERR")
                return " ".join(flags)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() in (0, 1, 2, 3, 4):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None
