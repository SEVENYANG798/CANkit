"""Connection panel: essentials always visible; rest under Advanced."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
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
from xcp_pcan.ui.collapsible import CollapsibleSection
from xcp_pcan.ui.id_preset_bar import IdPresetBar


def parse_can_id(text: str) -> int:
    text = text.strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    return int(text, 10)


class ConnectionPanel(QGroupBox):
    """Hardware / channel / CAN IDs + Connect; advanced under fold."""

    connect_clicked = Signal()
    disconnect_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Connection", parent)
        root = QVBoxLayout(self)
        root.setSpacing(6)
        self._vector_devices: list[VectorDeviceInfo] = []

        # --- Always visible essentials -----------------------------------------
        basic = QHBoxLayout()
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)

        self.cmb_hw = QComboBox()
        self.cmb_hw.addItem("PCAN (PEAK)", HardwareBackend.PCAN.value)
        self.cmb_hw.addItem("Vector (VN / XL Driver)", HardwareBackend.VECTOR.value)
        self.cmb_hw.currentIndexChanged.connect(self._on_hardware_changed)
        form.addRow("Hardware", self.cmb_hw)

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
        form.addRow(self._lbl_device, device_row)

        self.cmb_channel = QComboBox()
        form.addRow("Channel", self.cmb_channel)

        # Bus mode on the main panel — Classic vs FD is easy to miss under Advanced,
        # and opening Classic on a CAN FD bus yields only stuff/bit error frames.
        self.spn_bitrate = QSpinBox()
        self.spn_bitrate.setRange(10_000, 1_000_000)
        self.spn_bitrate.setSingleStep(10_000)
        self.spn_bitrate.setValue(500_000)
        self.spn_bitrate.setToolTip("Nominal (arbitration) bitrate — match PCAN-View / ECU.")
        form.addRow("Nom bitrate", self.spn_bitrate)

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
        form.addRow("Mode", mode_row)

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
        form.addRow("XCP target", self._xcp_preset_bar)
        form.addRow("Master TX ID", self.edt_tx)
        form.addRow("Slave RX ID", self.edt_rx)
        self._xcp_preset_bar.sync_from_fields()
        basic.addLayout(form, stretch=1)

        btns = QVBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_clicked.emit)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self.disconnect_clicked.emit)
        btns.addWidget(self.btn_connect)
        btns.addWidget(self.btn_disconnect)
        btns.addStretch()
        basic.addLayout(btns)
        root.addLayout(basic)

        # --- Collapsible advanced (bus timing + resilience + optional extras) --
        self.advanced = CollapsibleSection("Advanced", expanded=False)
        adv_row = QHBoxLayout()

        bus_box = QGroupBox("Bus / timing")
        bus_form = QFormLayout(bus_box)
        self.edt_app_name = QLineEdit("CANkit")
        self.edt_app_name.setToolTip(
            "Vector application name — assign channels in Vector Hardware Config."
        )
        bus_form.addRow("Vector app name", self.edt_app_name)

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
        adv_row.addWidget(bus_box)

        res_box = QGroupBox("Session resilience")
        res_form = QFormLayout(res_box)
        self.spn_cto_retries = QSpinBox()
        self.spn_cto_retries.setRange(0, 10)
        self.spn_cto_retries.setValue(2)
        self.spn_cto_retries.setToolTip("Extra CTO attempts after the first (total = 1 + retries)")
        res_form.addRow("CTO retries", self.spn_cto_retries)

        self.spn_cto_retry_delay = QSpinBox()
        self.spn_cto_retry_delay.setRange(0, 2000)
        self.spn_cto_retry_delay.setValue(50)
        self.spn_cto_retry_delay.setSuffix(" ms")
        res_form.addRow("CTO retry delay", self.spn_cto_retry_delay)

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
        adv_row.addWidget(res_box)

        bus_host = QWidget()
        bus_host.setLayout(adv_row)

        # Placeholder host for Security panel (filled by MainWindow)
        self.advanced_extra = QVBoxLayout()
        self.advanced_extra.setContentsMargins(0, 0, 0, 0)
        extra_wrap = QWidget()
        extra_wrap.setLayout(self.advanced_extra)

        # Bound Advanced body height and scroll — never push/cover Symbols/Watch.
        adv_content = QWidget()
        adv_content_layout = QVBoxLayout(adv_content)
        adv_content_layout.setContentsMargins(0, 0, 0, 0)
        adv_content_layout.setSpacing(6)
        adv_content_layout.addWidget(bus_host)
        adv_content_layout.addWidget(extra_wrap)

        self._advanced_scroll = QScrollArea()
        self._advanced_scroll.setWidgetResizable(True)
        self._advanced_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._advanced_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._advanced_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._advanced_scroll.setWidget(adv_content)
        # Fixed viewport height so expanding Advanced never covers Symbols/Watch.
        # Content scrolls inside; header (Advanced ▶/▼) always stays visible.
        self._advanced_scroll.setFixedHeight(240)
        self.advanced.add_widget(self._advanced_scroll)
        root.addWidget(self.advanced)

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
        self.edt_app_name.setEnabled(is_vector)
        self.spn_fclock.setEnabled((not is_vector) and self.chk_fd.isChecked())
        if is_vector:
            self._populate_vector_devices()
        self._populate_channels()

    def _on_fd_toggled(self, enabled: bool) -> None:
        self.spn_data_bitrate.setEnabled(enabled)
        is_pcan = self.selected_backend() == HardwareBackend.PCAN
        self.spn_fclock.setEnabled(enabled and is_pcan)

    def set_connected(self, connected: bool) -> None:
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.cmb_hw.setEnabled(not connected)
        self.cmb_device.setEnabled(not connected)
        self.btn_refresh_hw.setEnabled(not connected)
        self.cmb_channel.setEnabled(not connected)
        self.edt_app_name.setEnabled(
            (not connected) and self.selected_backend() == HardwareBackend.VECTOR
        )
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
            vector_app_name=self.edt_app_name.text().strip() or "CANkit",
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
        self.edt_app_name.setText(conn.vector_app_name)
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
