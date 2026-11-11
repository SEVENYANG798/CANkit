"""MainWindow mixin: connection, session helpers, Seed&Key / cal page."""

from __future__ import annotations

import logging

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from xcp_pcan.can.transport import HardwareBackend
from xcp_pcan.symbols.types import XcpTransportHints
from xcp_pcan.ui.app_log import LogCategory
from xcp_pcan.ui.connect_worker import ConnectWorker
from xcp_pcan.ui.connection_panel import parse_can_id
from xcp_pcan.ui.window_util import raise_window
from xcp_pcan.ui.worker_util import (
    close_progress,
    make_progress_dialog,
    mark_progress_cancelling,
)
from xcp_pcan.xcp.daq import DaqEngine
from xcp_pcan.xcp.master import XcpMaster, XcpSessionConfig
from xcp_pcan.xcp.poller import Poller
from xcp_pcan.xcp.seedkey import SeedKeyError

logger = logging.getLogger(__name__)


class ConnectMixin:
    # --- Connected state / session helpers -------------------------------------

    def _set_connected_ui(self, connected: bool) -> None:
        """Legacy helper: fully up or fully down."""
        self._set_link_ui(bus_open=connected, xcp=connected)

    def _refresh_link_ui(self) -> None:
        self._set_link_ui(
            bus_open=self._transport.is_open,
            xcp=self._master.is_connected,
        )

    def _set_link_ui(self, *, bus_open: bool, xcp: bool) -> None:
        """Bus open enables Trace / Poll / DAQ buttons; XCP starts on demand."""
        _ = xcp  # tracked for status; Poll/DAQ/write ensure XCP when clicked
        self.connection.set_connected(bus_open)
        self.security.set_connected(bus_open)
        busy = self._connect_busy()
        self._act_can_connect.setEnabled(not busy)
        self._act_can_disconnect.setEnabled(bus_open or busy)
        if self._trace_viewer is not None:
            self._trace_viewer.sync_toolbar_actions(
                bus_open=bus_open, busy=busy
            )
        self.watch.set_connected(
            bus_open,
            poll_running=self._poller.is_running,
            daq_running=self._daq.is_running,
        )

    @Slot()
    def _open_connect_dialog(self) -> None:
        """CAN → Connect... — configure tabs, then open the bus (if not already)."""
        from PySide6.QtCore import QTimer

        from xcp_pcan.ui.connect_dialog import ConnectDialog

        if self._connect_busy():
            return
        if getattr(self, "_connect_dialog_open", False):
            return
        bus_open = self._transport.is_open
        # Defer past InstantPopup menu teardown so the dialog does not flash away.
        def _show() -> None:
            if getattr(self, "_connect_dialog_open", False):
                return
            self._connect_dialog_open = True
            try:
                dlg = ConnectDialog(self.connection, bus_open=bus_open, parent=self)
                dlg.exec()
                self.connection.setParent(self)
                self.connection.hide()
                if dlg.should_connect:
                    self._connect()
            finally:
                self._connect_dialog_open = False

        QTimer.singleShot(0, _show)

    def _rebuild_session_helpers(self) -> None:
        res = self.connection.collect_resilience()
        self._poller = Poller(
            self._master,
            on_sample=lambda s: self._bridge.sample.emit(s),
            on_error=lambda m: self._bridge.error.emit(m),
        )
        self._daq = DaqEngine(
            self._master,
            on_sample=lambda s: self._bridge.daq_sample.emit(s),
            on_error=lambda m: self._bridge.error.emit(m),
            dto_timeout_s=res.daq_dto_timeout_ms / 1000.0,
        )

    def _apply_daq_timeout_from_ui(self) -> None:
        res = self.connection.collect_resilience()
        self._daq.set_dto_timeout_s(res.daq_dto_timeout_ms / 1000.0)

    # --- Connect / disconnect / security ---------------------------------------

    def _connect_busy(self) -> bool:
        return self._connect_worker is not None and self._connect_worker.isRunning()

    @Slot()
    def _connect_ui_parent(self) -> QWidget:
        """Prefer the tool window that started Connect so progress stays there."""
        front = getattr(self, "_connect_keep_front", None)
        if front is not None:
            return front
        parent = getattr(self, "_dialog_parent", None)
        return parent() if callable(parent) else self

    def _restore_connect_front(self) -> None:
        front = getattr(self, "_connect_keep_front", None)
        self._connect_keep_front = None
        if front is not None:
            raise_window(front)

    def _connect(self, keep_front: QWidget | None = None) -> None:
        """Open CAN bus. ``keep_front`` = Trace/Diag window when Connect started from there."""
        self._connect_keep_front = keep_front
        ui = self._connect_ui_parent()
        if self._connect_busy():
            QMessageBox.information(ui, "Busy", "Connect is already in progress.")
            self._restore_connect_front()
            return
        if self._transport.is_open:
            QMessageBox.information(
                ui,
                "Already open",
                "CAN bus is already open. Disconnect first, then Connect again.",
            )
            self._restore_connect_front()
            return
        try:
            conn = self.connection.collect_connection()
            res = self.connection.collect_resilience()
            backend = self.connection.selected_backend()
            channel = self.connection.cmb_channel.currentData()
            tx = parse_can_id(conn.master_can_id)
            rx = parse_can_id(conn.slave_can_id)
            security = self.security.session_security_kwargs()
        except ValueError:
            QMessageBox.warning(ui, "Invalid input", "Check CAN IDs (hex like 0x667).")
            self._restore_connect_front()
            return
        except SeedKeyError as exc:
            QMessageBox.critical(ui, "Seed&Key", str(exc))
            self._restore_connect_front()
            return

        self._master = XcpMaster(
            self._transport,
            XcpSessionConfig(
                master_can_id=tx,
                slave_can_id=rx,
                timeout_s=conn.timeout_ms / 1000.0,
                fd=conn.fd,
                cto_retries=res.cto_retries,
                cto_retry_delay_s=res.cto_retry_delay_ms / 1000.0,
                reconnect_enabled=res.reconnect_enabled,
                reconnect_max_attempts=res.reconnect_max_attempts,
                reconnect_backoff_s=res.reconnect_backoff_ms / 1000.0,
                **security,
            ),
        )
        self._rebuild_session_helpers()

        hw = backend.value.upper()
        app_name = conn.vector_app_name
        serial = conn.vector_serial
        vec_extra = ""
        if backend == HardwareBackend.VECTOR:
            vec_extra = f" app={app_name}"
            if serial is not None:
                vec_extra += f" SN={serial}"
        mode = "FD" if conn.fd else "Classic"
        if conn.fd:
            self._append_log(
                f"Opening {hw} ch={channel!r} CAN FD nom={conn.bitrate} "
                f"data={conn.data_bitrate}"
                + (f" f_clock={conn.f_clock_mhz}MHz" if backend == HardwareBackend.PCAN else "")
                + vec_extra
                + " …"
            )
        else:
            self._append_log(
                f"Opening {hw} ch={channel!r} Classic CAN @ {conn.bitrate}{vec_extra} …"
            )

        self._connect_meta = {
            "kind": "bus",
            "hw": hw,
            "mode": mode,
            "channel": channel,
            "tx": tx,
            "rx": rx,
            "cto_retries": res.cto_retries,
            "reconnect": res.reconnect_enabled,
        }
        self._pending_after_xcp = None
        # Lock hardware controls while connecting (same as connected).
        self.connection.set_connected(True)
        self._act_can_connect.setEnabled(False)
        self._act_can_disconnect.setEnabled(True)
        if self._trace_viewer is not None:
            self._trace_viewer.sync_toolbar_actions(bus_open=False, busy=True)
        progress = make_progress_dialog(
            ui,
            title="Connect",
            label="Opening CAN bus…",
            cancel_text="Cancel",
            maximum=0,
            tool_window=keep_front is not None,
        )
        progress.canceled.connect(self._cancel_connect)
        self._connect_progress = progress
        self.statusBar().showMessage("Opening CAN bus…")

        worker = ConnectWorker(
            self._transport,
            self._master,
            {
                "backend": backend,
                "channel": channel,
                "bitrate": conn.bitrate,
                "fd": conn.fd,
                "data_bitrate": conn.data_bitrate,
                "f_clock_mhz": conn.f_clock_mhz,
                "app_name": app_name,
                "serial": serial,
            },
            mode="bus",
            parent=self,
        )
        worker.progress.connect(self._on_connect_progress)
        worker.finished_ok.connect(self._on_connect_ok)
        worker.failed.connect(self._on_connect_failed)
        worker.cancelled.connect(self._on_connect_cancelled)
        worker.finished.connect(self._on_connect_thread_finished)
        self._connect_worker = worker
        worker.start()

    @Slot()
    def _cancel_connect(self) -> None:
        if self._connect_worker is not None and self._connect_worker.isRunning():
            self._append_log("Connect cancel requested")
            mark_progress_cancelling(self._connect_progress)
            self._connect_worker.abort()

    @Slot(str)
    def _on_connect_progress(self, message: str) -> None:
        if self._connect_progress is not None:
            self._connect_progress.setLabelText(message)
        self.statusBar().showMessage(message)

    def _ensure_xcp_then(self, action) -> None:
        """Run ``action`` after XCP is up; Connect (bus) must already have succeeded."""
        if self._master.is_connected:
            action()
            return
        if not self._transport.is_open:
            QMessageBox.information(
                self,
                "Not connected",
                "Open the CAN bus first (Connect).\n"
                "XCP starts automatically when you Start poll / Start DAQ.",
            )
            return
        if self._connect_busy():
            QMessageBox.information(self, "Busy", "A connect operation is already in progress.")
            return
        self._pending_after_xcp = action
        self._connect_meta = {"kind": "xcp"}
        self._act_can_connect.setEnabled(False)
        self._act_can_disconnect.setEnabled(True)
        if self._trace_viewer is not None:
            self._trace_viewer.sync_toolbar_actions(bus_open=True, busy=True)
        progress = make_progress_dialog(
            self, title="XCP", label="XCP CONNECT…", cancel_text="Cancel", maximum=0
        )
        progress.canceled.connect(self._cancel_connect)
        self._connect_progress = progress
        self.statusBar().showMessage("XCP CONNECT…")
        self._append_log("XCP CONNECT…")
        worker = ConnectWorker(
            self._transport,
            self._master,
            mode="xcp",
            parent=self,
        )
        worker.progress.connect(self._on_connect_progress)
        worker.finished_ok.connect(self._on_connect_ok)
        worker.failed.connect(self._on_connect_failed)
        worker.cancelled.connect(self._on_connect_cancelled)
        worker.finished.connect(self._on_connect_thread_finished)
        self._connect_worker = worker
        worker.start()

    @Slot(object)
    def _on_connect_ok(self, info: object) -> None:
        if self._closing:
            return
        meta = self._connect_meta or {}
        kind = meta.get("kind", "bus")
        if kind == "bus" or info is None:
            hw = meta.get("hw", "?")
            mode = meta.get("mode", "?")
            channel = meta.get("channel", "?")
            tx = int(meta.get("tx", 0))
            rx = int(meta.get("rx", 0))
            self._append_log(f"CAN bus open [{hw}/{mode}] ch={channel!r}")
            self._set_link_ui(bus_open=True, xcp=False)
            self.statusBar().showMessage(
                f"Bus open [{hw}/{mode}]  TX=0x{tx:X} RX=0x{rx:X}  ch={channel}  "
                "(Monitor OK; XCP on Start poll/DAQ)"
            )
            if self._trace_viewer is not None:
                self._trace_viewer.set_transport(self._transport)
                self._trace_viewer.notify_bus_open()
            if self._diag_console is not None:
                self._diag_console.set_transport(self._transport)
                self._diag_console.notify_bus_open()
            self._restore_connect_front()
            return
        max_cto = getattr(info, "max_cto", "?")
        max_dto = getattr(info, "max_dto", "?")
        intel = getattr(info, "byte_order_intel", "?")
        resource = getattr(info, "available_resources", 0)
        self._append_log(
            f"XCP CONNECT OK — max_cto={max_cto} max_dto={max_dto} intel={intel} "
            f"resource=0x{int(resource):02X}"
        )
        if self._master.unlocked_mask:
            self._append_log(
                f"Auto-unlocked resources 0x{self._master.unlocked_mask:02X}",
                category=LogCategory.XCP,
            )
        self._set_link_ui(bus_open=True, xcp=True)
        self.statusBar().showMessage("XCP connected")
        pending = self._pending_after_xcp
        self._pending_after_xcp = None
        if pending is not None:
            try:
                pending()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Post-XCP action failed")
                QMessageBox.critical(self, "Failed", str(exc))

    @Slot(str)
    def _on_connect_failed(self, message: str) -> None:
        if self._closing:
            return
        meta = self._connect_meta or {}
        kind = meta.get("kind", "bus")
        self._pending_after_xcp = None
        logger.error("Connect failed (%s): %s", kind, message)
        if kind == "xcp":
            try:
                self._master.close_listener()
            except Exception:  # noqa: BLE001
                logger.debug("close_listener failed", exc_info=True)
            self._set_link_ui(bus_open=True, xcp=False)
            self._append_log(f"ERROR XCP CONNECT: {message}")
            QMessageBox.critical(self, "XCP connect failed", message)
            self.statusBar().showMessage("XCP connect failed (bus still open)")
            return
        self._safe_shutdown_bus()
        self._set_link_ui(bus_open=False, xcp=False)
        self._append_log(f"ERROR: {message}")
        if self._trace_viewer is not None:
            self._trace_viewer.clear_pending_live_after_connect()
        if self._diag_console is not None:
            self._diag_console.clear_pending_after_connect()
        ui = self._connect_ui_parent()
        QMessageBox.critical(ui, "Connect failed", message)
        self.statusBar().showMessage("Connect failed")
        self._restore_connect_front()

    @Slot()
    def _on_connect_cancelled(self) -> None:
        if self._closing:
            return
        meta = self._connect_meta or {}
        kind = meta.get("kind", "bus")
        self._pending_after_xcp = None
        if kind == "xcp":
            try:
                self._master.close_listener()
            except Exception:  # noqa: BLE001
                logger.debug("close_listener failed", exc_info=True)
            self._set_link_ui(bus_open=True, xcp=False)
            self._append_log("XCP CONNECT cancelled")
            self.statusBar().showMessage("XCP CONNECT cancelled")
            return
        self._safe_shutdown_bus()
        self._set_link_ui(bus_open=False, xcp=False)
        self._append_log("Connect cancelled")
        if self._trace_viewer is not None:
            self._trace_viewer.clear_pending_live_after_connect()
        if self._diag_console is not None:
            self._diag_console.clear_pending_after_connect()
        self.statusBar().showMessage("Connect cancelled")
        self._restore_connect_front()

    @Slot()
    def _on_connect_thread_finished(self) -> None:
        close_progress(self._connect_progress)
        self._connect_progress = None
        self._connect_worker = None
        self._connect_meta = None

    @Slot()
    def _disconnect(self) -> None:
        if self._connect_busy():
            self._cancel_connect()
            if self._connect_worker is not None:
                self._connect_worker.wait(5000)
            self._on_connect_thread_finished()
        self._stop_poll()
        self._stop_daq()
        try:
            if self._master.is_connected:
                self._master.disconnect()
                self._append_log("XCP DISCONNECT")
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"DISCONNECT warning: {exc}")
        self._master.close_listener()
        self._safe_shutdown_bus()
        if self._trace_viewer is not None:
            self._trace_viewer.notify_bus_closed()
        if self._diag_console is not None:
            self._diag_console.notify_bus_closed()
        self._set_connected_ui(False)
        self.statusBar().showMessage("Disconnected")

    def _safe_shutdown_bus(self) -> None:
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001
            logger.exception("Bus close error")

    @Slot()
    def _unlock_now(self) -> None:
        self._ensure_xcp_then(self._unlock_now_impl)

    def _unlock_now_impl(self) -> None:
        if not self._master.is_connected:
            QMessageBox.information(self, "Not connected", "XCP is not connected.")
            return
        try:
            provider = self.security.load_seedkey_provider()
        except SeedKeyError as exc:
            QMessageBox.critical(self, "Seed&Key", str(exc))
            return
        if provider is None:
            QMessageBox.information(self, "Seed&Key", "Select a Seed&Key .py or .dll first.")
            return
        self._master.config.seed_key = provider
        self._master.config.unlock_resources = self.security.unlock_resource_mask()
        try:
            mask = self._master.unlock_configured_resources()
            self._append_log(
                f"Unlock done — mask=0x{mask:02X} (unlocked tracker=0x{self._master.unlocked_mask:02X})",
                category=LogCategory.XCP,
            )
            self.statusBar().showMessage(f"Unlocked resources 0x{mask:02X}", 4000)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unlock failed")
            self._append_log(f"ERROR unlock: {exc}", category=LogCategory.ERROR, level=logging.ERROR)
            QMessageBox.critical(self, "Unlock failed", str(exc))

    @Slot()
    def _apply_cal_page(self) -> None:
        self._ensure_xcp_then(self._apply_cal_page_impl)

    def _apply_cal_page_impl(self) -> None:
        mode = self.security.cal_page_mode()
        seg = self.security.spn_cal_segment.value()
        page = self.security.spn_cal_page.value()
        try:
            self._master.set_cal_page(mode, seg, page)
            self._append_log(
                f"SET_CAL_PAGE mode=0x{mode:02X} segment={seg} page={page}",
                category=LogCategory.XCP,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("SET_CAL_PAGE failed")
            QMessageBox.critical(self, "SET_CAL_PAGE failed", str(exc))

    @Slot()
    def _query_cal_page(self) -> None:
        self._ensure_xcp_then(self._query_cal_page_impl)

    def _query_cal_page_impl(self) -> None:
        mode = self.security.cal_page_mode()
        seg = self.security.spn_cal_segment.value()
        try:
            page = self._master.get_cal_page(mode, seg)
            self.security.set_cal_page_value(page)
            self._append_log(
                f"GET_CAL_PAGE mode=0x{mode:02X} segment={seg} → page={page}",
                category=LogCategory.XCP,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET_CAL_PAGE failed")
            QMessageBox.critical(self, "GET_CAL_PAGE failed", str(exc))

    def _apply_transport_hints(self, hints: XcpTransportHints) -> None:
        """Apply non-ID transport hints from A2L (bitrate / FD only).

        Master TX / Slave RX CAN IDs are never taken from A2L — always manual.
        """
        applied = self.connection.apply_transport_hints(hints)
        if applied:
            self._append_log(
                "A2L IF_DATA applied (bitrate/FD only; CAN IDs stay manual): "
                + ", ".join(applied)
            )
        # No log when only IDs were present — user controls TX/RX manually

