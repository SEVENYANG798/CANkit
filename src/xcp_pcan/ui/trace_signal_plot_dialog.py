"""Offline DBC signal table + curve for Trace Viewer (dropdown multi-select, snap)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.trace_log.dbc_decode import DbcDatabase, SignalInfo, SignalSample
from xcp_pcan.trace_log.types import TraceFrame
from xcp_pcan.ui.window_util import configure_tool_window

_PALETTE = (
    QColor("#1565c0"),
    QColor("#c62828"),
    QColor("#2e7d32"),
    QColor("#6a1b9a"),
    QColor("#ef6c00"),
    QColor("#00838f"),
    QColor("#ad1457"),
    QColor("#4527a0"),
)

_MAX_SERIES = 8


def _fmt_id(arb: int) -> str:
    width = 8 if arb > 0x7FF else 3
    return f"0x{arb:0{width}X}"


def _msg_label(fid: int, msg: str) -> str:
    """``0x050 (VDC_0x50)``."""
    return f"{_fmt_id(fid)} ({msg.replace('&', '&&')})"


def _sig_key(info: SignalInfo) -> tuple[int, str]:
    return (info.frame_id, info.signal)


@dataclass(slots=True)
class _Series:
    info: SignalInfo
    samples: list[SignalSample]
    color: QColor
    points: list[tuple[float, float, int]]


class CheckableSignalCombo(QComboBox):
    """Signal dropdown with checkboxes; selection persists across Message switches."""

    checkedChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        le = self.lineEdit()
        le.setReadOnly(True)
        # Editable combo only opens via the arrow unless we open on text clicks.
        le.installEventFilter(self)
        # Consume viewport clicks ourselves: Qt's checkbox indicator toggles the
        # check mark without going through view.pressed, so Selected never updated.
        self.view().viewport().installEventFilter(self)
        self._block = False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.showPopup()
            return
        super().mousePressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        le = self.lineEdit()
        if (
            le is not None
            and obj is le
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.showPopup()
            return True
        if obj is self.view().viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                index = self.view().indexAt(pos)
                if index.isValid():
                    self._toggle_row(index)
                    return True
        return super().eventFilter(obj, event)

    def _toggle_row(self, index) -> None:  # noqa: ANN001
        item = self.model().itemFromIndex(index)
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        checked = item.checkState() == Qt.CheckState.Checked
        self._block = True
        item.setCheckState(
            Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked
        )
        self._block = False
        self._refresh_caption()
        self.checkedChanged.emit()
        # Re-open so multiple signals can be checked in one go.
        QTimer.singleShot(0, self.showPopup)

    def set_signals(
        self,
        infos: list[SignalInfo],
        selected: set[tuple[int, str]],
    ) -> None:
        """Reload items for the current Message; restore checks from ``selected``."""
        self._block = True
        model = self.model()
        assert isinstance(model, QStandardItemModel)
        model.clear()
        for info in infos:
            label = info.signal
            if info.unit:
                label = f"{info.signal}  [{info.unit}]"
            item = QStandardItem(label.replace("&", "&&"))
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setData(info, Qt.ItemDataRole.UserRole)
            item.setCheckable(True)
            item.setCheckState(
                Qt.CheckState.Checked
                if _sig_key(info) in selected
                else Qt.CheckState.Unchecked
            )
            model.appendRow(item)
        self._block = False
        self._refresh_caption()

    def checked_infos(self) -> list[SignalInfo]:
        model = self.model()
        assert isinstance(model, QStandardItemModel)
        out: list[SignalInfo] = []
        for row in range(model.rowCount()):
            item = model.item(row)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            info = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(info, SignalInfo):
                out.append(info)
        return out

    def _refresh_caption(self) -> None:
        n = len(self.checked_infos())
        le = self.lineEdit()
        if le is None:
            return
        if n == 0:
            le.setText("Select signal(s)…")
        elif n == 1:
            info = self.checked_infos()[0]
            le.setText(info.signal)
        else:
            le.setText(f"{n} signals selected")


class _SignalCurve(QWidget):
    """Multi-series line chart with mouse snap + value readout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self._series: list[_Series] = []
        self._t0 = 0.0
        self._xmin = 0.0
        self._xmax = 1.0
        self._ymin = 0.0
        self._ymax = 1.0
        self._plot = QRect()
        self._snap: tuple[str, float, float, QPointF] | None = None

    def set_series_list(self, series: list[_Series]) -> None:
        self._series = list(series)
        self._snap = None
        self._recompute_bounds()
        self.update()

    def _recompute_bounds(self) -> None:
        abs_ts: list[float] = []
        for ser in self._series:
            for s in ser.samples:
                if s.numeric is not None:
                    abs_ts.append(s.timestamp)
        if not abs_ts:
            self._t0 = 0.0
            self._xmin, self._xmax = 0.0, 1.0
            self._ymin, self._ymax = 0.0, 1.0
            for ser in self._series:
                ser.points = []
            return
        self._t0 = min(abs_ts)
        xs: list[float] = []
        ys: list[float] = []
        for ser in self._series:
            pts: list[tuple[float, float, int]] = []
            for i, s in enumerate(ser.samples):
                if s.numeric is None:
                    continue
                x = s.timestamp - self._t0
                y = float(s.numeric)
                pts.append((x, y, i))
                xs.append(x)
                ys.append(y)
            ser.points = pts
        self._xmin, self._xmax = min(xs), max(xs)
        self._ymin, self._ymax = min(ys), max(ys)
        if abs(self._xmax - self._xmin) < 1e-15:
            self._xmax = self._xmin + 1.0
        if abs(self._ymax - self._ymin) < 1e-15:
            self._ymax = self._ymin + 1.0
            self._ymin = self._ymin - 1.0
        ypad = (self._ymax - self._ymin) * 0.05
        self._ymin -= ypad
        self._ymax += ypad

    def _map_pt(self, x: float, y: float) -> QPointF:
        plot = self._plot
        nx = (x - self._xmin) / (self._xmax - self._xmin)
        ny = (y - self._ymin) / (self._ymax - self._ymin)
        return QPointF(
            plot.left() + nx * plot.width(),
            plot.bottom() - ny * plot.height(),
        )

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#fafafa"))

        margin_l, margin_r, margin_t, margin_b = 52, 12, 28, 28
        self._plot = self.rect().adjusted(margin_l, margin_t, -margin_r, -margin_b)
        plot = self._plot
        p.setPen(QPen(QColor("#c4c4c4")))
        p.drawRect(plot)

        font = QFont("Consolas", 9)
        p.setFont(font)
        p.setPen(QColor("#424242"))

        n_pts = sum(len(s.points) for s in self._series)
        if n_pts < 2:
            tip = (
                "Select signal(s) in the dropdown (checkboxes)"
                if not self._series
                else "Selected signal(s) have fewer than 2 numeric samples"
            )
            p.drawText(
                plot.adjusted(8, 8, -8, -8),
                Qt.AlignmentFlag.AlignCenter,
                tip,
            )
            return

        p.drawText(4, plot.top() + 12, f"{self._ymax:.4g}")
        p.drawText(4, plot.bottom(), f"{self._ymin:.4g}")
        p.drawText(plot.left(), self.height() - 8, f"t0+{self._xmin:.3f}s")
        p.drawText(plot.right() - 70, self.height() - 8, f"t0+{self._xmax:.3f}s")

        lx = plot.left() + 4
        for ser in self._series:
            pen = QPen(ser.color)
            pen.setWidth(2)
            p.setPen(pen)
            p.drawLine(lx, 14, lx + 16, 14)
            p.setPen(QColor("#424242"))
            name = ser.info.signal
            p.drawText(lx + 20, 18, name)
            lx += 24 + p.fontMetrics().horizontalAdvance(name) + 12

        for ser in self._series:
            if len(ser.points) < 2:
                continue
            pen = QPen(ser.color)
            pen.setWidth(2)
            p.setPen(pen)
            last = self._map_pt(ser.points[0][0], ser.points[0][1])
            for x, y, _i in ser.points[1:]:
                cur = self._map_pt(x, y)
                p.drawLine(last, cur)
                last = cur

        if self._snap is not None:
            label, _t, _y, pix = self._snap
            p.setPen(QPen(QColor("#212121"), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(pix.x(), plot.top()), QPointF(pix.x(), plot.bottom()))
            p.drawLine(QPointF(plot.left(), pix.y()), QPointF(plot.right(), pix.y()))
            p.setBrush(QColor("#212121"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(pix, 4, 4)
            tip = label
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(tip) + 12
            th = fm.height() + 8
            tx = int(pix.x() + 10)
            ty = int(pix.y() - th - 6)
            if tx + tw > plot.right():
                tx = int(pix.x() - tw - 10)
            if ty < plot.top():
                ty = int(pix.y() + 8)
            box = QRect(tx, ty, tw, th)
            p.setBrush(QColor(255, 255, 255, 230))
            p.setPen(QPen(QColor("#616161")))
            p.drawRect(box)
            p.setPen(QColor("#212121"))
            p.drawText(box.adjusted(6, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, tip)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        del event
        if self._snap is not None:
            self._snap = None
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802, ANN001
        if not self._series:
            return
        if self._plot.width() <= 0 or self._plot.height() <= 0:
            self._plot = self.rect().adjusted(52, 28, -12, -28)
        plot = self._plot
        if plot.width() <= 0:
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        mx, my = float(pos.x()), float(pos.y())
        # Outside plot → clear cursor.
        if not (
            plot.left() <= mx <= plot.right() and plot.top() <= my <= plot.bottom()
        ):
            if self._snap is not None:
                self._snap = None
                self.update()
            return

        # Vertical snap: nearest sample by time (X). Among close X, prefer Y.
        # Horizontal snap: same point drives the Y crosshair.
        best: tuple[float, float, str, float, float, QPointF] | None = None
        for ser in self._series:
            for x, y, idx in ser.points:
                pix = self._map_pt(x, y)
                dx = abs(pix.x() - mx)
                dy = abs(pix.y() - my)
                if best is not None and (dx, dy) >= (best[0], best[1]):
                    continue
                s = ser.samples[idx]
                unit = f" {ser.info.unit}" if ser.info.unit else ""
                tip = (
                    f"{ser.info.signal}={s.text}{unit}  "
                    f"t={s.timestamp:.6f}s  #{s.frame_index + 1}"
                )
                best = (dx, dy, tip, x, y, pix)

        new_snap = None if best is None else (best[2], best[3], best[4], best[5])
        if new_snap != self._snap:
            self._snap = new_snap
            self.update()


class TraceSignalPlotDialog(QDialog):
    """Message dropdown + checkable Signal dropdown (cross-message multi-select)."""

    def __init__(
        self,
        dbc: DbcDatabase,
        frames: list[TraceFrame],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Signal plot")
        self.resize(900, 600)
        configure_tool_window(self)
        self._dbc = dbc
        self._frames = list(frames)
        self._by_message: dict[str, list[SignalInfo]] = {}
        for info in dbc.list_signals():
            self._by_message.setdefault(info.message, []).append(info)
        # Global selection across messages: (frame_id, signal_name) -> SignalInfo
        self._selected: dict[tuple[int, str], SignalInfo] = {}

        mono = QFont("Consolas", 9)
        # Non-editable: click anywhere on the box opens the list (not only ▾).
        self.cmb_msg = QComboBox()
        self.cmb_msg.setFont(mono)
        self.cmb_msg.setEditable(False)
        for msg, fid in dbc.list_messages():
            self.cmb_msg.addItem(_msg_label(fid, msg), msg)

        self.cmb_sig = CheckableSignalCombo()
        self.cmb_sig.setFont(mono)
        if self.cmb_sig.lineEdit() is not None:
            self.cmb_sig.lineEdit().setFont(mono)
        self.cmb_sig.setMinimumWidth(220)

        self.cmb_msg.currentIndexChanged.connect(self._on_message_changed)
        self.cmb_sig.checkedChanged.connect(self._on_signal_checked)

        self._sel_host = QWidget()
        self._sel_row = QHBoxLayout(self._sel_host)
        self._sel_row.setContentsMargins(0, 0, 0, 0)
        self._sel_row.setSpacing(6)
        sel_title = QLabel("Selected:")
        sel_title.setStyleSheet("color:#424242;")
        self._sel_row.addWidget(sel_title)
        self._sel_none = QLabel("(none)")
        self._sel_none.setStyleSheet("color:#757575;")
        self._sel_row.addWidget(self._sel_none)
        self._sel_row.addStretch(1)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color:#616161;")
        self.lbl_info.setWordWrap(True)

        btn_clear = QPushButton("Clear sel")
        btn_clear.setToolTip("Clear all selected signals.")
        btn_clear.clicked.connect(self._clear_selection)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)

        top = QHBoxLayout()
        top.addWidget(QLabel("Message"))
        top.addWidget(self.cmb_msg, stretch=1)
        top.addWidget(QLabel("Signal"))
        top.addWidget(self.cmb_sig, stretch=1)
        top.addWidget(btn_clear)
        top.addWidget(btn_close)

        self._curve = _SignalCurve()
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Time (s)", "Signal", "Value", "Frame #"])
        self._table.setFont(mono)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._curve)
        split.addWidget(self._table)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 200])

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self._sel_host)
        root.addWidget(self.lbl_info)
        root.addWidget(split, stretch=1)

        self._replot_timer = QTimer(self)
        self._replot_timer.setSingleShot(True)
        self._replot_timer.setInterval(100)
        self._replot_timer.timeout.connect(self._replot)

        self._on_message_changed()
        self._update_sel_chips()
        if self._frames:
            self.lbl_info.setText(
                f"{len(self._frames)} Trace frames — open Signal dropdown and check "
                f"items (max {_MAX_SERIES}). Switch Message to add more. "
                f"Click a Selected chip to remove. Hover plot for V/H snap cursor."
            )
        else:
            self.lbl_info.setText("No Trace frames — Open a log first.")

    def _current_message(self) -> str | None:
        msg = self.cmb_msg.currentData()
        if isinstance(msg, str):
            return msg
        # Editable combo can show a valid label with currentIndex=-1.
        text = self.cmb_msg.currentText()
        for i in range(self.cmb_msg.count()):
            if self.cmb_msg.itemText(i) == text:
                data = self.cmb_msg.itemData(i)
                if isinstance(data, str):
                    return data
        return None

    def _on_message_changed(self) -> None:
        msg = self._current_message()
        infos = self._by_message.get(msg, []) if msg else []
        self.cmb_sig.set_signals(infos, set(self._selected.keys()))

    def _on_signal_checked(self) -> None:
        msg = self._current_message()
        if msg is None:
            # Still apply checks from the open Signal list even if Message
            # editable index was cleared.
            for info in self.cmb_sig.checked_infos():
                self._selected[_sig_key(info)] = info
            self._update_sel_chips()
            self._replot_timer.start()
            return
        # Merge current-message checks into global selection.
        current_keys = {_sig_key(i) for i in self._by_message.get(msg, [])}
        # Drop previous checks for this message, then add newly checked.
        for key in list(self._selected):
            if key in current_keys:
                del self._selected[key]
        for info in self.cmb_sig.checked_infos():
            self._selected[_sig_key(info)] = info
        if len(self._selected) > _MAX_SERIES:
            # Revert last wave: keep first N by insertion order.
            keep = dict(list(self._selected.items())[:_MAX_SERIES])
            self._selected = keep
            self.cmb_sig.set_signals(
                self._by_message.get(msg, []), set(self._selected.keys())
            )
            self.lbl_info.setText(
                f"At most {_MAX_SERIES} signals — uncheck one before adding more."
            )
        self._update_sel_chips()
        self._replot_timer.start()

    def _clear_selection(self) -> None:
        self._selected.clear()
        msg = self._current_message()
        infos = self._by_message.get(msg, []) if msg else []
        self.cmb_sig.set_signals(infos, set())
        self._update_sel_chips()
        self._replot()

    def _remove_selected(self, key: tuple[int, str]) -> None:
        if key not in self._selected:
            return
        del self._selected[key]
        msg = self._current_message()
        infos = self._by_message.get(msg, []) if msg else []
        self.cmb_sig.set_signals(infos, set(self._selected.keys()))
        self._update_sel_chips()
        self._replot()

    def _update_sel_chips(self) -> None:
        # Remove previous chip buttons (keep title + none + stretch).
        for i in reversed(range(self._sel_row.count())):
            item = self._sel_row.itemAt(i)
            w = item.widget() if item is not None else None
            if w is None or w is self._sel_none:
                continue
            if isinstance(w, QLabel) and w.text() == "Selected:":
                continue
            if isinstance(w, QPushButton):
                self._sel_row.removeWidget(w)
                w.deleteLater()

        if not self._selected:
            self._sel_none.setVisible(True)
            return
        self._sel_none.setVisible(False)
        # Insert chips before the trailing stretch.
        stretch_idx = self._sel_row.count() - 1
        for key, info in self._selected.items():
            label = f"{_fmt_id(info.frame_id)}.{info.signal}  ×"
            btn = QPushButton(label)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("Click to remove this signal")
            btn.setStyleSheet(
                "QPushButton { color:#1565c0; text-align:left; padding:1px 6px; "
                "border:1px solid #bbdefb; border-radius:3px; background:#e3f2fd; }"
                "QPushButton:hover { background:#bbdefb; }"
            )
            btn.clicked.connect(
                lambda _checked=False, k=key: self._remove_selected(k)
            )
            self._sel_row.insertWidget(stretch_idx, btn)
            stretch_idx += 1

    def _replot(self) -> None:
        infos = list(self._selected.values())
        if not infos:
            self._curve.set_series_list([])
            self._table.setRowCount(0)
            n = len(self._frames)
            self.lbl_info.setText(
                f"{n} Trace frames — check signals in the Signal dropdown."
                if n
                else "No Trace frames — Open a log first."
            )
            return
        if not self._frames:
            self.lbl_info.setText("No Trace frames — Open a log first.")
            self._curve.set_series_list([])
            self._table.setRowCount(0)
            return

        series: list[_Series] = []
        table_rows: list[tuple[float, str, str, int]] = []
        parts: list[str] = []
        for idx, info in enumerate(infos):
            try:
                _msg, _unit, samples = self._dbc.extract_signal_series(
                    self._frames, frame_id=info.frame_id, signal=info.signal
                )
            except KeyError:
                continue
            color = _PALETTE[idx % len(_PALETTE)]
            for s in samples:
                table_rows.append(
                    (s.timestamp, info.signal, s.text, s.frame_index + 1)
                )
            series.append(
                _Series(info=info, samples=samples, color=color, points=[])
            )
            parts.append(f"{info.signal}:{len(samples)}")

        self._curve.set_series_list(series)
        table_rows.sort(key=lambda r: (r[0], r[1]))
        self._fill_table(table_rows)
        self.lbl_info.setText(
            f"{len(infos)} signal(s) [{', '.join(parts)}] — "
            f"hover plot for V/H snap cursor."
        )

    def _fill_table(self, rows: list[tuple[float, str, str, int]]) -> None:
        self._table.setRowCount(0)
        cap = 20_000
        show = rows if len(rows) <= cap else rows[:: max(1, len(rows) // cap)][:cap]
        self._table.setRowCount(len(show))
        for row, (t, sig, val, fr) in enumerate(show):
            self._table.setItem(row, 0, QTableWidgetItem(f"{t:.6f}"))
            self._table.setItem(row, 1, QTableWidgetItem(sig))
            self._table.setItem(row, 2, QTableWidgetItem(val))
            self._table.setItem(row, 3, QTableWidgetItem(str(fr)))
        if len(rows) > len(show):
            self.lbl_info.setText(
                self.lbl_info.text() + f"  Table shows {len(show)}/{len(rows)} rows."
            )
