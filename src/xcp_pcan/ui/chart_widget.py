"""Simple multi-series strip chart (QPainter)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
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


class StripChartWidget(QWidget):
    def __init__(self, buffer: TraceBuffer, parent=None) -> None:
        super().__init__(parent)
        self._buffer = buffer
        self._visible: set[str] = set()
        self._color_map: dict[str, QColor] = {}
        self.setMinimumHeight(280)
        self.setBackgroundRole(self.backgroundRole())
        self.setAutoFillBackground(True)

    def set_visible_series(self, names: set[str]) -> None:
        self._visible = set(names)
        self.update()

    def color_for(self, name: str) -> QColor:
        if name not in self._color_map:
            idx = len(self._color_map) % len(SERIES_COLORS)
            self._color_map[name] = SERIES_COLORS[idx]
        return self._color_map[name]

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.fillRect(self.rect(), QColor("#fafafa"))

        margin_l, margin_r, margin_t, margin_b = 56, 16, 16, 36
        plot = QRectF(
            rect.left() + margin_l,
            rect.top() + margin_t,
            max(10, rect.width() - margin_l - margin_r),
            max(10, rect.height() - margin_t - margin_b),
        )
        painter.fillRect(plot, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#cccccc")))
        painter.drawRect(plot)

        series_data: list[tuple[str, list[float], list[float], QColor]] = []
        x_min, x_max = 0.0, 1.0
        y_min, y_max = 0.0, 1.0
        any_pts = False
        for name in sorted(self._visible):
            xs, ys = self._buffer.relative_xy(name)
            if len(xs) < 1:
                continue
            any_pts = True
            series_data.append((name, xs, ys, self.color_for(name)))
            x_min = min(x_min, min(xs))
            x_max = max(x_max, max(xs))
            y_min = min(y_min, min(ys))
            y_max = max(y_max, max(ys))

        if not any_pts:
            painter.setPen(QColor("#888888"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No data yet")
            return

        if x_max <= x_min:
            x_max = x_min + 1.0
        if y_max <= y_min:
            y_max = y_min + 1.0
            y_min = y_min - 1.0
        # pad y
        y_pad = (y_max - y_min) * 0.08 or 1.0
        y_min -= y_pad
        y_max += y_pad

        # grid + axis labels
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QPen(QColor("#e0e0e0"), 1, Qt.PenStyle.DashLine))
        for i in range(5):
            gy = plot.top() + plot.height() * i / 4
            painter.drawLine(QPointF(plot.left(), gy), QPointF(plot.right(), gy))
            val = y_max - (y_max - y_min) * i / 4
            painter.setPen(QColor("#666666"))
            painter.drawText(
                QRectF(rect.left(), gy - 8, margin_l - 4, 16),
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

        def map_pt(x: float, y: float) -> QPointF:
            nx = (x - x_min) / (x_max - x_min)
            ny = (y - y_min) / (y_max - y_min)
            return QPointF(
                plot.left() + nx * plot.width(),
                plot.bottom() - ny * plot.height(),
            )

        for _name, xs, ys, color in series_data:
            pen = QPen(color, 1.8)
            painter.setPen(pen)
            if len(xs) == 1:
                p = map_pt(xs[0], ys[0])
                painter.drawEllipse(p, 3, 3)
                continue
            for i in range(1, len(xs)):
                painter.drawLine(map_pt(xs[i - 1], ys[i - 1]), map_pt(xs[i], ys[i]))

        painter.setPen(QColor("#444444"))
        painter.drawText(
            QRectF(plot.left(), rect.bottom() - 14, plot.width(), 14),
            Qt.AlignmentFlag.AlignHCenter,
            "Time (s)",
        )
