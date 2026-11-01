"""DAQ packing and protocol helper tests."""

from xcp_pcan.symbols.types import DataType, Variable
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.daq import pack_variables


def _var(name: str, size: int, addr: int = 0x1000) -> Variable:
    return Variable(
        name=name,
        address=addr,
        size=size,
        datatype={1: DataType.UBYTE, 2: DataType.UWORD, 4: DataType.ULONG}.get(
            size, DataType.UNKNOWN
        ),
    )


def test_pack_variables_into_odts():
    # max_payload=7 (Classic CAN DTO without extra ID field)
    layout = pack_variables(
        [_var("a", 2), _var("b", 2), _var("c", 4), _var("d", 1)],
        max_payload=7,
        identification_field_type=0,
    )
    # a(2)+b(2)=4, +c(4)=8>7 → ODT0: a,b; then c(4)+d(1)=5 → ODT1: c,d
    assert len(layout.odts) == 2
    assert [e.variable.name for e in layout.odts[0].entries] == ["a", "b"]
    assert [e.variable.name for e in layout.odts[1].entries] == ["c", "d"]


def test_pack_rejects_oversized_variable():
    import pytest

    with pytest.raises(ValueError, match="exceeds"):
        pack_variables([_var("big", 8)], max_payload=7, identification_field_type=0)


def test_encode_daq_commands():
    frame = proto.encode_alloc_daq(1, little_endian=True)
    assert frame[0] == proto.Command.ALLOC_DAQ
    assert frame[2:4] == b"\x01\x00"

    frame = proto.encode_write_daq(2, 0x20000000, little_endian=True)
    assert frame[0] == proto.Command.WRITE_DAQ
    assert frame[2] == 2
    assert frame[4:8] == (0x20000000).to_bytes(4, "little")

    frame = proto.encode_set_daq_list_mode(0, 1, prescaler=2, little_endian=True)
    assert frame[0] == proto.Command.SET_DAQ_LIST_MODE
    assert frame[4:6] == (1).to_bytes(2, "little")
    assert frame[6] == 2


def test_parse_daq_processor_info():
    # RES + properties + max_daq + max_event + min_daq + daq_key_byte
    data = bytes([0xFF, 0x01, 0x04, 0x00, 0x08, 0x00, 0x00, 0x00])
    info = proto.parse_daq_processor_info(proto.parse_response(data), little_endian=True)
    assert info.max_daq == 4
    assert info.max_event_channel == 8
    assert info.identification_field_type == 0


def test_is_cto_pid():
    assert proto.is_cto_pid(0xFF)
    assert proto.is_cto_pid(0xFE)
    assert not proto.is_cto_pid(0x00)
    assert not proto.is_cto_pid(0x05)
