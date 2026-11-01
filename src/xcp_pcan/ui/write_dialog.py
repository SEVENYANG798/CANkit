"""Write physical value dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from xcp_pcan.symbols.types import Variable


class WriteValueDialog(QDialog):
    """Enter a physical value to DOWNLOAD to ECU memory."""

    def __init__(self, var: Variable, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Write — {var.name}")
        self._var = var
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Name", QLabel(var.name))
        form.addRow("Address", QLabel(f"0x{var.address:08X}"))
        form.addRow("Type", QLabel(var.datatype.value))
        form.addRow("Unit", QLabel(var.unit or "—"))
        self.spn = QDoubleSpinBox()
        self.spn.setDecimals(6)
        self.spn.setRange(-1e12, 1e12)
        self.spn.setValue(0.0)
        form.addRow("Physical value", self.spn)
        layout.addLayout(form)
        hint = QLabel(
            "Value is converted with COMPU_METHOD (if any), then written via XCP DOWNLOAD."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def physical_value(self) -> float:
        return float(self.spn.value())
