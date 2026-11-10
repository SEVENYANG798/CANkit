"""Minimal ISO-TP (ISO 15765-2) client over CanBusTransport."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from xcp_pcan.can.frame_util import CanFrame, next_can_fd_length
from xcp_pcan.can.transport import CanBusTransport

logger = logging.getLogger(__name__)


class IsoTpError(RuntimeError):
    """ISO-TP protocol or configuration error."""


class IsoTpTimeout(TimeoutError):
    """No complete ISO-TP response before deadline."""


def build_sf_wire_frame(
    uds: bytes,
    *,
    pad_byte: int = 0xCC,
    min_frame_len: int = 8,
    fd: bool = False,
) -> bytes:
    """Build the CAN payload actually sent for a short UDS request (SF + pad).

    Example: ``22 F1 87`` → ``03 22 F1 87 CC CC CC CC`` (pad=0xCC, min 8).
    """
    if not uds:
        return b""
    if len(uds) <= 7:
        pci_data = bytes([len(uds) & 0x0F]) + uds
    elif fd and len(uds) <= 62:
        pci_data = bytes([0x00, len(uds)]) + uds
    else:
        # Multi-frame: preview First Frame only (UI Raw field).
        total = min(len(uds), 0xFFF)
        cap = 64 if fd else 8
        ff_data_len = max(1, cap - 2)
        pci_data = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + uds[:ff_data_len]

    cap = 64 if fd else 8
    min_len = max(0, min(cap, int(min_frame_len)))
    need = max(len(pci_data), min_len)
    target = next_can_fd_length(need) if fd else 8
    if len(pci_data) >= target:
        return pci_data[:target]
    return pci_data + bytes([pad_byte & 0xFF]) * (target - len(pci_data))


@dataclass(slots=True)
class IsoTpConfig:
    tx_id: int
    rx_id: int
    is_extended_id: bool = False
    fd: bool | None = None  # None → follow transport.fd
    pad_byte: int = 0xCC  # common automotive ISO-TP padding
    timeout_s: float = 2.0
    # Pad short frames to at least this many bytes (ECUs often expect 8 even on FD).
    min_frame_len: int = 8
    # FC we send when receiving multi-frame from ECU
    st_min_ms: int = 0
    block_size: int = 0  # 0 = send all remaining CFs without further FC


class IsoTpClient:
    """Blocking request/response ISO-TP client (tester side)."""

    def __init__(self, transport: CanBusTransport, config: IsoTpConfig) -> None:
        self._transport = transport
        self._cfg = config
        self._rx_q: queue.Queue[CanFrame] = queue.Queue()
        self._listening = False
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        """Unblock a waiting ``request()`` (e.g. dialog closed)."""
        self._cancelled = True
        self._stop_listen()

    def close(self) -> None:
        self._cancelled = True
        self._stop_listen()

    def _use_fd(self) -> bool:
        if self._cfg.fd is not None:
            return bool(self._cfg.fd)
        return bool(getattr(self._transport, "fd", False))

    def _frame_capacity(self) -> int:
        """Max CAN payload bytes per frame (Classic 8 / FD up to 64)."""
        return 64 if self._use_fd() else 8

    def _pad(self, pci_and_data: bytes) -> bytes:
        """Pad PCI+data like field tools: e.g. 03 22 F1 87 CC CC CC CC."""
        cap = self._frame_capacity()
        if len(pci_and_data) > cap:
            raise IsoTpError(f"ISO-TP frame too long ({len(pci_and_data)} > {cap})")
        min_len = max(0, min(cap, int(self._cfg.min_frame_len)))
        need = max(len(pci_and_data), min_len)
        if self._use_fd():
            target = next_can_fd_length(need)
        else:
            target = 8
        if len(pci_and_data) >= target:
            return pci_and_data[:target]
        return pci_and_data + bytes([self._cfg.pad_byte & 0xFF]) * (target - len(pci_and_data))

    def _send_raw(self, data: bytes) -> None:
        payload = self._pad(data)
        self._transport.send(
            self._cfg.tx_id,
            payload,
            is_extended_id=self._cfg.is_extended_id,
            fd=self._use_fd(),
            bitrate_switch=self._use_fd(),
        )

    def _on_frame(self, frame: CanFrame) -> None:
        if frame.is_error or not frame.is_rx:
            return
        if frame.arbitration_id != self._cfg.rx_id:
            return
        if bool(frame.is_extended_id) != bool(self._cfg.is_extended_id):
            return
        try:
            self._rx_q.put_nowait(frame)
        except queue.Full:
            pass

    def _start_listen(self) -> None:
        if self._listening:
            return
        self._transport.add_listener(self._on_frame)
        self._listening = True

    def _stop_listen(self) -> None:
        if not self._listening:
            return
        try:
            self._transport.remove_listener(self._on_frame)
        except Exception:  # noqa: BLE001
            pass
        self._listening = False

    def _drain(self) -> None:
        while True:
            try:
                self._rx_q.get_nowait()
            except queue.Empty:
                break

    def _recv_until(self, deadline: float) -> CanFrame:
        while True:
            if self._cancelled:
                raise IsoTpError("ISO-TP request cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsoTpTimeout(
                    f"ISO-TP timeout after {self._cfg.timeout_s:.2f}s "
                    f"(waiting 0x{self._cfg.rx_id:X})"
                )
            try:
                return self._rx_q.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue

    def request(self, payload: bytes) -> bytes:
        """Send UDS payload via ISO-TP and return the response payload (no PCI)."""
        if not payload:
            raise IsoTpError("empty UDS payload")
        if not self._transport.is_open:
            raise IsoTpError("CAN bus is not open — Connect first")
        with self._lock:
            self._cancelled = False
            return self._request_locked(bytes(payload))

    def _request_locked(self, payload: bytes) -> bytes:
        self._start_listen()
        self._drain()
        deadline = time.monotonic() + max(0.1, float(self._cfg.timeout_s))
        self._transmit(payload, deadline)
        return self._receive(deadline)

    def _build_sf(self, payload: bytes) -> bytes:
        n = len(payload)
        if n <= 7:
            return bytes([0x00 | n]) + payload
        # CAN FD escape SF (ISO 15765-2)
        if not self._use_fd():
            raise IsoTpError("payload requires First Frame on Classic CAN")
        if n > 62:
            raise IsoTpError("Single Frame data too long for CAN FD")
        return bytes([0x00, n]) + payload

    def _can_send_as_sf(self, payload: bytes) -> bool:
        n = len(payload)
        if n <= 7:
            return True
        return bool(self._use_fd() and n <= 62 and n <= self._frame_capacity() - 2)

    def _transmit(self, payload: bytes, deadline: float) -> None:
        if self._can_send_as_sf(payload):
            self._send_raw(self._build_sf(payload))
            return

        # First Frame + Consecutive Frames
        total = len(payload)
        if total > 0xFFF:
            raise IsoTpError("ISO-TP payload exceeds 4095 bytes")
        cap = self._frame_capacity()
        ff_data_len = cap - 2
        if ff_data_len < 1:
            raise IsoTpError("CAN frame too short for First Frame")
        pci0 = 0x10 | ((total >> 8) & 0x0F)
        pci1 = total & 0xFF
        first = bytes([pci0, pci1]) + payload[:ff_data_len]
        self._send_raw(first)
        offset = ff_data_len

        # Wait for Flow Control from ECU
        while True:
            frame = self._recv_until(deadline)
            fc = self._parse_fc(frame.data)
            if fc is None:
                continue
            fs, block_size, st_min = fc
            if fs == 2:  # overflow
                raise IsoTpError("ECU FlowControl overflow")
            if fs == 1:  # wait
                continue
            break

        sn = 1
        frames_in_block = 0
        cf_data_len = cap - 1
        while offset < total:
            chunk = payload[offset : offset + cf_data_len]
            self._send_raw(bytes([0x20 | (sn & 0x0F)]) + chunk)
            offset += len(chunk)
            sn = (sn + 1) & 0x0F
            frames_in_block += 1
            if st_min > 0:
                time.sleep(st_min / 1000.0)
            if block_size and frames_in_block >= block_size and offset < total:
                frames_in_block = 0
                while True:
                    frame = self._recv_until(deadline)
                    fc = self._parse_fc(frame.data)
                    if fc is None:
                        continue
                    fs, block_size, st_min = fc
                    if fs == 2:
                        raise IsoTpError("ECU FlowControl overflow")
                    if fs == 1:
                        continue
                    break

    @staticmethod
    def _parse_fc(data: bytes) -> tuple[int, int, float] | None:
        if not data:
            return None
        pci = data[0]
        if (pci & 0xF0) != 0x30:
            return None
        fs = pci & 0x0F
        bs = data[1] if len(data) > 1 else 0
        st_raw = data[2] if len(data) > 2 else 0
        if st_raw <= 0x7F:
            st_min = float(st_raw)
        elif 0xF1 <= st_raw <= 0xF9:
            st_min = (st_raw - 0xF0) * 0.1
        else:
            st_min = 0.0
        return fs, bs, st_min

    def _send_fc(self) -> None:
        st = max(0, min(0x7F, int(self._cfg.st_min_ms)))
        bs = max(0, min(0xFF, int(self._cfg.block_size)))
        self._send_raw(bytes([0x30, bs, st]))

    def _receive(self, deadline: float) -> bytes:
        """Assemble one ISO-TP indication; auto-continue on NRC 0x78 pending."""
        pending_rounds = 0
        timeout_s = max(0.1, float(self._cfg.timeout_s))
        while True:
            payload = self._receive_one(deadline)
            if len(payload) >= 3 and payload[0] == 0x7F and payload[2] == 0x78:
                pending_rounds += 1
                if pending_rounds > 20:
                    raise IsoTpError(
                        "UDS response pending (NRC 0x78) limit exceeded"
                    )
                # ECU is still working — refresh the wait window.
                deadline = time.monotonic() + timeout_s
                logger.debug("UDS NRC 0x78 pending (%s)", pending_rounds)
                continue
            return payload

    def _receive_one(self, deadline: float) -> bytes:
        while True:
            frame = self._recv_until(deadline)
            data = bytes(frame.data)
            if not data:
                continue
            pci = data[0]
            pci_type = pci & 0xF0
            if pci_type == 0x00:
                # Single Frame
                if pci == 0x00 and len(data) >= 2:
                    # FD escape
                    n = data[1]
                    return data[2 : 2 + n]
                n = pci & 0x0F
                if n == 0 or n > 7:
                    continue
                return data[1 : 1 + n]
            if pci_type == 0x10:
                # First Frame
                if len(data) < 2:
                    continue
                total = ((pci & 0x0F) << 8) | data[1]
                buf = bytearray(data[2:])
                self._send_fc()
                expected_sn = 1
                while len(buf) < total:
                    cf_frame = self._recv_until(deadline)
                    cf = bytes(cf_frame.data)
                    if not cf or (cf[0] & 0xF0) != 0x20:
                        continue
                    sn = cf[0] & 0x0F
                    if sn != expected_sn:
                        raise IsoTpError(
                            f"ISO-TP SN mismatch (got {sn}, expected {expected_sn})"
                        )
                    buf.extend(cf[1:])
                    expected_sn = (expected_sn + 1) & 0x0F
                return bytes(buf[:total])
            # Ignore unexpected FC/CF while waiting for response start
            continue
