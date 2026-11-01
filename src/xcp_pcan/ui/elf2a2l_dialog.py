"""Dialog: generate an A2L from ELF globals (elf2a2l)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.elf2a2l.a2l_gen import (
    BUNDLED_TEMPLATES,
    DEFAULT_CAN_ID_MASTER,
    DEFAULT_CAN_ID_SLAVE,
    bundled_template_path,
    default_template_path,
    normalize_can_id,
    parse_template_can_ids,
)
from xcp_pcan.elf2a2l.a2l_update import load_a2l

_CUSTOM = "Custom…"


class Elf2A2lDialog(QDialog):
    def __init__(
        self,
        settings: QSettings,
        *,
        master_id: str = "",
        slave_id: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create A2L from ELF")
        self.resize(640, 300)
        self._settings = settings

        self.cmb_preset = QComboBox()
        for label, filename, _m, _s in BUNDLED_TEMPLATES:
            path = bundled_template_path(filename)
            if path.is_file():
                self.cmb_preset.addItem(f"{label} ({filename})", filename)
        self.cmb_preset.addItem(_CUSTOM, "")

        self.edt_elf = QLineEdit(settings.value("elf2a2l/elf", "", str))
        self.edt_out = QLineEdit(settings.value("elf2a2l/output", "", str))
        tpl = settings.value("elf2a2l/template", "", str) or str(default_template_path())
        self.edt_tpl = QLineEdit(tpl)
        self.edt_master = QLineEdit(
            settings.value("elf2a2l/master", "", str) or master_id or DEFAULT_CAN_ID_MASTER
        )
        self.edt_slave = QLineEdit(
            settings.value("elf2a2l/slave", "", str) or slave_id or DEFAULT_CAN_ID_SLAVE
        )
        self.chk_dwarf = QCheckBox("Expand struct members (DWARF, slower)")
        self.chk_dwarf.setChecked(settings.value("elf2a2l/dwarf", False, bool))

        form = QFormLayout()
        form.addRow("ELF", self._file_row(self.edt_elf, self._browse_elf))
        form.addRow("Output A2L", self._file_row(self.edt_out, self._browse_out))
        form.addRow("Preset", self.cmb_preset)
        form.addRow("Template", self._file_row(self.edt_tpl, self._browse_tpl))
        form.addRow("Master TX ID", self.edt_master)
        form.addRow("Slave RX ID", self.edt_slave)
        form.addRow("", self.chk_dwarf)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

        self.cmb_preset.currentIndexChanged.connect(self._on_preset_changed)
        self.cmb_preset.blockSignals(True)
        try:
            self._select_preset_for_path(tpl)
        finally:
            self.cmb_preset.blockSignals(False)

    def _file_row(self, edit: QLineEdit, slot) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("…")
        btn.setFixedWidth(28)
        btn.clicked.connect(slot)
        lay.addWidget(edit, stretch=1)
        lay.addWidget(btn)
        return row

    def _select_preset_for_path(self, tpl: str) -> None:
        name = Path(tpl).name if tpl else ""
        for i in range(self.cmb_preset.count()):
            if self.cmb_preset.itemData(i) == name:
                self.cmb_preset.setCurrentIndex(i)
                return
        self.cmb_preset.setCurrentIndex(self.cmb_preset.count() - 1)

    def _on_preset_changed(self, _index: int = 0) -> None:
        filename = self.cmb_preset.currentData()
        if not filename:
            return
        path = bundled_template_path(str(filename))
        if not path.is_file():
            return
        self.edt_tpl.setText(str(path))
        for _label, fname, master, slave in BUNDLED_TEMPLATES:
            if fname == filename:
                self.edt_master.setText(master)
                self.edt_slave.setText(slave)
                return
        try:
            m, s = parse_template_can_ids(load_a2l(path))
            if m:
                self.edt_master.setText(m)
            if s:
                self.edt_slave.setText(s)
        except OSError:
            pass

    def _browse_elf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "ELF", self._dir(self.edt_elf.text()), "ELF (*.elf *.out *.axf);;All (*.*)"
        )
        if path:
            self.edt_elf.setText(path)
            if not self.edt_out.text().strip():
                self.edt_out.setText(str(Path(path).with_suffix(".a2l")))

    def _browse_out(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Output A2L",
            self.edt_out.text() or self._dir(self.edt_elf.text()),
            "A2L (*.a2l)",
        )
        if path:
            self.edt_out.setText(path)

    def _browse_tpl(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "A2L template", self._dir(self.edt_tpl.text()), "A2L (*.a2l);;All (*.*)"
        )
        if path:
            self.edt_tpl.setText(path)
            self.cmb_preset.blockSignals(True)
            try:
                self.cmb_preset.setCurrentIndex(self.cmb_preset.count() - 1)
            finally:
                self.cmb_preset.blockSignals(False)
            try:
                m, s = parse_template_can_ids(load_a2l(path))
                if m:
                    self.edt_master.setText(m)
                if s:
                    self.edt_slave.setText(s)
            except OSError:
                pass

    @staticmethod
    def _dir(current: str) -> str:
        p = Path(current) if current else None
        if p and p.parent.is_dir():
            return str(p.parent if p.suffix else p)
        return ""

    def _accept(self) -> None:
        elf = self.edt_elf.text().strip()
        out = self.edt_out.text().strip()
        tpl = self.edt_tpl.text().strip()
        if not elf or not Path(elf).is_file():
            QMessageBox.warning(self, "Create A2L from ELF", "Select a valid ELF file.")
            return
        if not out:
            QMessageBox.warning(self, "Create A2L from ELF", "Select an output A2L path.")
            return
        if not tpl or not Path(tpl).is_file():
            QMessageBox.warning(self, "Create A2L from ELF", "Select a valid A2L template.")
            return
        try:
            master = normalize_can_id(self.edt_master.text(), DEFAULT_CAN_ID_MASTER)
            slave = normalize_can_id(self.edt_slave.text(), DEFAULT_CAN_ID_SLAVE)
        except ValueError:
            QMessageBox.warning(self, "Create A2L from ELF", "CAN IDs must be hex, e.g. 0x6A6.")
            return
        self.edt_master.setText(master)
        self.edt_slave.setText(slave)
        self._settings.setValue("elf2a2l/elf", elf)
        self._settings.setValue("elf2a2l/output", out)
        self._settings.setValue("elf2a2l/template", tpl)
        self._settings.setValue("elf2a2l/master", master)
        self._settings.setValue("elf2a2l/slave", slave)
        self._settings.setValue("elf2a2l/dwarf", self.chk_dwarf.isChecked())
        self.accept()

    def values(self) -> dict:
        return {
            "elf": self.edt_elf.text().strip(),
            "output": self.edt_out.text().strip(),
            "template": self.edt_tpl.text().strip(),
            "master": self.edt_master.text().strip(),
            "slave": self.edt_slave.text().strip(),
            "expand_structs": self.chk_dwarf.isChecked(),
        }
