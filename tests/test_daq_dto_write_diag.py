"""DAQ DTO identification/timestamp parsing and write failure classification."""

from __future__ import annotations

from xcp_pcan.symbols.types import DataType, Variable
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.daq import pack_variables, parse_dto_header
from xcp_pcan.xcp.protocol import XcpError
from xcp_pcan.xcp.write_diag import WriteFailureKind, classify_verify_mismatch, classify_xcp_error


def _var(name: str, size: int) -> Variable:
    return Variable(name=name, address=0x1000, size=size, datatype=DataType.UBYTE)


def test_header_extra_sizes():
    assert proto.daq_identification_header_extra(0) == 0
    assert proto.daq_identification_header_extra(1) == 1
    assert proto.daq_identification_header_extra(2) == 2
    assert proto.daq_identification_header_extra(3) == 3


def test_pack_with_timestamp_reduces_payload():
    layout = pack_variables(
        [_var("a", 2), _var("b", 2)],
        max_payload=7,
        identification_field_type=1,  # 1 byte DAQ → usable = 7-1-2ts = 4
        timestamp_size=2,
    )
    # usable=4 → a+b fit in one ODT
    assert layout.timestamp_size == 2
    assert layout.header_size == 1 + 1 + 2
    assert len(layout.odts) == 1


def test_parse_dto_type0_and_type1():
    # type 0: PID=ODT, data follows
    r = parse_dto_header(bytes([0x02, 0xAA, 0xBB]), identification_field_type=0, daq_list=0)
    assert r == (2, 1, None)

    # type 1: PID=rel ODT, byte1=DAQ
    r = parse_dto_header(
        bytes([0x01, 0x00, 0x11, 0x22]),
        identification_field_type=1,
        daq_list=0,
    )
    assert r == (1, 2, None)
    assert (
        parse_dto_header(bytes([0x01, 0x05, 0x11]), identification_field_type=1, daq_list=0)
        is None
    )


def test_parse_dto_type2_type3_with_timestamp():
    # type 2: PID + WORD DAQ LE
    frame = bytes([0x00, 0x03, 0x00, 0xDE, 0xAD, 0x01, 0x02])
    r = parse_dto_header(
        frame,
        identification_field_type=2,
        daq_list=3,
        little_endian=True,
        timestamp_size=2,
    )
    assert r is not None
    odt, off, ts = r
    assert odt == 0
    assert ts == 0xADDE
    assert frame[off:] == b"\x01\x02"

    # type 3: PID + fill + WORD DAQ
    frame = bytes([0x01, 0x00, 0x04, 0x00, 0x12, 0x34, 0x56])
    r = parse_dto_header(
        frame,
        identification_field_type=3,
        daq_list=4,
        little_endian=True,
        timestamp_size=2,
    )
    assert r is not None
    odt, off, ts = r
    assert odt == 1
    assert ts == 0x3412
    assert frame[off:] == b"\x56"


def test_parse_daq_resolution_info():
    # RES + gran_daq + max_daq + gran_stim + max_stim + ts_mode + ticks
    data = bytes([0xFF, 0x01, 0x07, 0x01, 0x07, 0x02, 0x10, 0x00])
    info = proto.parse_daq_resolution_info(proto.parse_response(data), little_endian=True)
    assert info.timestamp_size == 2
    assert info.timestamp_ticks == 0x0010


def test_classify_write_errors():
    locked = classify_xcp_error(XcpError(proto.ErrCode.ACCESS_LOCKED, "locked"))
    assert locked.kind == WriteFailureKind.LOCKED
    assert "Unlock" in locked.hint

    page = classify_xcp_error(XcpError(proto.ErrCode.PAGE_NOT_VALID))
    assert page.kind == WriteFailureKind.PAGE

    ro = classify_xcp_error(XcpError(proto.ErrCode.WRITE_PROTECTED))
    assert ro.kind == WriteFailureKind.READONLY

    to = classify_xcp_error(TimeoutError("CTO timeout"))
    assert to.kind == WriteFailureKind.TIMEOUT

    mismatch = classify_verify_mismatch(
        name="Cal", written=b"\x01\x02", readback=b"\x00\x00", written_phys=1, readback_phys=0
    )
    assert mismatch.kind == WriteFailureKind.VERIFY
    assert "Cal" in mismatch.detail


def test_encode_set_daq_list_mode_timestamp():
    frame = proto.encode_set_daq_list_mode(
        0, 1, mode=proto.DAQ_LIST_MODE_TIMESTAMP, prescaler=1
    )
    assert frame[1] == proto.DAQ_LIST_MODE_TIMESTAMP
