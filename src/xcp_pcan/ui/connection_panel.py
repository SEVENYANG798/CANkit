"""Connection settings panel (tabbed; hosted in Connect dialog)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.can.transport import (
    HardwareBackend,
    VectorDeviceInfo,
    discover_vector_devices,
    list_pcan_channels,
    list_vector_channels_for_serial,
)
from xcp_pcan.can_id_presets import XCP_PRESETS
from xcp_pcan.project import ConnectionSettings, ResilienceSettings
from xcp_pcan.symbols.types import XcpTransportHints
from xcp_pcan.ui.id_preset_bar import IdPresetBar


def parse_can_id(text: str) -> int:
    text = text.strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    return int(text, 10)


class ConnectionPanel(QWidget):
    """Hardware / bitrate / XCP IDs / session / security host — no Connect buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vector_devices: list[VectorDeviceInfo] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        root.addWidget(tabs)

        # --- CAN Setup ---------------------------------------------------------
        setup = QWidget()
        setup_form = QFormLayout(setup)

        self.cmb_hw = QComboBox()
        self.cmb_hw.addItem("PCAN (PEAK)", HardwareBackend.PCAN.value)
        self.cmb_hw.addItem("Vector (VN / XL Driver)", HardwareBackend.VECTOR.value)
        self.cmb_hw.currentIndexChanged.connect(self._on_hardware_changed)
        setup_form.addRow("Hardware", self.cmb_hw)

        device_row = QHBoxLayout()
        device_row.setContentsMargins(0, 0, 0, 0)
        self.cmb_device = QComboBox()
        self.cmb_device.setToolTip(
            "Enumerated Vector interfaces (VN1630 / VN1650 / …). "
            "Assign app channels in Vector Hardware Config only needed for fallback."
        )
        self.cmb_device.currentIndexChanged.connect(self._on_device_changed)
        self.btn_refresh_hw = QPushButton("↻")
        self.btn_refresh_hw.setFixedWidth(28)
        self.btn_refresh_hw.setToolTip("Refresh Vector hardware list")
        self.btn_refresh_hw.clicked.connect(self._refresh_vector_hardware)
        device_row.addWidget(self.cmb_device, stretch=1)
        device_row.addWidget(self.btn_refresh_hw)
        self._lbl_device = QLabel("Device")
        setup_form.addRow(self._lbl_device, device_row)

        self.cmb_channel = QComboBox()
        setup_form.addRow("Channel", self.cmb_channel)

        self.spn_bitrate = QSpinBox()
        self.spn_bitrate.setRange(10_000, 1_000_000)
        self.spn_bitrate.setSingleStep(10_000)
        self.spn_bitrate.setValue(500_000)
        self.spn_bitrate.setToolTip("Nominal (arbitration) bitrate — match PCAN-View / ECU.")
        setup_form.addRow("Nom bitrate", self.spn_bitrate)

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        self.chk_fd = QCheckBox("CAN FD")
        self.chk_fd.setToolTip(
            "Must match the bus. FD traffic on a Classic session shows as ERR/stuff frames."
        )
        self.chk_fd.toggled.connect(self._on_fd_toggled)
        mode_row.addWidget(self.chk_fd)
        self.spn_data_bitrate = QSpinBox()
        self.spn_data_bitrate.setRange(100_000, 8_000_000)
        self.spn_data_bitrate.setSingleStep(100_000)
        self.spn_data_bitrate.setValue(2_000_000)
        self.spn_data_bitrate.setToolTip("CAN FD data-phase bitrate (e.g. 2 Mbit/s).")
        mode_row.addWidget(QLabel("Data"))
        mode_row.addWidget(self.spn_data_bitrate, stretch=1)
        setup_form.addRow("Mode", mode_row)

        default_xcp = XCP_PRESETS[0]
        self.edt_tx = QLineEdit(default_xcp.tx)
        self.edt_rx = QLineEdit(default_xcp.rx)
        self.edt_tx.setToolTip("XCP master → slave CAN ID")
        self.edt_rx.setToolTip("XCP slave → master CAN ID")
        self._xcp_preset_bar = IdPresetBar(
            kind="xcp",
            builtin=XCP_PRESETS,
            edt_tx=self.edt_tx,
            edt_rx=self.edt_rx,
            parent=self,
        )
        setup_form.addRow("XCP target", self._xcp_preset_bar)
        setup_form.addRow("Master TX ID", self.edt_tx)
        setup_form.addRow("Slave RX ID", self.edt_rx)
        self._xcp_preset_bar.sync_from_fields()
        tabs.addTab(setup, "CAN Setup")

        # --- Session (timing + resilience) -------------------------------------
        session = QWidget()
        session_row = QHBoxLayout(session)
        session_row.setContentsMargins(8, 8, 8, 8)

        bus_box = QGroupBox("Bus / timing")
        bus_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        bus_form = QFormLayout(bus_box)
        self.spn_fclock = QSpinBox()
        self.spn_fclock.setRange(16, 80)
        self.spn_fclock.setValue(80)
        self.spn_fclock.setSuffix(" MHz")
        self.spn_fclock.setToolTip("Used for PCAN CAN FD bit timing only.")
        bus_form.addRow("FD f_clock (PCAN)", self.spn_fclock)

        self.spn_timeout = QSpinBox()
        self.spn_timeout.setRange(50, 10_000)
        self.spn_timeout.setValue(1000)
        self.spn_timeout.setSuffix(" ms")
        bus_form.addRow("CTO timeout", self.spn_timeout)

        self.spn_cto_retries = QSpinBox()
        self.spn_cto_retries.setRange(0, 10)
        self.spn_cto_retries.setValue(2)
        self.spn_cto_retries.setToolTip("Extra CTO attempts after the first (total = 1 + retries)")
        bus_form.addRow("CTO retries", self.spn_cto_retries)

        self.spn_cto_retry_delay = QSpinBox()
        self.spn_cto_retry_delay.setRange(0, 2000)
        self.spn_cto_retry_delay.setValue(50)
        self.spn_cto_retry_delay.setSuffix(" ms")
        bus_form.addRow("CTO retry delay", self.spn_cto_retry_delay)
        session_row.addWidget(bus_box)

        res_box = QGroupBox("Session resilience")
        res_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        res_form = QFormLayout(res_box)
        self.chk_reconnect = QCheckBox("Auto reconnect")
        self.chk_reconnect.setChecked(True)
        res_form.addRow("", self.chk_reconnect)

        self.spn_reconnect_attempts = QSpinBox()
        self.spn_reconnect_attempts.setRange(1, 20)
        self.spn_reconnect_attempts.setValue(5)
        res_form.addRow("Reconnect attempts", self.spn_reconnect_attempts)

        self.spn_reconnect_backoff = QSpinBox()
        self.spn_reconnect_backoff.setRange(50, 10_000)
        self.spn_reconnect_backoff.setValue(400)
        self.spn_reconnect_backoff.setSuffix(" ms")
        self.spn_reconnect_backoff.setToolTip("Base backoff; attempt n sleeps n × this value")
        res_form.addRow("Reconnect backoff", self.spn_reconnect_backoff)

        self.spn_daq_dto_timeout = QSpinBox()
        self.spn_daq_dto_timeout.setRange(500, 60_000)
        self.spn_daq_dto_timeout.setValue(2000)
        self.spn_daq_dto_timeout.setSuffix(" ms")
        self.spn_daq_dto_timeout.setToolTip(
            "If no DAQ DTO arrives for this long while running, rebuild the DAQ list"
        )
        res_form.addRow("DAQ DTO silence", self.spn_daq_dto_timeout)
        session_row.addWidget(res_box)
        tabs.addTab(session, "Session")

        # --- Security host (filled by MainWindow) ------------------------------
        security_page = QWidget()
        self.advanced_extra = QVBoxLayout(security_page)
        self.advanced_extra.setContentsMargins(8, 8, 8, 8)
        tabs.addTab(security_page, "Security")

        self._populate_channels()
        self._on_fd_toggled(False)
        self._on_hardware_changed()

    def selected_backend(self) -> HardwareBackend:
        return HardwareBackend(self.cmb_hw.currentData())

    def selected_vector_serial(self) -> int | None:
        if self.selected_backend() != HardwareBackend.VECTOR:
            return None
        data = self.cmb_device.currentData()
        return int(data) if data is not None else None

    def _refresh_vector_hardware(self) -> None:
        if self.selected_backend() != HardwareBackend.VECTOR:
            return
        prev_serial = self.selected_vector_serial()
        prev_ch = self.cmb_channel.currentData()
        self._populate_vector_devices()
        if prev_serial is not None:
            self._set_combo_data(self.cmb_device, prev_serial)
        self._populate_channels()
        if prev_ch is not None:
            self._set_combo_data(self.cmb_channel, prev_ch)

    def _populate_vector_devices(self) -> None:
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        self._vector_devices = discover_vector_devices()
        if self._vector_devices:
            for dev in self._vector_devices:
                self.cmb_device.addItem(dev.label, dev.serial)
        else:
            self.cmb_device.addItem("Not detected", None)
        self.cmb_device.blockSignals(False)

    def _populate_channels(self) -> None:
        self.cmb_channel.clear()
        if self.selected_backend() == HardwareBackend.VECTOR:
            serial = self.selected_vector_serial()
            for label, value in list_vector_channels_for_serial(
                serial, devices=self._vector_devices
            ):
                self.cmb_channel.addItem(label, value)
        else:
            for label, value in list_pcan_channels():
                self.cmb_channel.addItem(label, value)

    def _on_device_changed(self) -> None:
        if self.selected_backend() == HardwareBackend.VECTOR:
            self._populate_channels()

    def _on_hardware_changed(self) -> None:
        is_vector = self.selected_backend() == HardwareBackend.VECTOR
        self.cmb_device.setVisible(is_vector)
        self._lbl_device.setVisible(is_vector)
        self.btn_refresh_hw.setVisible(is_vector)
        self.spn_fclock.setEnabled((not is_vector) and self.chk_fd.isChecked())
        if is_vector:
            self._populate_vector_devices()
        self._populate_channels()

    def _on_fd_toggled(self, enabled: bool) -> None:
        self.spn_data_bitrate.setEnabled(enabled)
        is_pcan = self.selected_backend() == HardwareBackend.PCAN
        self.spn_fclock.setEnabled(enabled and is_pcan)

    def set_connected(self, connected: bool) -> None:
        self.cmb_hw.setEnabled(not connected)
        self.cmb_device.setEnabled(not connected)
        self.btn_refresh_hw.setEnabled(not connected)
        self.cmb_channel.setEnabled(not connected)
        self.spn_bitrate.setEnabled(not connected)
        self.chk_fd.setEnabled(not connected)
        fd_on = self.chk_fd.isChecked()
        self.spn_data_bitrate.setEnabled((not connected) and fd_on)
        self.spn_fclock.setEnabled(
            (not connected)
            and fd_on
            and self.selected_backend() == HardwareBackend.PCAN
        )
        self.edt_tx.setEnabled(not connected)
        self.edt_rx.setEnabled(not connected)
        for w in (
            self.spn_cto_retries,
            self.spn_cto_retry_delay,
            self.chk_reconnect,
            self.spn_reconnect_attempts,
            self.spn_reconnect_backoff,
            self.spn_timeout,
        ):
            w.setEnabled(not connected)
        self.spn_daq_dto_timeout.setEnabled(True)

    def collect_connection(self) -> ConnectionSettings:
        channel = self.cmb_channel.currentData()
        return ConnectionSettings(
            hardware=self.selected_backend().value,
            channel=channel if channel is not None else "PCAN_USBBUS1",
            vector_app_name="CANkit",
            vector_serial=self.selected_vector_serial(),
            bitrate=self.spn_bitrate.value(),
            fd=self.chk_fd.isChecked(),
            data_bitrate=self.spn_data_bitrate.value(),
            f_clock_mhz=self.spn_fclock.value(),
            master_can_id=self.edt_tx.text().strip() or XCP_PRESETS[0].tx,
            slave_can_id=self.edt_rx.text().strip() or XCP_PRESETS[0].rx,
            timeout_ms=self.spn_timeout.value(),
        )

    def collect_resilience(self) -> ResilienceSettings:
        return ResilienceSettings(
            cto_retries=self.spn_cto_retries.value(),
            cto_retry_delay_ms=self.spn_cto_retry_delay.value(),
            reconnect_enabled=self.chk_reconnect.isChecked(),
            reconnect_max_attempts=self.spn_reconnect_attempts.value(),
            reconnect_backoff_ms=self.spn_reconnect_backoff.value(),
            daq_dto_timeout_ms=self.spn_daq_dto_timeout.value(),
        )

    def apply_connection(self, conn: ConnectionSettings) -> None:
        self._set_combo_data(self.cmb_hw, conn.hardware)
        self._on_hardware_changed()
        if conn.vector_serial is not None:
            self._set_combo_data(self.cmb_device, conn.vector_serial)
            self._populate_channels()
        self._set_combo_data(self.cmb_channel, conn.channel)
        self.spn_bitrate.setValue(conn.bitrate)
        self.chk_fd.setChecked(conn.fd)
        self.spn_data_bitrate.setValue(conn.data_bitrate)
        self.spn_fclock.setValue(conn.f_clock_mhz)
        self.edt_tx.setText(conn.master_can_id)
        self.edt_rx.setText(conn.slave_can_id)
        self._xcp_preset_bar.sync_from_fields()
        self.spn_timeout.setValue(conn.timeout_ms)
        self._on_fd_toggled(conn.fd)

    def apply_resilience(self, res: ResilienceSettings) -> None:
        self.spn_cto_retries.setValue(res.cto_retries)
        self.spn_cto_retry_delay.setValue(res.cto_retry_delay_ms)
        self.chk_reconnect.setChecked(res.reconnect_enabled)
        self.spn_reconnect_attempts.setValue(res.reconnect_max_attempts)
        self.spn_reconnect_backoff.setValue(res.reconnect_backoff_ms)
        self.spn_daq_dto_timeout.setValue(res.daq_dto_timeout_ms)

    def apply_transport_hints(self, hints: XcpTransportHints) -> list[str]:
        """Apply baud/FD from A2L only — never overwrite CAN IDs."""
        applied: list[str] = []
        if hints.bitrate is not None:
            self.spn_bitrate.setValue(int(hints.bitrate))
            applied.append(f"nom={hints.bitrate}")
        if hints.fd is True:
            self.chk_fd.setChecked(True)
            applied.append("FD=on")
        if hints.data_bitrate is not None:
            self.spn_data_bitrate.setValue(int(hints.data_bitrate))
            applied.append(f"data={hints.data_bitrate}")
        return applied

    @staticmethod
    def _set_combo_data(combo: QComboBox, data) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data or str(combo.itemData(i)) == str(data):
                combo.setCurrentIndex(i)
                return
