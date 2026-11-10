"""Combo + Add/Remove for XCP / UDS CAN ID presets (built-in + user-saved)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from xcp_pcan.can_id_presets import (
    CUSTOM_KEY,
    CanIdPreset,
    add_user_preset,
    find_preset,
    is_user_preset_key,
    load_user_presets,
    match_preset_key,
    preset_label,
    remove_user_preset,
)


class IdPresetBar(QWidget):
    """Built-in + persisted user labels; ``+`` saves current TX/RX, ``−`` removes user label."""

    def __init__(
        self,
        *,
        kind: str,
        builtin: Sequence[CanIdPreset],
        edt_tx: QLineEdit,
        edt_rx: QLineEdit,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._builtin = tuple(builtin)
        self._edt_tx = edt_tx
        self._edt_rx = edt_rx
        self._settings = settings

        self.combo = QComboBox(self)
        self.combo.setToolTip(
            "Built-in and saved ID labels. Custom = not in the list. "
            "Use + to save the current TX/RX under a name."
        )
        self.btn_add = QPushButton("+", self)
        self.btn_add.setFixedWidth(28)
        self.btn_add.setToolTip("Save current TX / RX as a named label (kept after restart).")
        self.btn_add.clicked.connect(self._add_label)
        self.btn_del = QPushButton("−", self)
        self.btn_del.setFixedWidth(28)
        self.btn_del.setToolTip("Remove the selected user-saved label (built-in cannot be removed).")
        self.btn_del.clicked.connect(self._remove_label)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.combo, stretch=1)
        lay.addWidget(self.btn_add)
        lay.addWidget(self.btn_del)

        self.combo.currentIndexChanged.connect(self._on_combo_changed)
        self._edt_tx.textEdited.connect(self.sync_from_fields)
        self._edt_rx.textEdited.connect(self.sync_from_fields)
        self.rebuild(select_key=None)

    def all_presets(self) -> list[CanIdPreset]:
        return list(self._builtin) + load_user_presets(self._kind, self._settings)

    def rebuild(self, *, select_key: str | None) -> None:
        current = select_key
        if current is None:
            current = self.combo.currentData()
            if current is not None:
                current = str(current)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("Custom", CUSTOM_KEY)
        for preset in self._builtin:
            self.combo.addItem(preset_label(preset), preset.key)
        users = load_user_presets(self._kind, self._settings)
        if users:
            self.combo.insertSeparator(self.combo.count())
            for preset in users:
                self.combo.addItem(preset_label(preset), preset.key)
        if current:
            idx = self.combo.findData(current)
            self.combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)
        self._update_remove_enabled()

    def sync_from_fields(self) -> None:
        key = match_preset_key(
            self.all_presets(),
            self._edt_tx.text(),
            self._edt_rx.text(),
        )
        self.combo.blockSignals(True)
        idx = self.combo.findData(key)
        self.combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo.blockSignals(False)
        self._update_remove_enabled()

    def _on_combo_changed(self, _index: int = 0) -> None:
        key = self.combo.currentData()
        preset = find_preset(self.all_presets(), str(key) if key is not None else "")
        self._update_remove_enabled()
        if preset is None:
            return
        self._edt_tx.blockSignals(True)
        self._edt_rx.blockSignals(True)
        self._edt_tx.setText(preset.tx)
        self._edt_rx.setText(preset.rx)
        self._edt_tx.blockSignals(False)
        self._edt_rx.blockSignals(False)

    def _update_remove_enabled(self) -> None:
        key = self.combo.currentData()
        self.btn_del.setEnabled(bool(key) and is_user_preset_key(str(key)))

    def _add_label(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Add ID label",
            "Label name (saved for next launches):",
        )
        if not ok:
            return
        try:
            created = add_user_preset(
                self._kind,
                name,
                self._edt_tx.text(),
                self._edt_rx.text(),
                self._settings,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Add ID label", str(exc))
            return
        self.rebuild(select_key=created.key)
        # Ensure fields match saved formatting (0x…).
        self._edt_tx.blockSignals(True)
        self._edt_rx.blockSignals(True)
        self._edt_tx.setText(created.tx)
        self._edt_rx.setText(created.rx)
        self._edt_tx.blockSignals(False)
        self._edt_rx.blockSignals(False)

    def _remove_label(self) -> None:
        key = self.combo.currentData()
        if key is None or not is_user_preset_key(str(key)):
            QMessageBox.information(
                self,
                "Remove ID label",
                "Select a user-saved label first.\nBuilt-in labels cannot be removed.",
            )
            return
        preset = find_preset(self.all_presets(), str(key))
        label = preset.name if preset is not None else str(key)
        answer = QMessageBox.question(
            self,
            "Remove ID label",
            f"Remove saved label «{label}»?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        remove_user_preset(self._kind, str(key), self._settings)
        self.rebuild(select_key=CUSTOM_KEY)
