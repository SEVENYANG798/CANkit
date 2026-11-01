"""Datatype decode / conversion tests."""

from xcp_pcan.symbols.types import Conversion, DataType, decode_raw, physical_value


def test_decode_uword_le():
    assert decode_raw(bytes([0x34, 0x12]), DataType.UWORD, True) == 0x1234


def test_decode_float_le():
    import struct

    raw = struct.pack("<f", 3.5)
    assert abs(decode_raw(raw, DataType.FLOAT32_IEEE, True) - 3.5) < 1e-6


def test_physical_identity():
    assert physical_value(42, Conversion(is_identity=True)) == 42


def test_physical_linear():
    c = Conversion(factor=0.5, offset=10.0, is_identity=False)
    assert physical_value(20, c) == 20.0
