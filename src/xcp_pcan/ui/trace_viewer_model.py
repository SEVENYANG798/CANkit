"""Table model for offline / live CAN trace frames (timeline + collapsed-by-ID)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from xcp_pcan.can.error_frame import decode_pcan_error_frame, error_frame_flags_suffix
from xcp_pcan.trace_log.types import TraceFrame

# Timeline (expanded) — Dir before Type; Name shown only when DBC loaded.
_COLUMNS_TIMELINE = (
    "#",
    "Time (s)",
    "CAN-ID",
    "Dir",
    "Type",
    "Length",
    "Data",
    "Name",
)

# Collapsed (by ID) — Dir before Type; Name shown only when DBC loaded.
_COLUMNS_COLLAPSED = (
    "#",
    "CAN-ID",
    "Dir",
    "Type",
    "Length",
    "Data",
    "Cycle (ms)",
    "Count",
    "Name",
)

# Column indices used by header click filters (depend on mode).
TIME_COLUMN_TIMELINE = 1
ID_COLUMN_TIMELINE = 2
DIR_COLUMN_TIMELINE = 3
NAME_COLUMN_TIMELINE = 7
ID_COLUMN_COLLAPSED = 1
DIR_COLUMN_COLLAPSED = 2
NAME_COLUMN_COLLAPSED = 8
DATA_COLUMN_TIMELINE = 6
DATA_COLUMN_COLLAPSED = 5

# Back-compat aliases (timeline defaults; dialog uses model helpers).
ID_COLUMN = ID_COLUMN_TIMELINE
DIR_COLUMN = DIR_COLUMN_TIMELINE


@dataclass(slots=True)
class _CollapsedEntry:
    frame: TraceFrame
    count: int
    cycle_ms: float | None
    first_index: int  # stable row order = first-seen order


def _collapse_key(fr: TraceFrame) -> tuple[int, bool, bool]:
    """Group by CAN-ID (+ RX/TX and error) like PCAN receive rows."""
    return (fr.arbitration_id, fr.is_rx, fr.is_error)


class TraceViewerModel(QAbstractTableModel):
    def __init__(self, parent=None, *, live_maxlen: int = 50_000) -> None:
        super().__init__(parent)
        self._all: list[TraceFrame] = []
        self._rows: list[TraceFrame] = []  # timeline filtered rows
        self._collapsed: list[_CollapsedEntry] = []
        self._collapsed_map: dict[tuple[int, bool, bool], int] = {}
        self._by_id = False
        self._dbc_names: dict[int, str] = {}
        self._id_filter: set[int] | None = None
        self._id_mask: int | None = None
        self._text = ""
        self._rx_only: bool | None = None  # True=RX, False=TX, None=all
        self._errors_only = False
        self._live_maxlen = max(1000, live_maxlen)
        self._time_delta = False  # timeline Time column: abs seconds vs Δt ms

    # --- Mode -----------------------------------------------------------------

    def is_collapsed(self) -> bool:
        return self._by_id

    def set_collapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._by_id:
            return
        self.beginResetModel()
        self._by_id = collapsed
        if collapsed:
            self._rebuild_collapsed(self._rows)
        self.endResetModel()
        self._emit_filter_headers()

    def id_column(self) -> int:
        return ID_COLUMN_COLLAPSED if self._by_id else ID_COLUMN_TIMELINE

    def dir_column(self) -> int:
        return DIR_COLUMN_COLLAPSED if self._by_id else DIR_COLUMN_TIMELINE

    def name_column(self) -> int:
        return NAME_COLUMN_COLLAPSED if self._by_id else NAME_COLUMN_TIMELINE

    def data_column(self) -> int:
        return DATA_COLUMN_COLLAPSED if self._by_id else DATA_COLUMN_TIMELINE

    def has_dbc_names(self) -> bool:
        return bool(self._dbc_names)

    def time_column(self) -> int | None:
        return None if self._by_id else TIME_COLUMN_TIMELINE

    def time_delta_mode(self) -> bool:
        return self._time_delta

    def set_time_delta_mode(self, enabled: bool) -> None:
        """Toggle timeline Time column between absolute (s) and Δt (ms)."""
        enabled = bool(enabled)
        if enabled == self._time_delta or self._by_id:
            return
        self._time_delta = enabled
        col = TIME_COLUMN_TIMELINE
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, col, col)
        n = len(self._rows)
        if n > 0:
            self.dataChanged.emit(self.index(0, col), self.index(n - 1, col))

    def toggle_time_delta_mode(self) -> bool:
        self.set_time_delta_mode(not self._time_delta)
        return self._time_delta

    def delta_ms_at(self, row: int) -> float | None:
        """Interval to previous *displayed* timeline row; None for first / collapsed."""
        if self._by_id or row <= 0 or row >= len(self._rows):
            return None
        return (self._rows[row].timestamp - self._rows[row - 1].timestamp) * 1000.0

    def _columns(self) -> tuple[str, ...]:
        return _COLUMNS_COLLAPSED if self._by_id else _COLUMNS_TIMELINE

    def _emit_filter_headers(self) -> None:
        id_c = self.id_column()
        dir_c = self.dir_column()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, id_c, id_c)
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, dir_c, dir_c)
        tc = self.time_column()
        if tc is not None:
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, tc, tc)

    # --- Data load / append ---------------------------------------------------

    def set_frames(self, frames: list[TraceFrame]) -> None:
        self.beginResetModel()
        self._all = frames
        self._rows = self._filtered(frames)
        if self._by_id:
            self._rebuild_collapsed(self._rows)
        self.endResetModel()
        self._emit_filter_headers()

    def append_live(self, frame: TraceFrame) -> None:
        self.append_live_batch([frame])

    def append_live_batch(self, frames: list[TraceFrame]) -> None:
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
            if self._by_id:
                self._rebuild_collapsed(self._rows)
            self.endResetModel()
            return

        accepted = [fr for fr in frames if self._passes(fr)]
        if not accepted:
            return

        if self._by_id:
            self._append_collapsed(accepted)
        else:
            first = len(self._rows)
            last = first + len(accepted) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._rows.extend(accepted)
            self.endInsertRows()

    def _append_collapsed(self, frames: list[TraceFrame]) -> None:
        """Update existing ID rows in place; insert only for new IDs."""
        updated: set[int] = set()
        new_entries: list[_CollapsedEntry] = []
        pending: dict[tuple[int, bool, bool], int] = {}  # key → index in new_entries

        def _bump(entry: _CollapsedEntry, fr: TraceFrame) -> None:
            dt = (fr.timestamp - entry.frame.timestamp) * 1000.0
            entry.cycle_ms = dt if dt >= 0 else None
            entry.frame = fr
            entry.count += 1

        for fr in frames:
            self._rows.append(fr)
            key = _collapse_key(fr)
            idx = self._collapsed_map.get(key)
            if idx is not None:
                _bump(self._collapsed[idx], fr)
                updated.add(idx)
                continue
            pend = pending.get(key)
            if pend is not None:
                _bump(new_entries[pend], fr)
                continue
            pending[key] = len(new_entries)
            new_entries.append(
                _CollapsedEntry(
                    frame=fr,
                    count=1,
                    cycle_ms=None,
                    first_index=fr.index,
                )
            )

        if new_entries:
            base = len(self._collapsed)
            for i, entry in enumerate(new_entries):
                self._collapsed_map[_collapse_key(entry.frame)] = base + i
            first = base
            last = base + len(new_entries) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._collapsed.extend(new_entries)
            self.endInsertRows()

        if updated:
            cols = self.columnCount() - 1
            for row in sorted(updated):
                self.dataChanged.emit(self.index(row, 0), self.index(row, cols))

    def _rebuild_collapsed(self, frames: list[TraceFrame]) -> None:
        self._collapsed = []
        self._collapsed_map = {}
        for fr in frames:
            key = _collapse_key(fr)
            idx = self._collapsed_map.get(key)
            if idx is None:
                self._collapsed_map[key] = len(self._collapsed)
                self._collapsed.append(
                    _CollapsedEntry(
                        frame=fr,
                        count=1,
                        cycle_ms=None,
                        first_index=fr.index,
                    )
                )
            else:
                entry = self._collapsed[idx]
                dt = (fr.timestamp - entry.frame.timestamp) * 1000.0
                entry.cycle_ms = dt if dt >= 0 else None
                entry.frame = fr
                entry.count += 1

    # --- DBC / filters --------------------------------------------------------

    def set_dbc_names(self, names: dict[int, str] | None) -> None:
        self.beginResetModel()
        self._dbc_names = dict(names or {})
        self._rows = self._filtered(self._all)
        if self._by_id:
            self._rebuild_collapsed(self._rows)
        self.endResetModel()

    def dbc_names(self) -> dict[int, str]:
        return self._dbc_names

    def longest_appeared_dbc_name(self) -> str:
        """Longest DBC name among IDs already in the buffer (not the whole DBC)."""
        if not self._dbc_names or not self._all:
            return ""
        longest = ""
        seen: set[int] = set()
        for fr in self._all:
            arb = fr.arbitration_id
            if arb in seen:
                continue
            seen.add(arb)
            name = self._dbc_names.get(arb) or ""
            if name and len(name) > len(longest):
                longest = name
        return longest

    def display_name(self, fr: TraceFrame) -> str:
        if fr.is_error:
            decoded = decode_pcan_error_frame(fr.arbitration_id, fr.data)
            if decoded:
                return decoded
        return self._dbc_names.get(fr.arbitration_id) or fr.name

    def id_filter(self) -> set[int] | None:
        return None if self._id_filter is None else set(self._id_filter)

    def set_id_filter(self, ids: set[int] | None, mask: int | None = None) -> int:
        self._id_filter = None if not ids else set(ids)
        self._id_mask = mask
        return self.reapply_filter()

    def dir_filter(self) -> bool | None:
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
        if self._by_id:
            self._rebuild_collapsed(self._rows)
        self.endResetModel()
        self._emit_filter_headers()
        return self.rowCount()

    def id_counts(self) -> list[tuple[int, int]]:
        counts = Counter(fr.arbitration_id for fr in self._all)
        return sorted(counts.items(), key=lambda x: (-x[1], x[0]))

    def clear(self) -> None:
        self.beginResetModel()
        self._all = []
        self._rows = []
        self._collapsed = []
        self._collapsed_map = {}
        self.endResetModel()

    def frame_at(self, row: int) -> TraceFrame | None:
        if self._by_id:
            if 0 <= row < len(self._collapsed):
                return self._collapsed[row].frame
            return None
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
            flags = [fr.type_label().lower()]
            blob = (
                f"{fr.id_hex()} {fr.data_hex()} {fr.name} {dbc} {fr.channel} "
                f"{direction} {' '.join(flags)}"
            ).lower()
            if needle not in blob:
                return False
        return True

    def _filtered(self, frames: list[TraceFrame]) -> list[TraceFrame]:
        # Fast path: no active filters → keep the same list object (large BLFs).
        if (
            not self._errors_only
            and self._rx_only is None
            and not self._id_filter
            and not self._text.strip()
        ):
            return frames
        return [fr for fr in frames if self._passes(fr)]

    def _type_display(self, fr: TraceFrame) -> str:
        label = fr.type_label()
        if fr.is_error:
            suffix = error_frame_flags_suffix(fr.arbitration_id, fr.data)
            if suffix:
                return f"{label} {suffix}"
        return label

    # --- Qt model -------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        if self._by_id:
            return len(self._collapsed)
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._columns())

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        cols = self._columns()
        id_c = self.id_column()
        dir_c = self.dir_column()
        time_c = self.time_column()
        if role == Qt.ItemDataRole.DisplayRole:
            if section == id_c:
                n = len(self._id_filter) if self._id_filter else 0
                return f"CAN-ID▾({n})" if n else "CAN-ID▾"
            if section == dir_c:
                if self._errors_only and self._rx_only is None:
                    return "Dir▾(ERR)"
                if self._rx_only is True:
                    return "Dir▾(RX)" + ("+E" if self._errors_only else "")
                if self._rx_only is False:
                    return "Dir▾(TX)" + ("+E" if self._errors_only else "")
                return "Dir▾"
            if time_c is not None and section == time_c:
                return "Δt (ms)" if self._time_delta else "Time (s)"
            if 0 <= section < len(cols):
                return cols[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            if section == id_c:
                return "Click to multi-select CAN IDs for filtering"
            if section == dir_c:
                return "Click to filter RX / TX / errors"
            if time_c is not None and section == time_c:
                return (
                    "Click to switch to absolute Time (s)"
                    if self._time_delta
                    else "Click to switch to Δt (ms) vs previous frame"
                )
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if self._by_id:
            if not (0 <= row < len(self._collapsed)):
                return None
            entry = self._collapsed[row]
            fr = entry.frame
            if role == Qt.ItemDataRole.DisplayRole:
                # #, CAN-ID, Dir, Type, Length, Data, Cycle, Count, Name
                if col == 0:
                    return row + 1
                if col == 1:
                    return fr.id_hex()
                if col == 2:
                    return "RX" if fr.is_rx else "TX"
                if col == 3:
                    return self._type_display(fr)
                if col == 4:
                    return fr.dlc
                if col == 5:
                    return fr.data_hex()
                if col == 6:
                    return "" if entry.cycle_ms is None else f"{entry.cycle_ms:.1f}"
                if col == 7:
                    return entry.count
                if col == 8:
                    return self.display_name(fr)
            if role == Qt.ItemDataRole.TextAlignmentRole:
                # Data + Name stay left; everything else centered.
                if col in (DATA_COLUMN_COLLAPSED, NAME_COLUMN_COLLAPSED):
                    return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            return None

        if not (0 <= row < len(self._rows)):
            return None
        fr = self._rows[row]
        if role == Qt.ItemDataRole.DisplayRole:
            # #, Time/Δt, CAN-ID, Dir, Type, Length, Data, Name
            if col == 0:
                return fr.index + 1
            if col == 1:
                if self._time_delta:
                    dt = self.delta_ms_at(row)
                    return "" if dt is None else f"{dt:.3f}"
                return f"{fr.timestamp:.6f}"
            if col == 2:
                return fr.id_hex()
            if col == 3:
                return "RX" if fr.is_rx else "TX"
            if col == 4:
                return self._type_display(fr)
            if col == 5:
                return fr.dlc
            if col == 6:
                return fr.data_hex()
            if col == 7:
                return self.display_name(fr)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            # Data + Name stay left; everything else centered.
            if col in (DATA_COLUMN_TIMELINE, NAME_COLUMN_TIMELINE):
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        return None
