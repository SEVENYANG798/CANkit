"""XCP on CAN CTO encode / decode (ASAM XCP Part 2 — Protocol Layer).

Supports connect, memory upload (polling), and dynamic DAQ configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final


class Command(IntEnum):
    CONNECT = 0xFF
    DISCONNECT = 0xFE
    GET_STATUS = 0xFD
    SYNCH = 0xFC
    GET_ID = 0xFA
    GET_SEED = 0xF8
    UNLOCK = 0xF7
    SET_MTA = 0xF6
    UPLOAD = 0xF5
    SHORT_UPLOAD = 0xF4
    DOWNLOAD = 0xF0
    DOWNLOAD_NEXT = 0xEF
    SHORT_DOWNLOAD = 0xED
    # Calibration page (PAG)
    SET_CAL_PAGE = 0xEB
    GET_CAL_PAGE = 0xEA
    GET_PAG_PROCESSOR_INFO = 0xE9
    # DAQ
    START_STOP_DAQ_LIST = 0xDE
    START_STOP_SYNCH = 0xDD
    GET_DAQ_PROCESSOR_INFO = 0xDA
    GET_DAQ_RESOLUTION_INFO = 0xD9
    SET_DAQ_PTR = 0xE2
    WRITE_DAQ = 0xE1
    SET_DAQ_LIST_MODE = 0xE0
    GET_DAQ_LIST_MODE = 0xDF
    CLEAR_DAQ_LIST = 0xE3
    FREE_DAQ = 0xD6
    ALLOC_DAQ = 0xD5
    ALLOC_ODT = 0xD4
    ALLOC_ODT_ENTRY = 0xD3


class Resource(IntEnum):
    """CONNECT / GET_SEED resource bit masks (ASAM XCP)."""

    CAL_PAG = 0x01
    DAQ = 0x02
    STIM = 0x04
    PGM = 0x08


# GET_SEED mode
SEED_MODE_FIRST: Final = 0x00
SEED_MODE_REMAINING: Final = 0x01

# SET_CAL_PAGE mode bits
CAL_PAGE_MODE_ECU: Final = 0x01
CAL_PAGE_MODE_XCP: Final = 0x02
CAL_PAGE_MODE_ALL: Final = 0x80


class Pid(IntEnum):
    RES = 0xFF
    ERR = 0xFE
    # Event / Service (not used heavily in v1)
    EV = 0xFD
    SERV = 0xFC


class ErrCode(IntEnum):
    CMD_SYNCH = 0x00
    CMD_BUSY = 0x10
    DAQ_ACTIVE = 0x11
    PGM_ACTIVE = 0x12
    CMD_UNKNOWN = 0x20
    CMD_SYNTAX = 0x21
    OUT_OF_RANGE = 0x22
    WRITE_PROTECTED = 0x23
    ACCESS_DENIED = 0x24
    ACCESS_LOCKED = 0x25
    PAGE_NOT_VALID = 0x26
    MODE_NOT_VALID = 0x27
    SEGMENT_NOT_VALID = 0x28
    SEQUENCE = 0x29
    DAQ_CONFIG = 0x2A
    MEMORY_OVERFLOW = 0x30
    GENERIC = 0x31
    VERIFY = 0x32
    RESOURCE_TEMPORARY_NOT_ACCESSIBLE = 0x33
    SUBCMD_UNKNOWN = 0x34


CONNECT_MODE_NORMAL: Final = 0x00
CONNECT_MODE_USER_DEFINED: Final = 0x01


@dataclass(frozen=True)
class ConnectInfo:
    resource: int
    comm_mode_basic: int
    max_cto: int
    max_dto: int
    protocol_version: int
    transport_version: int

    @property
    def byte_order_intel(self) -> bool:
        """True if Intel (little-endian) address/data packing."""
        return (self.comm_mode_basic & 0x01) == 0

    @property
    def address_granularity(self) -> int:
        """1 = BYTE, 2 = WORD, 4 = DWORD."""
        bits = (self.comm_mode_basic >> 1) & 0x03
        return {0: 1, 1: 2, 2: 4}.get(bits, 1)

    @property
    def available_resources(self) -> int:
        """Resource bits advertised by the slave (CAL/DAQ/STIM/PGM)."""
        return self.resource & 0x0F

    def supports(self, resource: int) -> bool:
        return (self.available_resources & int(resource)) != 0


@dataclass(frozen=True)
class XcpResponse:
    pid: int
    payload: bytes

    @property
    def is_ok(self) -> bool:
        return self.pid == Pid.RES

    @property
    def is_error(self) -> bool:
        return self.pid == Pid.ERR

    @property
    def error_code(self) -> int | None:
        if self.is_error and len(self.payload) >= 1:
            return self.payload[0]
        return None


class XcpError(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        name = ErrCode(code).name if code in ErrCode._value2member_map_ else f"0x{code:02X}"
        super().__init__(message or f"XCP error {name}")


def pad_can(data: bytes, length: int = 8) -> bytes:
    if len(data) >= length:
        return data[:length]
    return data + bytes(length - len(data))


def encode_connect(mode: int = CONNECT_MODE_NORMAL) -> bytes:
    return pad_can(bytes([Command.CONNECT, mode & 0xFF]))


def encode_disconnect() -> bytes:
    return pad_can(bytes([Command.DISCONNECT]))


def encode_get_status() -> bytes:
    return pad_can(bytes([Command.GET_STATUS]))


def encode_synch() -> bytes:
    return pad_can(bytes([Command.SYNCH]))


def encode_get_id(id_type: int = 0) -> bytes:
    return pad_can(bytes([Command.GET_ID, id_type & 0xFF]))


def encode_get_seed(mode: int, resource: int) -> bytes:
    """GET_SEED: CMD, mode (0=first / 1=remaining), resource bit."""
    return pad_can(bytes([Command.GET_SEED, mode & 0xFF, resource & 0xFF]))


def encode_unlock(remaining_key_len: int, key_chunk: bytes) -> bytes:
    """UNLOCK: CMD, remaining key length (incl. this chunk), key bytes…"""
    if remaining_key_len < 0 or remaining_key_len > 255:
        raise ValueError("UNLOCK remaining length must be 0..255")
    return bytes([Command.UNLOCK, remaining_key_len & 0xFF]) + key_chunk


def encode_set_cal_page(mode: int, segment: int, page: int) -> bytes:
    """SET_CAL_PAGE: CMD, mode, segment number, page number."""
    return pad_can(
        bytes(
            [
                Command.SET_CAL_PAGE,
                mode & 0xFF,
                segment & 0xFF,
                page & 0xFF,
            ]
        )
    )


def encode_get_cal_page(mode: int, segment: int) -> bytes:
    """GET_CAL_PAGE: CMD, access mode, segment number."""
    return pad_can(bytes([Command.GET_CAL_PAGE, mode & 0xFF, segment & 0xFF]))


def parse_get_seed_response(resp: XcpResponse) -> tuple[int, bytes]:
    """Return ``(remaining_length, seed_chunk)``. length 0 ⇒ already unlocked."""
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "GET_SEED failed")
    p = resp.payload
    if not p:
        raise ValueError("GET_SEED RES too short")
    length = p[0]
    return length, p[1:]


def parse_unlock_response(resp: XcpResponse) -> int:
    """Return current resource protection bit mask (1 = still locked)."""
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "UNLOCK failed")
    if not resp.payload:
        return 0
    return int(resp.payload[0])


def parse_get_cal_page_response(resp: XcpResponse) -> int:
    """Return active page number."""
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "GET_CAL_PAGE failed")
    p = resp.payload
    # RES, reserved, page  — some slaves omit reserved
    if len(p) >= 2:
        return int(p[1])
    if len(p) == 1:
        return int(p[0])
    raise ValueError("GET_CAL_PAGE RES too short")


def encode_set_mta(
    address: int,
    *,
    addr_ext: int = 0,
    little_endian: bool = True,
) -> bytes:
    addr = address & 0xFFFFFFFF
    addr_bytes = addr.to_bytes(4, "little" if little_endian else "big")
    # SET_MTA: CMD, reserved, reserved, addr_ext, addr[4]
    return pad_can(bytes([Command.SET_MTA, 0x00, 0x00, addr_ext & 0xFF]) + addr_bytes)


def encode_upload(size: int) -> bytes:
    return pad_can(bytes([Command.UPLOAD, size & 0xFF]))


def encode_short_upload(
    size: int,
    address: int,
    *,
    addr_ext: int = 0,
    little_endian: bool = True,
) -> bytes:
    addr = address & 0xFFFFFFFF
    addr_bytes = addr.to_bytes(4, "little" if little_endian else "big")
    # SHORT_UPLOAD: CMD, size, reserved, addr_ext, addr[4]
    return pad_can(
        bytes([Command.SHORT_UPLOAD, size & 0xFF, 0x00, addr_ext & 0xFF]) + addr_bytes
    )


def encode_download(data: bytes) -> bytes:
    """DOWNLOAD: CMD, size, data… (caller pads to CTO length)."""
    if len(data) > 255:
        raise ValueError("DOWNLOAD size must be <= 255")
    return bytes([Command.DOWNLOAD, len(data) & 0xFF]) + data


def encode_download_next(data: bytes) -> bytes:
    if len(data) > 255:
        raise ValueError("DOWNLOAD_NEXT size must be <= 255")
    return bytes([Command.DOWNLOAD_NEXT, len(data) & 0xFF]) + data


def encode_short_download(
    address: int,
    data: bytes,
    *,
    addr_ext: int = 0,
    little_endian: bool = True,
) -> bytes:
    """SHORT_DOWNLOAD: CMD, size, reserved, addr_ext, addr[4], data…"""
    if len(data) > 255:
        raise ValueError("SHORT_DOWNLOAD size must be <= 255")
    addr_bytes = (address & 0xFFFFFFFF).to_bytes(4, "little" if little_endian else "big")
    return (
        bytes([Command.SHORT_DOWNLOAD, len(data) & 0xFF, 0x00, addr_ext & 0xFF])
        + addr_bytes
        + data
    )


def parse_response(data: bytes) -> XcpResponse:
    if not data:
        raise ValueError("Empty XCP frame")
    return XcpResponse(pid=data[0], payload=data[1:])


def parse_connect_response(resp: XcpResponse) -> ConnectInfo:
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "CONNECT failed")
    p = resp.payload
    if len(p) < 7:
        raise ValueError(f"CONNECT RES too short: {p.hex()}")
    return ConnectInfo(
        resource=p[0],
        comm_mode_basic=p[1],
        max_cto=p[2],
        max_dto=p[3] | (p[4] << 8),
        protocol_version=p[5],
        transport_version=p[6],
    )


def parse_upload_data(resp: XcpResponse) -> bytes:
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "UPLOAD failed")
    return resp.payload


# --- DAQ -----------------------------------------------------------------------

# START_STOP_SYNCH modes
SS_STOP_ALL: Final = 0x00
SS_START_SELECTED: Final = 0x01
SS_STOP_SELECTED: Final = 0x02

# START_STOP_DAQ_LIST modes
SS_DAQ_STOP: Final = 0x00
SS_DAQ_START: Final = 0x01
SS_DAQ_SELECT: Final = 0x02

# SET_DAQ_LIST_MODE bits
DAQ_LIST_MODE_DEFAULT: Final = 0x00
DAQ_LIST_MODE_TIMESTAMP: Final = 0x10  # include timestamp in DTO
DAQ_LIST_MODE_PID_OFF: Final = 0x20
DAQ_LIST_MODE_DTO_CTR: Final = 0x08


def daq_identification_header_extra(identification_field_type: int) -> int:
    """Bytes after PID used by the DAQ identification field (not counting timestamp)."""
    return {0: 0, 1: 1, 2: 2, 3: 3}.get(int(identification_field_type) & 0x03, 0)


@dataclass(frozen=True)
class DaqProcessorInfo:
    properties: int
    max_daq: int
    max_event_channel: int
    min_daq: int
    daq_key_byte: int

    @property
    def identification_field_type(self) -> int:
        """0=absolute ODT, 1=relative ODT + absolute DAQ, 2/3=relative variants."""
        return self.daq_key_byte & 0x03

    @property
    def address_extension_odt(self) -> bool:
        return bool(self.daq_key_byte & 0x10)

    @property
    def address_extension_daq(self) -> bool:
        return bool(self.daq_key_byte & 0x20)


@dataclass(frozen=True)
class DaqResolutionInfo:
    granularity_odt_entry_size_daq: int
    max_odt_entry_size_daq: int
    granularity_odt_entry_size_stim: int
    max_odt_entry_size_stim: int
    timestamp_mode: int
    timestamp_ticks: int

    @property
    def timestamp_size(self) -> int:
        """0 / 1 / 2 / 4 — bytes of timestamp appended after identification field."""
        size = self.timestamp_mode & 0x07
        return size if size in (0, 1, 2, 4) else 0

    @property
    def timestamp_fixed(self) -> bool:
        return bool(self.timestamp_mode & 0x08)

    @property
    def timestamp_unit(self) -> int:
        """TIMESTAMP_MODE unit field (bits 4..7)."""
        return (self.timestamp_mode >> 4) & 0x0F


def encode_get_daq_processor_info() -> bytes:
    return pad_can(bytes([Command.GET_DAQ_PROCESSOR_INFO]))


def encode_get_daq_resolution_info() -> bytes:
    return pad_can(bytes([Command.GET_DAQ_RESOLUTION_INFO]))


def parse_daq_resolution_info(resp: XcpResponse, *, little_endian: bool = True) -> DaqResolutionInfo:
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "GET_DAQ_RESOLUTION_INFO failed")
    p = resp.payload
    if len(p) < 7:
        raise ValueError(f"GET_DAQ_RESOLUTION_INFO RES too short: {p.hex()}")
    endian = "little" if little_endian else "big"
    return DaqResolutionInfo(
        granularity_odt_entry_size_daq=p[0],
        max_odt_entry_size_daq=p[1],
        granularity_odt_entry_size_stim=p[2],
        max_odt_entry_size_stim=p[3],
        timestamp_mode=p[4],
        timestamp_ticks=int.from_bytes(p[5:7], endian),
    )


def parse_daq_processor_info(resp: XcpResponse, *, little_endian: bool = True) -> DaqProcessorInfo:
    if not resp.is_ok:
        raise XcpError(resp.error_code or ErrCode.GENERIC, "GET_DAQ_PROCESSOR_INFO failed")
    p = resp.payload
    if len(p) < 7:
        raise ValueError(f"GET_DAQ_PROCESSOR_INFO RES too short: {p.hex()}")
    endian = "little" if little_endian else "big"
    max_daq = int.from_bytes(p[1:3], endian)
    max_event = int.from_bytes(p[3:5], endian)
    return DaqProcessorInfo(
        properties=p[0],
        max_daq=max_daq,
        max_event_channel=max_event,
        min_daq=p[5],
        daq_key_byte=p[6],
    )


def encode_free_daq() -> bytes:
    return pad_can(bytes([Command.FREE_DAQ]))


def encode_alloc_daq(count: int, *, little_endian: bool = True) -> bytes:
    raw = (count & 0xFFFF).to_bytes(2, "little" if little_endian else "big")
    return pad_can(bytes([Command.ALLOC_DAQ, 0x00]) + raw)


def encode_alloc_odt(
    daq_list: int,
    odt_count: int,
    *,
    little_endian: bool = True,
) -> bytes:
    daq = (daq_list & 0xFFFF).to_bytes(2, "little" if little_endian else "big")
    return pad_can(bytes([Command.ALLOC_ODT, 0x00]) + daq + bytes([odt_count & 0xFF]))


def encode_alloc_odt_entry(
    daq_list: int,
    odt_number: int,
    entries: int,
    *,
    little_endian: bool = True,
) -> bytes:
    daq = (daq_list & 0xFFFF).to_bytes(2, "little" if little_endian else "big")
    return pad_can(
        bytes([Command.ALLOC_ODT_ENTRY, 0x00])
        + daq
        + bytes([odt_number & 0xFF, entries & 0xFF])
    )


def encode_set_daq_ptr(
    daq_list: int,
    odt_number: int,
    odt_entry: int,
    *,
    little_endian: bool = True,
) -> bytes:
    daq = (daq_list & 0xFFFF).to_bytes(2, "little" if little_endian else "big")
    return pad_can(
        bytes([Command.SET_DAQ_PTR, 0x00])
        + daq
        + bytes([odt_number & 0xFF, odt_entry & 0xFF])
    )


def encode_write_daq(
    size: int,
    address: int,
    *,
    bit_offset: int = 0xFF,
    addr_ext: int = 0,
    little_endian: bool = True,
) -> bytes:
    addr = (address & 0xFFFFFFFF).to_bytes(4, "little" if little_endian else "big")
    return pad_can(
        bytes(
            [
                Command.WRITE_DAQ,
                bit_offset & 0xFF,
                size & 0xFF,
                addr_ext & 0xFF,
            ]
        )
        + addr
    )


def encode_set_daq_list_mode(
    daq_list: int,
    event_channel: int,
    *,
    mode: int = DAQ_LIST_MODE_DEFAULT,
    prescaler: int = 1,
    priority: int = 0,
    little_endian: bool = True,
) -> bytes:
    endian = "little" if little_endian else "big"
    return pad_can(
        bytes([Command.SET_DAQ_LIST_MODE, mode & 0xFF])
        + (daq_list & 0xFFFF).to_bytes(2, endian)
        + (event_channel & 0xFFFF).to_bytes(2, endian)
        + bytes([prescaler & 0xFF, priority & 0xFF])
    )


def encode_start_stop_daq_list(
    mode: int,
    daq_list: int,
    *,
    little_endian: bool = True,
) -> bytes:
    daq = (daq_list & 0xFFFF).to_bytes(2, "little" if little_endian else "big")
    return pad_can(bytes([Command.START_STOP_DAQ_LIST, mode & 0xFF]) + daq)


def encode_start_stop_synch(mode: int) -> bytes:
    """mode: 0=stop all, 1=start selected, 2=stop selected."""
    return pad_can(bytes([Command.START_STOP_SYNCH, mode & 0xFF]))


def encode_clear_daq_list(daq_list: int, *, little_endian: bool = True) -> bytes:
    daq = (daq_list & 0xFFFF).to_bytes(2, "little" if little_endian else "big")
    return pad_can(bytes([Command.CLEAR_DAQ_LIST, 0x00]) + daq)


def is_cto_pid(pid: int) -> bool:
    """True if PID is a command-transfer response (not a DAQ DTO)."""
    return pid in (Pid.RES, Pid.ERR, Pid.EV, Pid.SERV)
