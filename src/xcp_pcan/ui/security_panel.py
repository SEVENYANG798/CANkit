"""Seed&Key + calibration page panel."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.project import SecuritySettings
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.seedkey import SeedKeyError, SeedKeyProvider, load_seedkey


class SecurityPanel(QGroupBox):
    unlock_clicked = Signal()
    set_cal_page_clicked = Signal()
    get_cal_page_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Security & Cal page", parent)
        layout = QHBoxLayout(self)

        form = QFormLayout()
        sk_row = QHBoxLayout()
        self.edt_seedkey = QLineEdit()
        self.edt_seedkey.setPlaceholderText("Seed&Key .py / .dll (optional)")
        self.edt_seedkey.setToolTip(
            "Python plugin with compute_key(resource, seed)->bytes, "
            "or a SeedNKey-style DLL exporting XCP_ComputeKeyFromSeed."
        )
        btn_sk = QPushButton("…")
        btn_sk.setFixedWidth(28)
        btn_sk.clicked.connect(self._browse_seedkey)
        sk_row.addWidget(self.edt_seedkey, stretch=1)
        sk_row.addWidget(btn_sk)
        form.addRow("Seed&Key", sk_row)

        self.edt_dll_symbol = QLineEdit("XCP_ComputeKeyFromSeed")
        self.edt_dll_symbol.setToolTip("DLL export name (ignored for .py plugins)")
        form.addRow("DLL symbol", self.edt_dll_symbol)

        self.chk_auto_unlock = QCheckBox("Auto unlock after CONNECT")
        form.addRow("", self.chk_auto_unlock)

        res_row = QHBoxLayout()
        self.chk_unlock_cal = QCheckBox("CAL")
        self.chk_unlock_cal.setChecked(True)
        self.chk_unlock_daq = QCheckBox("DAQ")
        self.chk_unlock_daq.setChecked(True)
        self.chk_unlock_stim = QCheckBox("STIM")
        self.chk_unlock_pgm = QCheckBox("PGM")
        for w in (
            self.chk_unlock_cal,
            self.chk_unlock_daq,
            self.chk_unlock_stim,
            self.chk_unlock_pgm,
        ):
            res_row.addWidget(w)
        res_row.addStretch()
        form.addRow("Unlock resources", res_row)

        self.chk_auto_cal_page = QCheckBox("Auto SET_CAL_PAGE after CONNECT")
        form.addRow("", self.chk_auto_cal_page)

        cal_row = QHBoxLayout()
        self.chk_cal_ecu = QCheckBox("ECU")
        self.chk_cal_ecu.setChecked(True)
        self.chk_cal_xcp = QCheckBox("XCP")
        self.chk_cal_xcp.setChecked(True)
        self.spn_cal_segment = QSpinBox()
        self.spn_cal_segment.setRange(0, 255)
        self.spn_cal_page = QSpinBox()
        self.spn_cal_page.setRange(0, 255)
        cal_row.addWidget(self.chk_cal_ecu)
        cal_row.addWidget(self.chk_cal_xcp)
        cal_row.addWidget(QLabel("Seg"))
        cal_row.addWidget(self.spn_cal_segment)
        cal_row.addWidget(QLabel("Page"))
        cal_row.addWidget(self.spn_cal_page)
        cal_row.addStretch()
        form.addRow("Cal page", cal_row)
        layout.addLayout(form, stretch=1)

        btns = QVBoxLayout()
        self.btn_unlock_now = QPushButton("Unlock now")
        self.btn_unlock_now.clicked.connect(self.unlock_clicked.emit)
        self.btn_set_cal_page = QPushButton("SET_CAL_PAGE")
        self.btn_set_cal_page.clicked.connect(self.set_cal_page_clicked.emit)
        self.btn_get_cal_page = QPushButton("GET_CAL_PAGE")
        self.btn_get_cal_page.clicked.connect(self.get_cal_page_clicked.emit)
        btns.addWidget(self.btn_unlock_now)
        btns.addWidget(self.btn_set_cal_page)
        btns.addWidget(self.btn_get_cal_page)
        btns.addStretch()
        layout.addLayout(btns)

    def set_connected(self, connected: bool) -> None:
        self.btn_unlock_now.setEnabled(connected)
        self.btn_set_cal_page.setEnabled(connected)
        self.btn_get_cal_page.setEnabled(connected)
        self.edt_seedkey.setEnabled(not connected)
        self.edt_dll_symbol.setEnabled(not connected)

    def unlock_resource_mask(self) -> int:
        mask = 0
        if self.chk_unlock_cal.isChecked():
            mask |= proto.Resource.CAL_PAG
        if self.chk_unlock_daq.isChecked():
            mask |= proto.Resource.DAQ
        if self.chk_unlock_stim.isChecked():
            mask |= proto.Resource.STIM
        if self.chk_unlock_pgm.isChecked():
            mask |= proto.Resource.PGM
        return mask

    def cal_page_mode(self) -> int:
        mode = 0
        if self.chk_cal_ecu.isChecked():
            mode |= proto.CAL_PAGE_MODE_ECU
        if self.chk_cal_xcp.isChecked():
            mode |= proto.CAL_PAGE_MODE_XCP
        return mode or (proto.CAL_PAGE_MODE_ECU | proto.CAL_PAGE_MODE_XCP)

    def load_seedkey_provider(self) -> SeedKeyProvider | None:
        path = self.edt_seedkey.text().strip()
        if not path:
            return None
        symbol = self.edt_dll_symbol.text().strip() or "XCP_ComputeKeyFromSeed"
        return load_seedkey(path, dll_symbol=symbol)

    def session_security_kwargs(self) -> dict:
        """Build kwargs for XcpSessionConfig; may raise SeedKeyError if auto_unlock."""
        provider = None
        try:
            provider = self.load_seedkey_provider()
        except SeedKeyError:
            if self.chk_auto_unlock.isChecked():
                raise
            provider = None
        return {
            "auto_unlock": self.chk_auto_unlock.isChecked() and provider is not None,
            "unlock_resources": self.unlock_resource_mask(),
            "seed_key": provider,
            "auto_set_cal_page": self.chk_auto_cal_page.isChecked(),
            "cal_page_mode": self.cal_page_mode(),
            "cal_segment": self.spn_cal_segment.value(),
            "cal_page": self.spn_cal_page.value(),
        }

    def collect(self) -> SecuritySettings:
        return SecuritySettings(
            seedkey_path=self.edt_seedkey.text().strip(),
            dll_symbol=self.edt_dll_symbol.text().strip() or "XCP_ComputeKeyFromSeed",
            auto_unlock=self.chk_auto_unlock.isChecked(),
            unlock_cal=self.chk_unlock_cal.isChecked(),
            unlock_daq=self.chk_unlock_daq.isChecked(),
            unlock_stim=self.chk_unlock_stim.isChecked(),
            unlock_pgm=self.chk_unlock_pgm.isChecked(),
            auto_set_cal_page=self.chk_auto_cal_page.isChecked(),
            cal_mode_ecu=self.chk_cal_ecu.isChecked(),
            cal_mode_xcp=self.chk_cal_xcp.isChecked(),
            cal_segment=self.spn_cal_segment.value(),
            cal_page=self.spn_cal_page.value(),
        )

    def apply(self, sec: SecuritySettings) -> None:
        self.edt_seedkey.setText(sec.seedkey_path)
        self.edt_dll_symbol.setText(sec.dll_symbol or "XCP_ComputeKeyFromSeed")
        self.chk_auto_unlock.setChecked(sec.auto_unlock)
        self.chk_unlock_cal.setChecked(sec.unlock_cal)
        self.chk_unlock_daq.setChecked(sec.unlock_daq)
        self.chk_unlock_stim.setChecked(sec.unlock_stim)
        self.chk_unlock_pgm.setChecked(sec.unlock_pgm)
        self.chk_auto_cal_page.setChecked(sec.auto_set_cal_page)
        self.chk_cal_ecu.setChecked(sec.cal_mode_ecu)
        self.chk_cal_xcp.setChecked(sec.cal_mode_xcp)
        self.spn_cal_segment.setValue(sec.cal_segment)
        self.spn_cal_page.setValue(sec.cal_page)

    def set_cal_page_value(self, page: int) -> None:
        self.spn_cal_page.setValue(page)

    def _browse_seedkey(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Seed&Key plugin",
            "",
            "Seed&Key (*.py *.dll);;Python (*.py);;DLL (*.dll);;All (*.*)",
        )
        if path:
            self.edt_seedkey.setText(path)
