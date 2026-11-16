"""QSettings-backed Interactive column widths (avoid ResizeToContents stutter)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QObject, QSettings
from PySide6.QtWidgets import QHeaderView, QTableView, QTableWidget, QTreeView

View = QTableView | QTableWidget | QTreeView


def _coerce_width_list(raw: object, n: int) -> list[int] | None:
    """Normalize QSettings list/QStringList/comma string into ``n`` ints."""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        values: list[object] = parts
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        try:
            values = list(raw)  # type: ignore[arg-type]
        except TypeError:
            return None
    if len(values) != n:
        return None
    out: list[int] = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            return None
    return out


def load_column_widths(
    settings: QSettings | None,
    key: str,
    defaults: Sequence[int],
    *,
    min_width: int = 36,
) -> list[int]:
    defaults_list = [max(min_width, int(w)) for w in defaults]
    if settings is None:
        return list(defaults_list)
    parsed = _coerce_width_list(settings.value(key), len(defaults_list))
    if parsed is None:
        return list(defaults_list)
    return [max(min_width, w) for w in parsed]


def save_column_widths(
    settings: QSettings | None, key: str, widths: Sequence[int]
) -> None:
    if settings is None:
        return
    # Store as QStringList-friendly ints; sync so a crash still keeps last drag.
    settings.setValue(key, [int(w) for w in widths])
    settings.sync()


def _header(view: View) -> QHeaderView:
    if isinstance(view, QTreeView):
        return view.header()
    return view.horizontalHeader()


def column_count(view: View) -> int:
    if isinstance(view, QTableWidget):
        return view.columnCount()
    model = view.model()
    return 0 if model is None else int(model.columnCount())


def apply_column_widths(
    view: View,
    widths: Sequence[int],
    *,
    stretch_cols: Sequence[int] = (),
    min_width: int = 36,
) -> None:
    """Apply widths. Prefer Interactive for persisted cols — Stretch fights save/restore."""
    hdr = _header(view)
    hdr.setStretchLastSection(False)
    hdr.setMinimumSectionSize(min_width)
    stretch = set(stretch_cols)
    n = column_count(view)
    # Block sectionResized while applying so Stretch/layout churn cannot
    # overwrite the just-loaded (or user) widths in QSettings.
    blocked = hdr.blockSignals(True)
    try:
        for col in range(n):
            if col in stretch:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
                continue
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            if col < len(widths):
                view.setColumnWidth(col, max(min_width, int(widths[col])))
    finally:
        hdr.blockSignals(blocked)


class PersistedColumnWidths(QObject):
    """One saved width list shared by one or more table/tree views."""

    def __init__(
        self,
        settings: QSettings | None,
        key: str,
        defaults: Sequence[int],
        *,
        stretch_cols: Sequence[int] = (),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._key = key
        self._defaults = list(defaults)
        self._stretch_cols = tuple(stretch_cols)
        self._views: list[View] = []
        self._applying = False

    def attach(self, view: View) -> None:
        self._views.append(view)
        _header(view).sectionResized.connect(
            lambda *_args, v=view: self._on_resized(v)
        )
        self.apply(view)

    def reload(self) -> None:
        for view in self._views:
            self.apply(view)

    def apply(self, view: View) -> None:
        widths = load_column_widths(self._settings, self._key, self._defaults)
        self._applying = True
        try:
            apply_column_widths(
                view, widths, stretch_cols=self._stretch_cols
            )
        finally:
            self._applying = False

    def _on_resized(self, view: View) -> None:
        if self._applying:
            return
        widths = [view.columnWidth(col) for col in range(column_count(view))]
        save_column_widths(self._settings, self._key, widths)
        self._applying = True
        try:
            for other in self._views:
                if other is view:
                    continue
                apply_column_widths(
                    other, widths, stretch_cols=self._stretch_cols
                )
        finally:
            self._applying = False
