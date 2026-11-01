"""Map C / DWARF base types to ASAP2 MEASUREMENT data types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class A2lTypeInfo:
    a2l_dtype: str  # UBYTE, SBYTE, UWORD, ...
    record_layout: str
    compu_method: str
    lower: str
    upper: str
    format_str: str
    size: int


# size-based fallback when DWARF type name is missing
SIZE_FALLBACK: dict[int, A2lTypeInfo] = {
    1: A2lTypeInfo("UBYTE", "Record_UBYTE", "CM_uint8", "0", "255", "%3.0", 1),
    2: A2lTypeInfo("UWORD", "Record_UWORD", "CM_uint16", "0", "65535", "%5.0", 2),
    4: A2lTypeInfo("ULONG", "Record_ULONG", "CM_uint32", "0", "4294967295", "%10.0", 4),
    8: A2lTypeInfo("FLOAT64_IEEE", "Record_FLOAT64_IEEE", "CM_float64", "-1.7e308", "1.7e308", "%15.10", 8),
}

# DWARF encoding (DW_ATE_*)
DW_ATE_address = 0x01
DW_ATE_boolean = 0x02
DW_ATE_complex_float = 0x03
DW_ATE_float = 0x04
DW_ATE_signed = 0x05
DW_ATE_signed_char = 0x06
DW_ATE_unsigned = 0x07
DW_ATE_unsigned_char = 0x08


def from_encoding(encoding: int | None, byte_size: int | None) -> A2lTypeInfo | None:
    if byte_size is None:
        return None
    if encoding == DW_ATE_boolean:
        return A2lTypeInfo("UBYTE", "Record_BOOLEAN", "CM_boolean", "0", "1", "%1.0", 1)
    if encoding in (DW_ATE_float, DW_ATE_complex_float):
        if byte_size == 4:
            return A2lTypeInfo(
                "FLOAT32_IEEE", "Record_FLOAT32_IEEE", "CM_float32",
                "-3.4e38", "3.4e38", "%8.6", 4,
            )
        if byte_size == 8:
            return A2lTypeInfo(
                "FLOAT64_IEEE", "Record_FLOAT64_IEEE", "CM_float64",
                "-1.7e308", "1.7e308", "%15.10", 8,
            )
    if encoding in (DW_ATE_signed, DW_ATE_signed_char):
        return {
            1: A2lTypeInfo("SBYTE", "Record_SBYTE", "CM_sint8", "-128", "127", "%4.0", 1),
            2: A2lTypeInfo("SWORD", "Record_SWORD", "CM_sint16", "-32768", "32767", "%6.0", 2),
            4: A2lTypeInfo("SLONG", "Record_SLONG", "CM_sint32", "-2147483648", "2147483647", "%11.0", 4),
            8: A2lTypeInfo("A_INT64", "Record_A_INT64", "CM_sint64", "-9223372036854775808", "9223372036854775807", "%20.0", 8),
        }.get(byte_size)
    if encoding in (DW_ATE_unsigned, DW_ATE_unsigned_char, DW_ATE_address):
        return {
            1: A2lTypeInfo("UBYTE", "Record_UBYTE", "CM_uint8", "0", "255", "%3.0", 1),
            2: A2lTypeInfo("UWORD", "Record_UWORD", "CM_uint16", "0", "65535", "%5.0", 2),
            4: A2lTypeInfo("ULONG", "Record_ULONG", "CM_uint32", "0", "4294967295", "%10.0", 4),
            8: A2lTypeInfo("A_UINT64", "Record_A_UINT64", "CM_uint64", "0", "18446744073709551615", "%20.0", 8),
        }.get(byte_size)
    return SIZE_FALLBACK.get(byte_size)


def type_for_object_size(size: int) -> A2lTypeInfo:
    """Fast mapping used when only the symbol size is known."""
    if size in SIZE_FALLBACK:
        return SIZE_FALLBACK[size]
    if size <= 0:
        return SIZE_FALLBACK[1]
    # Opaque object: one UBYTE measurement plus ARRAY_SIZE = size.
    return A2lTypeInfo("UBYTE", "Record_UBYTE", "CM_uint8", "0", "255", "%3.0", 1)


def from_typename(name: str | None, byte_size: int | None) -> A2lTypeInfo | None:
    if not name:
        return SIZE_FALLBACK.get(byte_size) if byte_size else None
    n = name.lower().replace(" ", "")
    # common C / AUTOSAR aliases
    aliases = {
        "bool": (DW_ATE_boolean, 1),
        "_bool": (DW_ATE_boolean, 1),
        "boolean": (DW_ATE_boolean, 1),
        "char": (DW_ATE_signed_char, 1),
        "signedchar": (DW_ATE_signed_char, 1),
        "unsignedchar": (DW_ATE_unsigned_char, 1),
        "uint8": (DW_ATE_unsigned, 1),
        "uint8_t": (DW_ATE_unsigned, 1),
        "sint8": (DW_ATE_signed, 1),
        "int8": (DW_ATE_signed, 1),
        "int8_t": (DW_ATE_signed, 1),
        "short": (DW_ATE_signed, 2),
        "signedshort": (DW_ATE_signed, 2),
        "unsignedshort": (DW_ATE_unsigned, 2),
        "uint16": (DW_ATE_unsigned, 2),
        "uint16_t": (DW_ATE_unsigned, 2),
        "sint16": (DW_ATE_signed, 2),
        "int16": (DW_ATE_signed, 2),
        "int16_t": (DW_ATE_signed, 2),
        "int": (DW_ATE_signed, 4),
        "signedint": (DW_ATE_signed, 4),
        "unsignedint": (DW_ATE_unsigned, 4),
        "long": (DW_ATE_signed, 4),
        "signedlong": (DW_ATE_signed, 4),
        "unsignedlong": (DW_ATE_unsigned, 4),
        "uint32": (DW_ATE_unsigned, 4),
        "uint32_t": (DW_ATE_unsigned, 4),
        "sint32": (DW_ATE_signed, 4),
        "int32": (DW_ATE_signed, 4),
        "int32_t": (DW_ATE_signed, 4),
        "longlong": (DW_ATE_signed, 8),
        "unsignedlonglong": (DW_ATE_unsigned, 8),
        "uint64": (DW_ATE_unsigned, 8),
        "uint64_t": (DW_ATE_unsigned, 8),
        "sint64": (DW_ATE_signed, 8),
        "int64": (DW_ATE_signed, 8),
        "int64_t": (DW_ATE_signed, 8),
        "float": (DW_ATE_float, 4),
        "float32": (DW_ATE_float, 4),
        "double": (DW_ATE_float, 8),
        "float64": (DW_ATE_float, 8),
    }
    if n in aliases:
        enc, sz = aliases[n]
        return from_encoding(enc, byte_size or sz)
    if "float" in n and (byte_size or 4) == 4:
        return from_encoding(DW_ATE_float, 4)
    if "double" in n or ("float" in n and byte_size == 8):
        return from_encoding(DW_ATE_float, 8)
    return SIZE_FALLBACK.get(byte_size) if byte_size else None


STANDARD_COMPU_METHODS = [
    ("CM_boolean", "%1.0", ""),
    ("CM_uint8", "%3.0", ""),
    ("CM_sint8", "%4.0", ""),
    ("CM_uint16", "%5.0", ""),
    ("CM_sint16", "%6.0", ""),
    ("CM_uint32", "%10.0", ""),
    ("CM_sint32", "%11.0", ""),
    ("CM_uint64", "%20.0", ""),
    ("CM_sint64", "%20.0", ""),
    ("CM_float32", "%8.6", ""),
    ("CM_float64", "%15.10", ""),
]

STANDARD_RECORD_LAYOUTS = [
    ("Record_BOOLEAN", "UBYTE"),
    ("Record_UBYTE", "UBYTE"),
    ("Record_SBYTE", "SBYTE"),
    ("Record_UWORD", "UWORD"),
    ("Record_SWORD", "SWORD"),
    ("Record_ULONG", "ULONG"),
    ("Record_SLONG", "SLONG"),
    ("Record_A_UINT64", "A_UINT64"),
    ("Record_A_INT64", "A_INT64"),
    ("Record_FLOAT32_IEEE", "FLOAT32_IEEE"),
    ("Record_FLOAT64_IEEE", "FLOAT64_IEEE"),
]
