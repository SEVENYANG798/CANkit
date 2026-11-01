"""XCP master session over CAN."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from xcp_pcan.can.frame_util import CanFrame, fit_can_payload
from xcp_pcan.can.transport import CanBusTransport
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.seedkey import SeedKeyProvider

logger = logging.getLogger(__name__)


@dataclass
class XcpSessionConfig:
    master_can_id: int = 0x667
    slave_can_id: int = 0x7E1
    is_extended_id: bool = False
    timeout_s: float = 1.0
    little_endian: bool | None = None  # None = take from CONNECT RES
    fd: bool = False  # mirror transport; used for CTO padding before CONNECT
    # CTO timeout retries (total attempts = 1 + cto_retries)
    cto_retries: int = 2
    cto_retry_delay_s: float = 0.05
    # Session recovery after consecutive link failures
    reconnect_enabled: bool = True
    reconnect_max_attempts: int = 5
    reconnect_backoff_s: float = 0.4
    # Security (Seed&Key)
    auto_unlock: bool = False
    unlock_resources: int | None = None  # None → CAL|DAQ ∩ CONNECT.resource
    seed_key: SeedKeyProvider | None = None
    # Calibration page
    auto_set_cal_page: bool = False
    cal_page_mode: int = proto.CAL_PAGE_MODE_ECU | proto.CAL_PAGE_MODE_XCP
    cal_segment: int = 0
    cal_page: int = 0


@dataclass
class XcpMaster:
    transport: CanBusTransport
    config: XcpSessionConfig = field(default_factory=XcpSessionConfig)

    def __post_init__(self) -> None:
        self._rx_q: queue.Queue[CanFrame] = queue.Queue()
        # RLock: multi-CTO sequences (SET_MTA+DOWNLOAD) hold the lock across calls
        self._cmd_lock = threading.RLock()
        self._connected = False
        self._connect_info: proto.ConnectInfo | None = None
        self._little_endian = True
        self._listening = False
        self._dto_handler: Callable[[CanFrame], None] | None = None
        self._dto_lock = threading.Lock()
        self._unlocked_mask: int = 0

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold the command lock across multiple CTOs (e.g. write + readback)."""
        with self._cmd_lock:
            yield

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def connect_info(self) -> proto.ConnectInfo | None:
        return self._connect_info

    @property
    def little_endian(self) -> bool:
        return self._little_endian

    def set_dto_handler(self, handler: Callable[[CanFrame], None] | None) -> None:
        """Register a callback for DAQ DTO frames (non-CTO PIDs on slave CAN ID)."""
        with self._dto_lock:
            self._dto_handler = handler

    def _on_frame(self, frame: CanFrame) -> None:
        if getattr(frame, "is_error", False):
            return
        if frame.arbitration_id != self.config.slave_can_id:
            return
        if frame.is_extended_id != self.config.is_extended_id:
            return
        if not frame.data:
            return
        pid = frame.data[0]
        if proto.is_cto_pid(pid):
            self._rx_q.put(frame)
            return
        with self._dto_lock:
            handler = self._dto_handler
        if handler is not None:
            try:
                handler(frame)
            except Exception:  # noqa: BLE001
                logger.exception("DTO handler error")

    def _ensure_listener(self) -> None:
        if not self._listening:
            self.transport.add_listener(self._on_frame)
            self._listening = True

    def _drain_rx(self) -> None:
        while True:
            try:
                self._rx_q.get_nowait()
            except queue.Empty:
                break

    def _cto_frame_len(self) -> int | None:
        """Target CTO length for padding (None → classic 8 / minimal FD)."""
        if self._connect_info is not None and self._connect_info.max_cto > 0:
            return int(self._connect_info.max_cto)
        return None

    def _prepare_cto(self, request: bytes) -> bytes:
        use_fd = bool(getattr(self.transport, "fd", False) or self.config.fd)
        max_len = self._cto_frame_len()
        if use_fd:
            # Before CONNECT, send at least 8 bytes (widely accepted on FD)
            if max_len is None:
                max_len = 8
            return fit_can_payload(request, fd=True, max_len=max_len)
        return fit_can_payload(request[:8] if len(request) > 8 else request, fd=False)

    def _transact_once(self, request: bytes) -> proto.XcpResponse:
        if not self.transport.is_open:
            raise RuntimeError("CAN transport is not open")
        self._ensure_listener()
        frame_data = self._prepare_cto(request)
        with self._cmd_lock:
            self._drain_rx()
            logger.debug(
                "CTO TX cmd=0x%02X data=%s",
                request[0] if request else 0,
                frame_data.hex(),
            )
            self.transport.send(
                self.config.master_can_id,
                frame_data,
                is_extended_id=self.config.is_extended_id,
            )
            deadline = time.monotonic() + self.config.timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"XCP CTO timeout after {self.config.timeout_s:.2f}s "
                        f"(waiting for 0x{self.config.slave_can_id:X})"
                    )
                try:
                    frame = self._rx_q.get(timeout=min(remaining, 0.05))
                except queue.Empty:
                    continue
                resp = proto.parse_response(frame.data)
                if resp.pid == proto.Pid.ERR and resp.error_code == proto.ErrCode.CMD_BUSY:
                    time.sleep(0.01)
                    continue
                logger.debug(
                    "CTO RX pid=0x%02X data=%s",
                    resp.pid,
                    frame.data.hex(),
                )
                return resp

    def _transact(self, request: bytes) -> proto.XcpResponse:
        """Send CTO with timeout retries (does not reconnect the session)."""
        attempts = 1 + max(0, int(self.config.cto_retries))
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._transact_once(request)
            except TimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "CTO timeout attempt %s/%s: %s",
                    attempt + 1,
                    attempts,
                    exc,
                )
                if attempt + 1 >= attempts:
                    break
                time.sleep(max(0.0, self.config.cto_retry_delay_s))
            except Exception:
                raise
        assert last_exc is not None
        raise last_exc

    def _expect_ok(self, resp: proto.XcpResponse, what: str) -> bytes:
        if not resp.is_ok:
            raise proto.XcpError(resp.error_code or 0, f"{what} failed")
        return resp.payload

    def connect(
        self,
        mode: int = proto.CONNECT_MODE_NORMAL,
        *,
        apply_post: bool = True,
    ) -> proto.ConnectInfo:
        resp = self._transact(proto.encode_connect(mode))
        info = proto.parse_connect_response(resp)
        self._connect_info = info
        self._unlocked_mask = 0
        if self.config.little_endian is None:
            self._little_endian = info.byte_order_intel
        else:
            self._little_endian = self.config.little_endian
        self._connected = True
        logger.info(
            "XCP connected: max_cto=%s max_dto=%s intel=%s resource=0x%02X",
            info.max_cto,
            info.max_dto,
            self._little_endian,
            info.available_resources,
        )
        if apply_post:
            self.apply_post_connect()
        return info

    def disconnect(self) -> None:
        if not self.transport.is_open:
            self._connected = False
            return
        try:
            resp = self._transact(proto.encode_disconnect())
            if resp.is_error:
                logger.warning("DISCONNECT returned ERR 0x%02X", resp.error_code or 0)
        except Exception:  # noqa: BLE001
            logger.exception("DISCONNECT failed")
        finally:
            self._connected = False
            self._connect_info = None
            self._unlocked_mask = 0
            self.set_dto_handler(None)

    def recover_session(self) -> proto.ConnectInfo:
        """Re-CONNECT after brief link loss. Keeps the CAN bus open.

        Tries SYNCH (best-effort) then CONNECT up to ``reconnect_max_attempts``.
        Re-applies auto unlock / cal page via ``apply_post_connect``.
        """
        if not self.config.reconnect_enabled:
            raise RuntimeError("XCP reconnect is disabled in session config")

        self._connected = False
        self._connect_info = None
        self._unlocked_mask = 0
        self.set_dto_handler(None)

        # Best-effort reopen bus if the transport dropped
        reopen = getattr(self.transport, "reopen", None)
        if callable(reopen) and not self.transport.is_open:
            logger.warning("CAN bus closed — attempting reopen")
            reopen()

        attempts = max(1, int(self.config.reconnect_max_attempts))
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                try:
                    # ERR/SYNCH or timeout is fine — clears slave command state
                    self._transact_once(proto.encode_synch())
                except Exception:  # noqa: BLE001
                    pass
                info = self.connect(apply_post=True)
                logger.info("XCP session recovered (attempt %s/%s)", i + 1, attempts)
                return info
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                delay = self.config.reconnect_backoff_s * (i + 1)
                logger.warning(
                    "XCP reconnect attempt %s/%s failed: %s (sleep %.2fs)",
                    i + 1,
                    attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError(
            f"XCP reconnect failed after {attempts} attempt(s): {last_exc}"
        ) from last_exc

    def apply_post_connect(self) -> None:
        """Optional Seed&Key unlock and SET_CAL_PAGE after CONNECT."""
        if self.config.auto_unlock:
            self.unlock_configured_resources()
        if self.config.auto_set_cal_page:
            self.set_cal_page(
                self.config.cal_page_mode,
                self.config.cal_segment,
                self.config.cal_page,
            )
            logger.info(
                "SET_CAL_PAGE mode=0x%02X segment=%s page=%s",
                self.config.cal_page_mode,
                self.config.cal_segment,
                self.config.cal_page,
            )

    def _seed_payload_budget(self) -> int:
        """Max seed/key bytes in one CTO after CMD+length."""
        max_cto = 8
        if self._connect_info is not None and self._connect_info.max_cto > 0:
            max_cto = int(self._connect_info.max_cto)
        if not (getattr(self.transport, "fd", False) or self.config.fd):
            max_cto = min(max_cto, 8)
        else:
            max_cto = min(max_cto, 64)
        return max(1, max_cto - 2)

    def get_seed(self, resource: int) -> bytes:
        """Fetch full seed for one resource bit. Empty ⇒ already unlocked."""
        with self._cmd_lock:
            seed = bytearray()
            mode = proto.SEED_MODE_FIRST
            while True:
                resp = self._transact(proto.encode_get_seed(mode, resource))
                remaining, chunk = proto.parse_get_seed_response(resp)
                if remaining == 0:
                    return b""
                take = min(remaining, len(chunk))
                seed.extend(chunk[:take])
                left = remaining - take
                if left <= 0:
                    break
                mode = proto.SEED_MODE_REMAINING
            return bytes(seed)

    def unlock(self, key: bytes) -> int:
        """Send UNLOCK key (multi-CTO if needed). Returns protection mask."""
        if not key:
            raise ValueError("UNLOCK key must not be empty")
        with self._cmd_lock:
            budget = self._seed_payload_budget()
            offset = 0
            remaining = len(key)
            protection = 0
            while remaining > 0:
                chunk = key[offset : offset + budget]
                resp = self._transact(proto.encode_unlock(remaining, chunk))
                protection = proto.parse_unlock_response(resp)
                offset += len(chunk)
                remaining -= len(chunk)
            return protection

    def unlock_resource(
        self,
        resource: int,
        provider: SeedKeyProvider | None = None,
    ) -> bool:
        """GET_SEED → compute key → UNLOCK for a single resource bit.

        Returns True if the resource appears unlocked afterwards.
        """
        provider = provider or self.config.seed_key
        if provider is None:
            raise RuntimeError("No Seed&Key provider configured")
        seed = self.get_seed(resource)
        if not seed:
            logger.info("Resource 0x%02X already unlocked", resource)
            self._unlocked_mask |= int(resource)
            return True
        key = provider.compute_key(int(resource), seed)
        logger.info(
            "UNLOCK resource=0x%02X seed_len=%s key_len=%s via %s",
            resource,
            len(seed),
            len(key),
            getattr(provider, "name", type(provider).__name__),
        )
        protection = self.unlock(key)
        unlocked = (protection & int(resource)) == 0
        if unlocked:
            self._unlocked_mask |= int(resource)
        else:
            logger.warning(
                "Resource 0x%02X still protected after UNLOCK (mask=0x%02X)",
                resource,
                protection,
            )
        return unlocked

    def unlock_configured_resources(self) -> int:
        """Unlock resources from config (intersected with CONNECT availability).

        Returns bitmask of resources successfully unlocked this call.
        """
        provider = self.config.seed_key
        if provider is None:
            logger.warning("auto_unlock enabled but no Seed&Key provider — skip")
            return 0
        if self._connect_info is None:
            raise RuntimeError("Not connected")
        wanted = self.config.unlock_resources
        if wanted is None:
            wanted = proto.Resource.CAL_PAG | proto.Resource.DAQ
        wanted &= self._connect_info.available_resources
        if wanted == 0:
            logger.info("No unlockable resources advertised by slave")
            return 0
        ok_mask = 0
        for bit in (
            proto.Resource.CAL_PAG,
            proto.Resource.DAQ,
            proto.Resource.STIM,
            proto.Resource.PGM,
        ):
            if wanted & int(bit):
                try:
                    if self.unlock_resource(int(bit), provider):
                        ok_mask |= int(bit)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Unlock resource 0x%02X failed: %s", int(bit), exc)
        return ok_mask

    @property
    def unlocked_mask(self) -> int:
        return self._unlocked_mask

    def set_cal_page(self, mode: int, segment: int, page: int) -> None:
        resp = self._transact(proto.encode_set_cal_page(mode, segment, page))
        self._expect_ok(resp, "SET_CAL_PAGE")

    def get_cal_page(self, mode: int, segment: int) -> int:
        resp = self._transact(proto.encode_get_cal_page(mode, segment))
        return proto.parse_get_cal_page_response(resp)

    def get_status(self) -> bytes:
        resp = self._transact(proto.encode_get_status())
        return self._expect_ok(resp, "GET_STATUS")

    def set_mta(self, address: int, addr_ext: int = 0) -> None:
        resp = self._transact(
            proto.encode_set_mta(
                address, addr_ext=addr_ext, little_endian=self._little_endian
            )
        )
        self._expect_ok(resp, "SET_MTA")

    def upload(self, size: int) -> bytes:
        resp = self._transact(proto.encode_upload(size))
        return proto.parse_upload_data(resp)

    def short_upload(self, size: int, address: int, addr_ext: int = 0) -> bytes:
        resp = self._transact(
            proto.encode_short_upload(
                size,
                address,
                addr_ext=addr_ext,
                little_endian=self._little_endian,
            )
        )
        return proto.parse_upload_data(resp)

    def _upload_chunk_limit(self) -> int:
        """Max data bytes in one UPLOAD/SHORT_UPLOAD response (PID excluded)."""
        # Classic CAN: 7; CAN FD: up to max_cto-1 (commonly 63)
        limit = 7
        if self._connect_info is not None and self._connect_info.max_cto > 1:
            limit = max(1, self._connect_info.max_cto - 1)
            if not (getattr(self.transport, "fd", False) or self.config.fd):
                limit = min(limit, 7)
            else:
                limit = min(limit, 63)
        return limit

    def read_memory(self, address: int, size: int, addr_ext: int = 0) -> bytes:
        """Read ``size`` bytes from ``address`` using SHORT_UPLOAD or SET_MTA+UPLOAD."""
        if size <= 0:
            return b""
        max_chunk = self._upload_chunk_limit()

        out = bytearray()
        offset = 0
        while offset < size:
            chunk = min(max_chunk, size - offset)
            addr = address + offset
            data = self.short_upload(chunk, addr, addr_ext=addr_ext)
            out.extend(data[:chunk])
            offset += chunk
        return bytes(out)

    def _cto_data_budget(self) -> int:
        """Max payload bytes available in one CTO after a 2-byte DOWNLOAD header."""
        max_cto = 8
        if self._connect_info is not None and self._connect_info.max_cto > 0:
            max_cto = int(self._connect_info.max_cto)
        if not (getattr(self.transport, "fd", False) or self.config.fd):
            max_cto = min(max_cto, 8)
        else:
            max_cto = min(max_cto, 64)
        return max(1, max_cto - 2)

    def _short_download_fits(self, data_len: int) -> bool:
        """SHORT_DOWNLOAD needs 8 header bytes + data inside one CTO."""
        max_cto = 8
        if self._connect_info is not None and self._connect_info.max_cto > 0:
            max_cto = int(self._connect_info.max_cto)
        if not (getattr(self.transport, "fd", False) or self.config.fd):
            max_cto = min(max_cto, 8)
        else:
            max_cto = min(max_cto, 64)
        return (8 + data_len) <= max_cto

    def short_download(self, address: int, data: bytes, addr_ext: int = 0) -> None:
        resp = self._transact(
            proto.encode_short_download(
                address,
                data,
                addr_ext=addr_ext,
                little_endian=self._little_endian,
            )
        )
        self._expect_ok(resp, "SHORT_DOWNLOAD")

    def download(self, data: bytes) -> None:
        resp = self._transact(proto.encode_download(data))
        self._expect_ok(resp, "DOWNLOAD")

    def download_next(self, data: bytes) -> None:
        resp = self._transact(proto.encode_download_next(data))
        self._expect_ok(resp, "DOWNLOAD_NEXT")

    def write_memory(self, address: int, data: bytes, addr_ext: int = 0) -> None:
        """Write bytes via SHORT_DOWNLOAD or SET_MTA + DOWNLOAD[/NEXT].

        Holds ``_cmd_lock`` for the whole sequence so a concurrent poller
        cannot interleave CTOs between SET_MTA and DOWNLOAD.
        """
        if not data:
            return
        with self._cmd_lock:
            if self._short_download_fits(len(data)):
                self.short_download(address, data, addr_ext=addr_ext)
                return

            self.set_mta(address, addr_ext=addr_ext)
            budget = self._cto_data_budget()
            offset = 0
            first = True
            while offset < len(data):
                chunk = data[offset : offset + budget]
                if first:
                    self.download(chunk)
                    first = False
                else:
                    self.download_next(chunk)
                offset += len(chunk)

    # --- DAQ command helpers ---------------------------------------------------

    def get_daq_processor_info(self) -> proto.DaqProcessorInfo:
        resp = self._transact(proto.encode_get_daq_processor_info())
        return proto.parse_daq_processor_info(resp, little_endian=self._little_endian)

    def get_daq_resolution_info(self) -> proto.DaqResolutionInfo:
        resp = self._transact(proto.encode_get_daq_resolution_info())
        return proto.parse_daq_resolution_info(resp, little_endian=self._little_endian)

    def free_daq(self) -> None:
        self._expect_ok(self._transact(proto.encode_free_daq()), "FREE_DAQ")

    def alloc_daq(self, count: int) -> None:
        self._expect_ok(
            self._transact(proto.encode_alloc_daq(count, little_endian=self._little_endian)),
            "ALLOC_DAQ",
        )

    def alloc_odt(self, daq_list: int, odt_count: int) -> None:
        self._expect_ok(
            self._transact(
                proto.encode_alloc_odt(
                    daq_list, odt_count, little_endian=self._little_endian
                )
            ),
            "ALLOC_ODT",
        )

    def alloc_odt_entry(self, daq_list: int, odt_number: int, entries: int) -> None:
        self._expect_ok(
            self._transact(
                proto.encode_alloc_odt_entry(
                    daq_list,
                    odt_number,
                    entries,
                    little_endian=self._little_endian,
                )
            ),
            "ALLOC_ODT_ENTRY",
        )

    def set_daq_ptr(self, daq_list: int, odt_number: int, odt_entry: int) -> None:
        self._expect_ok(
            self._transact(
                proto.encode_set_daq_ptr(
                    daq_list,
                    odt_number,
                    odt_entry,
                    little_endian=self._little_endian,
                )
            ),
            "SET_DAQ_PTR",
        )

    def write_daq(
        self,
        size: int,
        address: int,
        *,
        bit_offset: int = 0xFF,
        addr_ext: int = 0,
    ) -> None:
        self._expect_ok(
            self._transact(
                proto.encode_write_daq(
                    size,
                    address,
                    bit_offset=bit_offset,
                    addr_ext=addr_ext,
                    little_endian=self._little_endian,
                )
            ),
            "WRITE_DAQ",
        )

    def set_daq_list_mode(
        self,
        daq_list: int,
        event_channel: int,
        *,
        mode: int = proto.DAQ_LIST_MODE_DEFAULT,
        prescaler: int = 1,
        priority: int = 0,
    ) -> None:
        self._expect_ok(
            self._transact(
                proto.encode_set_daq_list_mode(
                    daq_list,
                    event_channel,
                    mode=mode,
                    prescaler=prescaler,
                    priority=priority,
                    little_endian=self._little_endian,
                )
            ),
            "SET_DAQ_LIST_MODE",
        )

    def start_stop_daq_list(self, mode: int, daq_list: int) -> None:
        self._expect_ok(
            self._transact(
                proto.encode_start_stop_daq_list(
                    mode, daq_list, little_endian=self._little_endian
                )
            ),
            "START_STOP_DAQ_LIST",
        )

    def start_stop_synch(self, mode: int) -> None:
        self._expect_ok(
            self._transact(proto.encode_start_stop_synch(mode)),
            "START_STOP_SYNCH",
        )

    def close_listener(self) -> None:
        if self._listening:
            self.transport.remove_listener(self._on_frame)
            self._listening = False
        self.set_dto_handler(None)
