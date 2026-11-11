"""XCP Viewer tool window: Symbols + Polling / DAQ watch."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings, QSize
from PySide6.QtWidgets import QVBoxLayout, QWidget

from xcp_pcan.ui.window_util import configure_tool_window


class XcpViewerDialog(QWidget):
    """Hosts the Symbols | Watch splitter as a separate tool window."""

    def __init__(
        self,
        content: QWidget,
        settings: QSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("XCP Viewer")
        self.resize(1100, 640)
        configure_tool_window(self)
        self._settings = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(content)
        restored = settings.value("xcp_viewer/geometry")
        if isinstance(restored, QByteArray) and not restored.isEmpty():
            self.restoreGeometry(restored)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(1100, 640)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._settings.setValue("xcp_viewer/geometry", self.saveGeometry())
        super().closeEvent(event)
