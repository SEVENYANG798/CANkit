"""Live curve viewer: show/hide series, pause redraw, save PNG after stop."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImageWriter
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.ui.chart_widget import StripChartWidget
from xcp_pcan.ui.trace_buffer import TraceBuffer
from xcp_pcan.ui.window_util import configure_tool_window


class CurveChartDialog(QDialog):
    """Non-modal-friendly dialog for watching measurement curves."""

    def __init__(
        self,
        buffer: TraceBuffer,
        *,
        title: str = "Live curves",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 520)
        configure_tool_window(self)

        self._buffer = buffer
        self._paused = False
        self._monitoring = False
        self._checkboxes: dict[str, QCheckBox] = {}

        root = QHBoxLayout(self)

        # Left: series visibility
        left = QVBoxLayout()
        left.addWidget(QLabel("Series (show / hide)"))
        self._series_host = QWidget()
        self._series_layout = QVBoxLayout(self._series_host)
        self._series_layout.setContentsMargins(0, 0, 0, 0)
        self._series_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._series_host)
        scroll.setFixedWidth(200)
        left.addWidget(scroll, stretch=1)

        btn_all = QPushButton("Show all")
        btn_all.clicked.connect(self._show_all)
        btn_none = QPushButton("Hide all")
        btn_none.clicked.connect(self._hide_all)
        left.addWidget(btn_all)
        left.addWidget(btn_none)
        root.addLayout(left)

        # Right: chart + controls
        right = QVBoxLayout()
        self._chart = StripChartWidget(buffer)
        right.addWidget(self._chart, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray;")
        right.addWidget(self._status)

        ctrl = QHBoxLayout()
        self.btn_pause = QPushButton("Pause chart")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause_toggled)
        self.btn_clear = QPushButton("Clear data")
        self.btn_clear.clicked.connect(self._clear_data)
        self.btn_save = QPushButton("Save chart")
        self.btn_save.clicked.connect(self._save_chart)
        self.btn_refresh_list = QPushButton("Refresh series")
        self.btn_refresh_list.clicked.connect(self.sync_series_list)
        ctrl.addWidget(self.btn_pause)
        ctrl.addWidget(self.btn_clear)
        ctrl.addWidget(self.btn_save)
        ctrl.addWidget(self.btn_refresh_list)
        ctrl.addStretch()
        right.addLayout(ctrl)
        root.addLayout(right, stretch=1)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        self.sync_series_list()
        self.set_monitoring(False)

    def set_monitoring(self, running: bool) -> None:
        """Tell the dialog whether acquisition is currently running."""
        self._monitoring = running
        # Prefer saving after stop/pause of monitoring; still allow anytime if data exists
        self._update_save_enabled()
        self._update_status()

    def _update_save_enabled(self) -> None:
        has_data = bool(self._buffer.names())
        # Enabled when not monitoring, or always if user paused chart — user asked
        # save after pause/stop monitoring; keep enabled when stopped or chart paused.
        self.btn_save.setEnabled(has_data and (not self._monitoring or self._paused))

    def _update_status(self) -> None:
        mon = "Monitoring" if self._monitoring else "Stopped"
        pause = " | Chart paused" if self._paused else ""
        n = len(self._buffer.names())
        self._status.setText(f"{mon}{pause} | {n} series in buffer")

    def sync_series_list(self) -> None:
        names = self._buffer.names()
        # Remove stale checkboxes
        for name in list(self._checkboxes):
            if name not in names:
                cb = self._checkboxes.pop(name)
                self._series_layout.removeWidget(cb)
                cb.deleteLater()
        # Add new
        for name in names:
            if name in self._checkboxes:
                continue
            unit = self._buffer.unit(name)
            label = f"{name}" + (f" [{unit}]" if unit else "")
            cb = QCheckBox(label)
            cb.setChecked(True)
            color = self._chart.color_for(name)
            cb.setStyleSheet(f"QCheckBox {{ color: {color.name()}; }}")
            cb.toggled.connect(self._on_visibility_changed)
            # insert before stretch
            self._series_layout.insertWidget(self._series_layout.count() - 1, cb)
            self._checkboxes[name] = cb
        self._apply_visibility()
        self._update_save_enabled()
        self._update_status()

    def _visible_names(self) -> set[str]:
        return {n for n, cb in self._checkboxes.items() if cb.isChecked()}

    def _apply_visibility(self) -> None:
        self._chart.set_visible_series(self._visible_names())

    def _on_visibility_changed(self, _checked: bool = False) -> None:
        self._apply_visibility()

    def _show_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def _hide_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self.btn_pause.setText("Resume chart" if paused else "Pause chart")
        self._update_save_enabled()
        self._update_status()

    def _clear_data(self) -> None:
        self._buffer.clear()
        for cb in list(self._checkboxes.values()):
            self._series_layout.removeWidget(cb)
            cb.deleteLater()
        self._checkboxes.clear()
        self._apply_visibility()
        self._update_save_enabled()
        self._update_status()

    def _on_tick(self) -> None:
        # Keep checkbox list in sync when new symbols appear
        current = set(self._checkboxes)
        names = set(self._buffer.names())
        if names != current:
            self.sync_series_list()
        if not self._paused:
            self._chart.update()
        self._update_save_enabled()

    def _save_chart(self) -> None:
        if not self._buffer.names():
            QMessageBox.information(self, "No data", "Buffer is empty.")
            return
        if self._monitoring and not self._paused:
            QMessageBox.information(
                self,
                "Still monitoring",
                "Pause the chart or stop monitoring before saving.",
            )
            return
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Save chart",
            "xcp_curves.png",
            "PNG image (*.png);;JPEG image (*.jpg *.jpeg)",
        )
        if not path:
            return
        # Ensure extension
        lower = path.lower()
        if not (lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg")):
            path += ".png"
        # Force one redraw with current visibility
        self._apply_visibility()
        self._chart.repaint()
        pix = self._chart.grab()
        ok = pix.save(path)
        if not ok:
            # Fallback via QImageWriter for diagnostics
            writer = QImageWriter(path)
            ok = writer.write(pix.toImage())
        if ok:
            QMessageBox.information(self, "Saved", f"Saved chart to:\n{path}")
        else:
            QMessageBox.critical(self, "Save failed", f"Could not write:\n{path}")
