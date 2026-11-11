"""PCAN-View-style Connect dialog hosting the tabbed ConnectionPanel."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.ui.connection_panel import ConnectionPanel


class ConnectDialog(QDialog):
    """Embeds an existing ConnectionPanel; OK means Connect when not linked."""

    def __init__(
        self,
        panel: ConnectionPanel,
        *,
        bus_open: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect" if not bus_open else "CAN configuration")
        self.resize(560, 420)
        self._panel = panel
        self._bus_open = bus_open

        root = QVBoxLayout(self)
        # Panel lives hidden off-layout on MainWindow; reparent + unhide for the dialog.
        root.addWidget(panel, stretch=1)
        panel.setVisible(True)
        panel.show()

        if bus_open:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            # Close maps to rejected on QDialogButtonBox.
            buttons.rejected.connect(self.accept)
        else:
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
            if ok is not None:
                ok.setText("Connect")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @property
    def should_connect(self) -> bool:
        return (not self._bus_open) and self.result() == QDialog.DialogCode.Accepted
