"""Background CAN bus open and optional XCP CONNECT (keeps UI responsive)."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QThread, Signal

from xcp_pcan.can.transport import CanBusTransport
from xcp_pcan.util.cancel import OperationCancelled
from xcp_pcan.xcp.master import XcpMaster

logger = logging.getLogger(__name__)


class ConnectWorker(QThread):
    """Open the CAN bus and/or run XCP CONNECT off the GUI thread.

    ``mode``:
      - ``\"bus\"``: open hardware only (Monitor / TX ready).
      - ``\"xcp\"``: XCP CONNECT on an already-open bus (Start poll / DAQ / write).
    """

    progress = Signal(str)
    finished_ok = Signal(object)  # ConnectInfo when mode=xcp; None when mode=bus
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        transport: CanBusTransport,
        master: XcpMaster,
        open_kwargs: dict[str, Any] | None = None,
        *,
        mode: str = "bus",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._master = master
        self._open_kwargs = dict(open_kwargs or {})
        self._mode = mode if mode in ("bus", "xcp") else "bus"

    def abort(self) -> None:
        """Request stop; closing the bus unblocks a waiting CTO recv."""
        self.requestInterruption()
        if self._mode == "bus":
            try:
                if self._transport.is_open:
                    self._transport.close()
            except Exception:  # noqa: BLE001
                logger.debug("ConnectWorker abort: bus close failed", exc_info=True)
        else:
            # Unblock CTO wait without tearing down the bus (Trace stays usable).
            try:
                self._master.close_listener()
            except Exception:  # noqa: BLE001
                logger.debug("ConnectWorker abort: close_listener failed", exc_info=True)

    def run(self) -> None:  # noqa: N802
        try:
            if self.isInterruptionRequested():
                raise OperationCancelled()
            if self._mode == "bus":
                self._run_bus()
            else:
                self._run_xcp()
        except OperationCancelled:
            if self._mode == "bus":
                try:
                    if self._transport.is_open:
                        self._transport.close()
                except Exception:  # noqa: BLE001
                    logger.debug("ConnectWorker cancel: bus close failed", exc_info=True)
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self.isInterruptionRequested():
                self.cancelled.emit()
                return
            if self._mode == "bus":
                try:
                    if self._transport.is_open:
                        self._transport.close()
                except Exception:  # noqa: BLE001
                    logger.debug("ConnectWorker fail: bus close failed", exc_info=True)
            else:
                # Leave bus open; drop XCP listener so Monitor is not starved.
                try:
                    self._master.close_listener()
                except Exception:  # noqa: BLE001
                    logger.debug("ConnectWorker fail: close_listener failed", exc_info=True)
            self.failed.emit(str(exc))

    def _run_bus(self) -> None:
        self.progress.emit("Opening CAN bus…")
        self._transport.open(**self._open_kwargs)
        if self.isInterruptionRequested():
            raise OperationCancelled()
        self.finished_ok.emit(None)

    def _run_xcp(self) -> None:
        if not self._transport.is_open:
            raise RuntimeError("CAN bus is not open")
        if self.isInterruptionRequested():
            raise OperationCancelled()
        self.progress.emit("XCP CONNECT…")
        try:
            info = self._master.connect()
        except Exception:
            try:
                self._master.close_listener()
            except Exception:  # noqa: BLE001
                logger.debug("ConnectWorker XCP fail: close_listener failed", exc_info=True)
            raise
        if self.isInterruptionRequested():
            raise OperationCancelled()
        self.finished_ok.emit(info)
