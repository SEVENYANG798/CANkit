"""Simple multi-series strip chart (QPainter) with pan / zoom / snap cursor."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from xcp_pcan.ui.trace_buffer import TraceBuffer

# Distinct colors for up to ~12 series
SERIES_COLORS = [
    QColor("#1f77b4"),
    QColor("#ff7f0e"),
    QColor("#2ca02c"),
    QColor("#d62728"),
    QColor("#9467bd"),
    QColor("#8c564b"),
    QColor("#e377c2"),
    QColor("#7f7f7f"),
    QColor("#bcbd22"),
    QColor("#17becf"),
    QColor("#393b79"),
    QColor("#ad494a"),
]

_MARGIN_L, _MARGIN_R, _MARGIN_T, _MARGIN_B = 56, 16, 16, 36
_ZOOM_STEP = 1.15
_SNAP_PX = 14.0  # pixel radius to snap onto a sample


class StripChartWidget(QWidget):
    def __init__(self, buffer: TraceBuffer, parent=None) -> None:
        super().__init__(parent)
        self._buffer = buffer
        self._visible: set[str] = set()
        self._color_map: dict[str, QColor] = {}
        # None = auto-fit to visible data each paint.
        self._x_range: tuple[float, float] | None = None
        self._y_range: tuple[float, float] | None = None
        self._plot = QRectF()
        self._view_x = (0.0, 1.0)
        self._view_y = (0.0, 1.0)
        self._panning = False
        self._pan_last = QPoint()
        self._cursor_pos: QPointF | None = None  # widget coords; None = outside plot
        # When paused: paint from this snapshot so live appends cannot move the curves.
        self._frozen: list[tuple[str, list[float], list[float], QColor]] | None = None
        self.setMinimumHeight(280)
        self.setBackgroundRole(self.backgroundRole())
        self.setAutoFillBackground(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, False)

    def set_visible_series(self, names: set[str]) -> None:
        self._visible = set(names)
        if self._frozen is not None:
            # Re-snapshot so show/hide while paused stays consistent with visibility.
            self._frozen = self._snapshot_series()
        self.update()

    def reset_view(self) -> None:
        """Fit axes to all visible (or frozen) series data."""
        self._x_range = None
        self._y_range = None
        # When paused/frozen, immediately lock to fitted extents so the next
        # paint does not leave a stale zoomed window.
        if self._frozen is not None and self._ensure_view_ranges(self._frozen):
            self._x_range = self._view_x
            self._y_range = self._view_y
        self.update()

    def view_locked(self) -> bool:
        """True when pan/zoom locked the axes (double-click fits again)."""
        return self._x_range is not None or self._y_range is not None

    def view_state(self) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        return self._x_range, self._y_range

    def set_view_state(
        self,
        x_range: tuple[float, float] | None,
        y_range: tuple[float, float] | None,
    ) -> None:
        self._x_range = x_range
        self._y_range = y_range
        self.update()

    def set_frozen(self, frozen: bool) -> None:
        """Freeze/unfreeze displayed curves (used when the chart is paused)."""
        if frozen:
            if not self._ensure_view_ranges():
                self._frozen = []
            else:
                # Lock axes so auto-fit cannot chase live buffer while paused.
                self._x_range = self._view_x
                self._y_range = self._view_y
                self._frozen = self._snapshot_series()
        else:
            self._frozen = None
            # Resume live follow.
            self._x_range = None
            self._y_range = None
        self.update()

    def color_for(self, name: str) -> QColor:
        if name not in self._color_map:
            idx = len(self._color_map) % len(SERIES_COLORS)
            self._color_map[name] = SERIES_COLORS[idx]
        return self._color_map[name]

    def _snapshot_series(self) -> list[tuple[str, list[float], list[float], QColor]]:
        out: list[tuple[str, list[float], list[float], QColor]] = []
        for name in sorted(self._visible):
            xs, ys = self._buffer.relative_xy(name)
            if len(xs) < 1:
                continue
            out.append((name, list(xs), list(ys), self.color_for(name)))
        return out

    def _series_data(self) -> list[tuple[str, list[float], list[float], QColor]]:
        if self._frozen is not None:
            return self._frozen
        return self._snapshot_series()

    def _plot_rect(self) -> QRectF:
        rect = self.rect().adjusted(8, 8, -8, -8)
        return QRectF(
            rect.left() + _MARGIN_L,
            rect.top() + _MARGIN_T,
            max(10, rect.width() - _MARGIN_L - _MARGIN_R),
            max(10, rect.height() - _MARGIN_T - _MARGIN_B),
        )

    def _data_extents(
        self, series: list[tuple[str, list[float], list[float], QColor]]
    ) -> tuple[float, float, float, float] | None:
        any_pts = False
        x_min = x_max = y_min = y_max = 0.0
        for _name, xs, ys, _color in series:
            if len(xs) < 1:
                continue
            if not any_pts:
                x_min, x_max = min(xs), max(xs)
                y_min, y_max = min(ys), max(ys)
                any_pts = True
            else:
                x_min = min(x_min, min(xs))
                x_max = max(x_max, max(xs))
                y_min = min(y_min, min(ys))
                y_max = max(y_max, max(ys))
        if not any_pts:
            return None
        if x_max <= x_min:
            x_max = x_min + 1.0
        if y_max <= y_min:
            y_max = y_min + 1.0
            y_min = y_min - 1.0
        y_pad = (y_max - y_min) * 0.08 or 1.0
        return x_min, x_max, y_min - y_pad, y_max + y_pad

    def _ensure_view_ranges(
        self, series: list[tuple[str, list[float], list[float], QColor]] | None = None
    ) -> bool:
        """Resolve current view into ``_view_x`` / ``_view_y``. False if no data."""
        data = series if series is not None else self._series_data()
        extents = self._data_extents(data)
        if extents is None:
            return False
        ax0, ax1, ay0, ay1 = extents
        if self._x_range is None:
            self._view_x = (ax0, ax1)
        else:
            self._view_x = self._x_range
        if self._y_range is None:
            self._view_y = (ay0, ay1)
        else:
            self._view_y = self._y_range
        return True

    def _map_pt(self, x: float, y: float, plot: QRectF | None = None) -> QPointF:
        plot = plot if plot is not None else (
            self._plot if self._plot.width() > 0 else self._plot_rect()
        )
        x0, x1 = self._view_x
        y0, y1 = self._view_y
        dx = x1 - x0 if x1 != x0 else 1.0
        dy = y1 - y0 if y1 != y0 else 1.0
        nx = (x - x0) / dx
        ny = (y - y0) / dy
        return QPointF(
            plot.left() + nx * plot.width(),
            plot.bottom() - ny * plot.height(),
        )

    def _pixel_to_data(self, pos: QPointF) -> tuple[float, float] | None:
        plot = self._plot if self._plot.width() > 0 else self._plot_rect()
        if plot.width() <= 0 or plot.height() <= 0:
            return None
        x0, x1 = self._view_x
        y0, y1 = self._view_y
        nx = (pos.x() - plot.left()) / plot.width()
        ny = (plot.bottom() - pos.y()) / plot.height()
        return x0 + nx * (x1 - x0), y0 + ny * (y1 - y0)

    @staticmethod
    def _closest_on_segment(
        pos: QPointF, a: QPointF, b: QPointF
    ) -> tuple[QPointF, float]:
        """Return (point on AB closest to pos, t in [0,1])."""
        abx = b.x() - a.x()
        aby = b.y() - a.y()
        ab2 = abx * abx + aby * aby
        if ab2 < 1e-9:
            return QPointF(a), 0.0
        t = ((pos.x() - a.x()) * abx + (pos.y() - a.y()) * aby) / ab2
        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        return QPointF(a.x() + t * abx, a.y() + t * aby), t

    def _find_snap(
        self,
        pos: QPointF,
        series: list[tuple[str, list[float], list[float], QColor]],
        plot: QRectF,
    ) -> tuple[str, float, float, QPointF, QColor] | None:
        """Nearest sample or polyline segment within ``_SNAP_PX`` (incl. vertical)."""
        best: tuple[str, float, float, QPointF, QColor] | None = None
        best_d2 = _SNAP_PX * _SNAP_PX
        data = self._pixel_to_data(pos)
        if data is None:
            return None
        tx, _ty = data
        for name, xs, ys, color in series:
            n = len(xs)
            if n < 1:
                continue
            lo, hi = 0, n - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if xs[mid] < tx:
                    lo = mid + 1
                else:
                    hi = mid
            # Wide window: vertical drops share nearly the same timestamp.
            start = max(0, lo - 80)
            end = min(n, lo + 81)
            for i in range(start, end):
                p = self._map_pt(xs[i], ys[i], plot)
                ddx = p.x() - pos.x()
                ddy = p.y() - pos.y()
                d2 = ddx * ddx + ddy * ddy
                if d2 <= best_d2:
                    best_d2 = d2
                    best = (name, xs[i], ys[i], p, color)
            # Segment snap (covers midpoints of vertical edges).
            seg_lo = max(1, start)
            seg_hi = min(n, end + 1)
            for i in range(seg_lo, seg_hi):
                a = self._map_pt(xs[i - 1], ys[i - 1], plot)
                b = self._map_pt(xs[i], ys[i], plot)
                # Skip tiny segments.
                if abs(a.x() - b.x()) < 0.5 and abs(a.y() - b.y()) < 0.5:
                    continue
                closest, t = self._closest_on_segment(pos, a, b)
                ddx = closest.x() - pos.x()
                ddy = closest.y() - pos.y()
                d2 = ddx * ddx + ddy * ddy
                if d2 <= best_d2:
                    best_d2 = d2
                    sx = xs[i - 1] + t * (xs[i] - xs[i - 1])
                    sy = ys[i - 1] + t * (ys[i] - ys[i - 1])
                    best = (name, sx, sy, closest, color)
        return best

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            if not self._ensure_view_ranges():
                return
            self._x_range = self._view_x
            self._y_range = self._view_y
            self._panning = True
            self._pan_last = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        plot = self._plot if self._plot.width() > 0 else self._plot_rect()
        if self._panning and self._x_range is not None and self._y_range is not None:
            cur = pos.toPoint()
            dx_px = cur.x() - self._pan_last.x()
            dy_px = cur.y() - self._pan_last.y()
            self._pan_last = cur
            x0, x1 = self._x_range
            y0, y1 = self._y_range
            if plot.width() > 0:
                dx = -dx_px / plot.width() * (x1 - x0)
                self._x_range = (x0 + dx, x1 + dx)
            if plot.height() > 0:
                dy = dy_px / plot.height() * (y1 - y0)
                self._y_range = (y0 + dy, y1 + dy)
            self._view_x = self._x_range
            self._view_y = self._y_range
            self._cursor_pos = pos if plot.contains(pos) else None
            self.update()
            event.accept()
            return
        new_pos = pos if plot.contains(pos) else None
        if new_pos != self._cursor_pos:
            self._cursor_pos = new_pos
            self.update()
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._cursor_pos is not None:
            self._cursor_pos = None
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if not self._ensure_view_ranges():
            return
        plot = self._plot if self._plot.width() > 0 else self._plot_rect()
        pos = event.position()
        if not plot.contains(pos):
            return
        anchor = self._pixel_to_data(pos)
        if anchor is None:
            return
        ax, ay = anchor
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.0 / _ZOOM_STEP if delta > 0 else _ZOOM_STEP
        x0, x1 = self._view_x
        y0, y1 = self._view_y

        def _zoom(lo: float, hi: float, center: float) -> tuple[float, float]:
            span = (hi - lo) * factor
            if span <= 0:
                return lo, hi
            t = 0.0 if hi == lo else (center - lo) / (hi - lo)
            new_lo = center - t * span
            return new_lo, new_lo + span

        self._x_range = _zoom(x0, x1, ax)
        self._y_range = _zoom(y0, y1, ay)
        self._view_x = self._x_range
        self._view_y = self._y_range
        self.update()
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.fillRect(self.rect(), QColor("#fafafa"))

        plot = self._plot_rect()
        self._plot = plot
        painter.fillRect(plot, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#cccccc")))
        painter.drawRect(plot)

        series_data = self._series_data()
        if not series_data or not self._ensure_view_ranges(series_data):
            painter.setPen(QColor("#888888"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No data yet")
            return

        x_min, x_max = self._view_x
        y_min, y_max = self._view_y
        if x_max <= x_min:
            x_max = x_min + 1.0
        if y_max <= y_min:
            y_max = y_min + 1.0

        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QPen(QColor("#e0e0e0"), 1, Qt.PenStyle.DashLine))
        for i in range(5):
            gy = plot.top() + plot.height() * i / 4
            painter.drawLine(QPointF(plot.left(), gy), QPointF(plot.right(), gy))
            val = y_max - (y_max - y_min) * i / 4
            painter.setPen(QColor("#666666"))
            painter.drawText(
                QRectF(rect.left(), gy - 8, _MARGIN_L - 4, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{val:.4g}",
            )
            painter.setPen(QPen(QColor("#e0e0e0"), 1, Qt.PenStyle.DashLine))
        for i in range(5):
            gx = plot.left() + plot.width() * i / 4
            painter.drawLine(QPointF(gx, plot.top()), QPointF(gx, plot.bottom()))
            val = x_min + (x_max - x_min) * i / 4
            painter.setPen(QColor("#666666"))
            painter.drawText(
                QRectF(gx - 30, plot.bottom() + 4, 60, 16),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                f"{val:.2f}s",
            )
            painter.setPen(QPen(QColor("#e0e0e0"), 1, Qt.PenStyle.DashLine))

        painter.setClipRect(plot)
        for _name, xs, ys, color in series_data:
            pen = QPen(color, 1.8)
            painter.setPen(pen)
            if len(xs) == 1:
                p = self._map_pt(xs[0], ys[0], plot)
                painter.drawEllipse(p, 3, 3)
                continue
            for i in range(1, len(xs)):
                painter.drawLine(
                    self._map_pt(xs[i - 1], ys[i - 1], plot),
                    self._map_pt(xs[i], ys[i], plot),
                )
        painter.setClipping(False)

        if self._cursor_pos is not None and plot.contains(self._cursor_pos):
            snap = self._find_snap(self._cursor_pos, series_data, plot)
            if snap is not None:
                name, sx, sy, sp, color = snap
                cx, cy = sp.x(), sp.y()
                painter.setPen(QPen(QColor("#666666"), 1, Qt.PenStyle.DotLine))
                painter.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))
                painter.drawLine(QPointF(plot.left(), cy), QPointF(plot.right(), cy))
                painter.setBrush(color)
                painter.setPen(QPen(QColor("#ffffff"), 1.5))
                painter.drawEllipse(sp, 5, 5)
                unit = self._buffer.unit(name)
                unit_s = f" {unit}" if unit else ""
                label = f"{name}\nt={sx:.3f}s   y={sy:.6g}{unit_s}"
                painter.setFont(QFont("Consolas", 9))
                metrics = painter.fontMetrics()
                lines = label.split("\n")
                tw = max(metrics.horizontalAdvance(line) for line in lines) + 12
                th = metrics.height() * len(lines) + 10
                bx = min(max(plot.left() + 4, cx + 12), plot.right() - tw - 4)
                by = min(max(plot.top() + 4, cy - th - 8), plot.bottom() - th - 4)
                badge = QRectF(bx, by, tw, th)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 230))
                painter.drawRoundedRect(badge, 3, 3)
                painter.setPen(QColor("#222222"))
                painter.drawText(
                    badge.adjusted(6, 4, -6, -4),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )
            else:
                data = self._pixel_to_data(self._cursor_pos)
                if data is not None:
                    cx, cy = self._cursor_pos.x(), self._cursor_pos.y()
                    dx, dy = data
                    painter.setPen(QPen(QColor("#888888"), 1, Qt.PenStyle.DotLine))
                    painter.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))
                    painter.drawLine(QPointF(plot.left(), cy), QPointF(plot.right(), cy))
                    label = f"t={dx:.3f}s   y={dy:.6g}"
                    painter.setFont(QFont("Consolas", 9))
                    metrics = painter.fontMetrics()
                    tw = metrics.horizontalAdvance(label) + 12
                    th = metrics.height() + 8
                    bx = min(max(plot.left() + 4, cx + 12), plot.right() - tw - 4)
                    by = min(max(plot.top() + 4, cy - th - 8), plot.bottom() - th - 4)
                    badge = QRectF(bx, by, tw, th)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(255, 255, 255, 220))
                    painter.drawRoundedRect(badge, 3, 3)
                    painter.setPen(QColor("#222222"))
                    painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(QColor("#444444"))
        painter.drawText(
            QRectF(plot.left(), rect.bottom() - 14, plot.width(), 14),
            Qt.AlignmentFlag.AlignHCenter,
            "Time (s)",
        )
