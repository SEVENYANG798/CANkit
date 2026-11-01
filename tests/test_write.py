"""DOWNLOAD / encode helpers."""

from xcp_pcan.symbols.types import (
    Conversion,
    DataType,
    encode_raw,
    physical_to_raw,
)
from xcp_pcan.xcp import protocol as proto


def test_encode_download():
    frame = proto.encode_download(b"\x12\x34")
    assert frame[0] == proto.Command.DOWNLOAD
    assert frame[1] == 2
    assert frame[2:4] == b"\x12\x34"


def test_encode_short_download():
    frame = proto.encode_short_download(0x20001000, b"\xAB\xCD", little_endian=True)
    assert frame[0] == proto.Command.SHORT_DOWNLOAD
    assert frame[1] == 2
    assert frame[4:8] == (0x20001000).to_bytes(4, "little")
    assert frame[8:10] == b"\xAB\xCD"


def test_physical_to_raw_linear():
    c = Conversion(factor=0.1, offset=0.0, is_identity=False)
    assert abs(physical_to_raw(25.0, c) - 250.0) < 1e-9


def test_encode_raw_uword():
    assert encode_raw(0x1234, DataType.UWORD, 2, True) == b"\x34\x12"
