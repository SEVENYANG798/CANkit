"""Dynamic DAQ configuration and DTO decoding for XCP on CAN."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from xcp_pcan.can.frame_util import CanFrame
from xcp_pcan.symbols.types import Variable, decode_raw, physical_value
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.master import XcpMaster
from xcp_pcan.xcp.poller import PollSample

logger = logging.getLogger(__name__)


@dataclass
class OdtEntry:
    variable: Variable
    size: int


@dataclass
class Odt:
    number: int
    entries: list[OdtEntry] = field(default_factory=list)

    @property
    def payload_size(self) -> int:
        return sum(e.size for e in self.entries)


@dataclass
class DaqLayout:
    """Packed ODTs for one DAQ list."""

    daq_list: int
    odts: list[Odt]
    identification_field_type: int
    header_size: int  # PID + identification (+ optional timestamp filled at runtime)
    max_payload: int
    timestamp_size: int = 0


@dataclass
class DaqRunConfig:
    """Snapshot used to rebuild the DAQ list after a link drop."""

    variables: list[Variable]
    event_channel: int = 0
    prescaler: int = 1
    daq_list: int = 0
    timestamp: bool = False


def pack_variables(
    variables: list[Variable],
    *,
    max_payload: int,
    identification_field_type: int = 0,
    timestamp_size: int = 0,
) -> DaqLayout:
    """Pack variables into ODTs that fit in one DTO payload."""
    if max_payload < 1:
        raise ValueError("max_payload must be >= 1")

    header_extra = proto.daq_identification_header_extra(identification_field_type)
    ts = max(0, int(timestamp_size))
    usable = max_payload - header_extra - ts
    if usable < 1:
        raise ValueError("DTO too small for DAQ identification/timestamp field")

    odts: list[Odt] = []
    current = Odt(number=0)
    for var in variables:
        size = max(1, var.size)
        if size > usable:
            raise ValueError(
                f"Variable {var.name} size={size} exceeds DTO payload {usable} bytes"
            )
        if current.entries and current.payload_size + size > usable:
            odts.append(current)
            current = Odt(number=len(odts))
        current.entries.append(OdtEntry(variable=var, size=size))
    if current.entries:
        odts.append(current)

    return DaqLayout(
        daq_list=0,
        odts=odts,
        identification_field_type=identification_field_type,
        header_size=1 + header_extra + ts,
        max_payload=max_payload,
        timestamp_size=ts,
    )


def parse_dto_header(
    data: bytes,
    *,
    identification_field_type: int,
    daq_list: int,
    little_endian: bool = True,
    timestamp_size: int = 0,
) -> tuple[int, int, int | None] | None:
    """Return ``(relative_or_abs_odt, payload_offset, ecu_timestamp)`` or None if skip.

    For id type 0 the first value is the absolute ODT (= PID).
    For types 1–3 it is the relative ODT number (PID).
    """
    if not data:
        return None
    pid = data[0]
    id_type = int(identification_field_type) & 0x03
    endian = "little" if little_endian else "big"

    if id_type == 0:
        payload_off = 1
        odt_num = pid
    elif id_type == 1:
        if len(data) < 2:
            return None
        if data[1] != (daq_list & 0xFF):
            return None
        payload_off = 2
        odt_num = pid
    elif id_type == 2:
        if len(data) < 3:
            return None
        daq_num = int.from_bytes(data[1:3], endian)
        if daq_num != daq_list:
            return None
        payload_off = 3
        odt_num = pid
    else:  # type 3 — relative ODT + fill + WORD DAQ
        if len(data) < 4:
            return None
        daq_num = int.from_bytes(data[2:4], endian)
        if daq_num != daq_list:
            return None
        payload_off = 4
        odt_num = pid

    ecu_ts: int | None = None
    ts = max(0, int(timestamp_size))
    if ts:
        if len(data) < payload_off + ts:
            return None
        ecu_ts = int.from_bytes(data[payload_off : payload_off + ts], endian)
        payload_off += ts

    return odt_num, payload_off, ecu_ts


class DaqEngine:
    """Configure one dynamic DAQ list and decode incoming DTO frames.

    When DTOs go silent while running, recovers the XCP session and rebuilds
    the DAQ list from the last configuration.
    """

    def __init__(
        self,
        master: XcpMaster,
        *,
        on_sample: Callable[[PollSample], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        dto_timeout_s: float = 2.0,
        rebuild_enabled: bool = True,
    ) -> None:
        self._master = master
        self._on_sample = on_sample
        self._on_error = on_error
        self._dto_timeout_s = max(0.5, float(dto_timeout_s))
        self._rebuild_enabled = rebuild_enabled
        self._layout: DaqLayout | None = None
        self._odt_by_pid: dict[int, Odt] = {}
        self._running = False
        self._info: proto.DaqProcessorInfo | None = None
        self._resolution: proto.DaqResolutionInfo | None = None
        self._run_config: DaqRunConfig | None = None
        self._last_dto_mono = 0.0
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._rebuild_lock = threading.Lock()

    def set_dto_timeout_s(self, timeout_s: float) -> None:
        """Update DTO silence threshold used by the rebuild watchdog."""
        self._dto_timeout_s = max(0.5, float(timeout_s))

    @property
    def dto_timeout_s(self) -> float:
        return self._dto_timeout_s

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def processor_info(self) -> proto.DaqProcessorInfo | None:
        return self._info

    @property
    def resolution_info(self) -> proto.DaqResolutionInfo | None:
        return self._resolution

    @property
    def last_config(self) -> DaqRunConfig | None:
        return self._run_config

    def configure_and_start(
        self,
        variables: list[Variable],
        *,
        event_channel: int = 0,
        prescaler: int = 1,
        daq_list: int = 0,
        timestamp: bool = False,
    ) -> DaqLayout:
        if not self._master.is_connected:
            raise RuntimeError("XCP is not connected")
        if not variables:
            raise ValueError("No variables for DAQ")
        if self._running:
            self.stop()

        cfg = DaqRunConfig(
            variables=list(variables),
            event_channel=event_channel,
            prescaler=prescaler,
            daq_list=daq_list,
            timestamp=timestamp,
        )
        layout = self._setup_daq(cfg)
        self._run_config = cfg
        self._running = True
        self._last_dto_mono = time.monotonic()
        self._start_watchdog()
        return layout

    def rebuild(self) -> DaqLayout:
        """Re-CONNECT (if needed) and re-apply the last DAQ configuration."""
        if self._run_config is None:
            raise RuntimeError("No DAQ configuration to rebuild")
        with self._rebuild_lock:
            return self._rebuild_locked()

    def stop(self) -> None:
        self._stop_watchdog()
        if not self._running and self._layout is None:
            self._master.set_dto_handler(None)
            return
        try:
            if self._master.is_connected:
                self._master.start_stop_synch(proto.SS_STOP_ALL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DAQ stop: %s", exc)
            if self._on_error:
                self._on_error(f"DAQ stop: {exc}")
        finally:
            self._master.set_dto_handler(None)
            self._running = False

    def _setup_daq(self, cfg: DaqRunConfig) -> DaqLayout:
        info = self._master.get_daq_processor_info()
        self._info = info
        logger.info(
            "DAQ processor: max_daq=%s max_event=%s id_type=%s",
            info.max_daq,
            info.max_event_channel,
            info.identification_field_type,
        )

        ts_size = 0
        mode = proto.DAQ_LIST_MODE_DEFAULT
        if cfg.timestamp:
            try:
                reso = self._master.get_daq_resolution_info()
                self._resolution = reso
                ts_size = reso.timestamp_size
                if ts_size <= 0:
                    logger.warning(
                        "DTO timestamp requested but slave reports size=0 — continuing without"
                    )
                    ts_size = 0
                else:
                    mode |= proto.DAQ_LIST_MODE_TIMESTAMP
                    logger.info(
                        "DAQ timestamp enabled: size=%s ticks=%s",
                        ts_size,
                        reso.timestamp_ticks,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("GET_DAQ_RESOLUTION_INFO failed: %s — no DTO timestamp", exc)
                if self._on_error:
                    self._on_error(f"DAQ timestamp unavailable: {exc}")
                self._resolution = None
                ts_size = 0

        max_dto = 8
        if self._master.connect_info is not None and self._master.connect_info.max_dto:
            max_dto = max(2, int(self._master.connect_info.max_dto))
            if getattr(self._master.transport, "fd", False) or self._master.config.fd:
                max_dto = min(64, max_dto)
            else:
                max_dto = min(8, max_dto)
        max_payload = max_dto - 1  # PID occupies first byte

        layout = pack_variables(
            cfg.variables,
            max_payload=max_payload,
            identification_field_type=info.identification_field_type,
            timestamp_size=ts_size,
        )
        layout.daq_list = cfg.daq_list
        self._layout = layout
        self._build_pid_map(layout)

        daq_list = cfg.daq_list
        self._master.free_daq()
        self._master.alloc_daq(1)
        self._master.alloc_odt(daq_list, len(layout.odts))
        for odt in layout.odts:
            self._master.alloc_odt_entry(daq_list, odt.number, len(odt.entries))

        for odt in layout.odts:
            for entry_idx, entry in enumerate(odt.entries):
                self._master.set_daq_ptr(daq_list, odt.number, entry_idx)
                self._master.write_daq(
                    entry.size,
                    entry.variable.address,
                    addr_ext=entry.variable.addr_ext,
                )

        self._master.set_daq_list_mode(
            daq_list,
            cfg.event_channel,
            mode=mode,
            prescaler=max(1, cfg.prescaler),
        )
        self._master.start_stop_daq_list(proto.SS_DAQ_SELECT, daq_list)
        self._master.set_dto_handler(self._on_dto)
        self._master.start_stop_synch(proto.SS_START_SELECTED)
        logger.info(
            "DAQ started: list=%s odts=%s event=%s vars=%s id_type=%s ts=%s",
            daq_list,
            len(layout.odts),
            cfg.event_channel,
            len(cfg.variables),
            layout.identification_field_type,
            ts_size,
        )
        return layout

    def _rebuild_locked(self) -> DaqLayout:
        cfg = self._run_config
        assert cfg is not None
        self._stop_watchdog()
        self._running = False
        self._master.set_dto_handler(None)
        if self._on_error:
            self._on_error("DAQ link lost — recovering session and rebuilding list…")
        try:
            if self._master.config.reconnect_enabled:
                self._master.recover_session()
            elif not self._master.is_connected:
                self._master.connect()
            layout = self._setup_daq(cfg)
            self._running = True
            self._last_dto_mono = time.monotonic()
            self._start_watchdog()
            if self._on_error:
                self._on_error(
                    f"DAQ rebuild OK — {len(layout.odts)} ODT(s), "
                    f"id_type={layout.identification_field_type}"
                )
            return layout
        except Exception as exc:  # noqa: BLE001
            msg = f"DAQ rebuild failed: {exc}"
            logger.warning(msg)
            if self._on_error:
                self._on_error(msg)
            # Stay "running" so watchdog can retry after backoff
            self._running = True
            self._last_dto_mono = time.monotonic()
            self._start_watchdog()
            raise

    def _build_pid_map(self, layout: DaqLayout) -> None:
        self._odt_by_pid.clear()
        for odt in layout.odts:
            self._odt_by_pid[odt.number & 0xFF] = odt

    def _on_dto(self, frame: CanFrame) -> None:
        if self._layout is None or not frame.data:
            return
        layout = self._layout
        parsed = parse_dto_header(
            frame.data,
            identification_field_type=layout.identification_field_type,
            daq_list=layout.daq_list,
            little_endian=self._master.little_endian,
            timestamp_size=layout.timestamp_size,
        )
        if parsed is None:
            return
        odt_num, payload_off, ecu_ts = parsed
        odt = self._odt_by_pid.get(odt_num & 0xFF)
        if odt is None:
            return

        self._last_dto_mono = time.monotonic()
        payload = frame.data[payload_off:]
        offset = 0
        now = time.time()
        for entry in odt.entries:
            chunk = payload[offset : offset + entry.size]
            offset += entry.size
            var = entry.variable
            if len(chunk) < entry.size:
                sample = PollSample(
                    name=var.name,
                    address=var.address,
                    raw=chunk,
                    raw_int=None,
                    physical=None,
                    unit=var.unit,
                    timestamp=now,
                    error="DTO truncated",
                    ecu_timestamp=ecu_ts,
                )
            else:
                raw_val = decode_raw(chunk, var.datatype, self._master.little_endian)
                phys = physical_value(raw_val, var.conversion)
                sample = PollSample(
                    name=var.name,
                    address=var.address,
                    raw=chunk,
                    raw_int=raw_val if isinstance(raw_val, (int, float)) else None,
                    physical=phys,
                    unit=var.unit,
                    timestamp=now,
                    ecu_timestamp=ecu_ts,
                )
            if self._on_sample:
                self._on_sample(sample)

    def _start_watchdog(self) -> None:
        if not self._rebuild_enabled:
            return
        self._stop_watchdog()
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watchdog_loop, name="xcp-daq-watch", daemon=True
        )
        self._watch_thread.start()

    def _stop_watchdog(self) -> None:
        self._watch_stop.set()
        t = self._watch_thread
        self._watch_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.0)

    def _watchdog_loop(self) -> None:
        while not self._watch_stop.wait(0.4):
            if not self._running or self._run_config is None:
                continue
            if self._rebuild_lock.locked():
                continue
            silent = time.monotonic() - self._last_dto_mono
            if silent < self._dto_timeout_s:
                continue
            try:
                with self._rebuild_lock:
                    # Re-check under lock
                    if not self._running or self._run_config is None:
                        continue
                    if time.monotonic() - self._last_dto_mono < self._dto_timeout_s:
                        continue
                    self._rebuild_locked()
            except Exception:  # noqa: BLE001
                # Back off after failure
                self._last_dto_mono = time.monotonic()
                self._watch_stop.wait(self._dto_timeout_s)
