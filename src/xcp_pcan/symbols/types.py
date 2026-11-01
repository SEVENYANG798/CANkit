"""Unified variable / conversion model."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DataType(str, Enum):
    UBYTE = "UBYTE"
    SBYTE = "SBYTE"
    UWORD = "UWORD"
    SWORD = "SWORD"
    ULONG = "ULONG"
    SLONG = "SLONG"
    FLOAT32_IEEE = "FLOAT32_IEEE"
    FLOAT64_IEEE = "FLOAT64_IEEE"
    A_UINT64 = "A_UINT64"
    A_INT64 = "A_INT64"
    UNKNOWN = "UNKNOWN"


_SIZE: dict[DataType, int] = {
    DataType.UBYTE: 1,
    DataType.SBYTE: 1,
    DataType.UWORD: 2,
    DataType.SWORD: 2,
    DataType.ULONG: 4,
    DataType.SLONG: 4,
    DataType.FLOAT32_IEEE: 4,
    DataType.FLOAT64_IEEE: 8,
    DataType.A_UINT64: 8,
    DataType.A_INT64: 8,
    DataType.UNKNOWN: 1,
}


def datatype_size(dt: DataType) -> int:
    return _SIZE.get(dt, 1)


def parse_datatype(name: str) -> DataType:
    key = name.strip().upper().replace("-", "_")
    aliases = {
        "UINT8": DataType.UBYTE,
        "INT8": DataType.SBYTE,
        "UINT16": DataType.UWORD,
        "INT16": DataType.SWORD,
        "UINT32": DataType.ULONG,
        "INT32": DataType.SLONG,
        "FLOAT32": DataType.FLOAT32_IEEE,
        "FLOAT64": DataType.FLOAT64_IEEE,
        "UBYTE": DataType.UBYTE,
        "SBYTE": DataType.SBYTE,
        "UWORD": DataType.UWORD,
        "SWORD": DataType.SWORD,
        "ULONG": DataType.ULONG,
        "SLONG": DataType.SLONG,
        "FLOAT32_IEEE": DataType.FLOAT32_IEEE,
        "FLOAT64_IEEE": DataType.FLOAT64_IEEE,
        "A_UINT64": DataType.A_UINT64,
        "A_INT64": DataType.A_INT64,
    }
    return aliases.get(key, DataType.UNKNOWN)


@dataclass
class Conversion:
    """Linear: phys = (raw * factor) + offset. Identity if factor=1 offset=0."""

    name: str = "NO_COMPU_METHOD"
    factor: float = 1.0
    offset: float = 0.0
    unit: str = ""
    is_identity: bool = True


class SymbolKind(str, Enum):
    MEASUREMENT = "measurement"
    CHARACTERISTIC = "characteristic"
    ELF = "elf"


@dataclass
class Variable:
    name: str
    address: int
    size: int
    datatype: DataType = DataType.UNKNOWN
    addr_ext: int = 0
    unit: str = ""
    conversion: Conversion = field(default_factory=Conversion)
    source: str = ""  # "a2l" | "elf" | "a2l+elf"
    comment: str = ""
    kind: SymbolKind = SymbolKind.MEASUREMENT
    writable: bool = False  # True only for scalar VALUE CHARACTERISTIC
    characteristic_type: str = ""  # VALUE | CURVE | MAP | VAL_BLK | …
    symbol_link: str = ""  # ASAP2 SYMBOL_LINK linker name (if any)
    address_source: str = ""  # ecu_address | link_map | memory_segment | elf | …


@dataclass
class XcpTransportHints:
    """Optional transport parameters extracted from A2L IF_DATA XCP."""

    master_can_id: int | None = None
    slave_can_id: int | None = None
    bitrate: int | None = None
    data_bitrate: int | None = None
    fd: bool | None = None

    def any_set(self) -> bool:
        return any(
            v is not None
            for v in (
                self.master_can_id,
                self.slave_can_id,
                self.bitrate,
                self.data_bitrate,
                self.fd,
            )
        )


@dataclass
class SymbolStore:
    variables: list[Variable] = field(default_factory=list)
    source_path: str = ""
    transport: XcpTransportHints | None = None


    def by_name(self) -> dict[str, Variable]:
        return {v.name: v for v in self.variables}

    def search(self, query: str) -> list[Variable]:
        """Filter symbols.

        - Empty query: all symbols
        - Single token: substring match (case-insensitive)
        - Multiple names (newline / comma / semicolon / whitespace separated):
          exact name match (case-insensitive), preserving paste order
        """
        names = parse_symbol_name_list(query)
        if not names:
            return list(self.variables)
        if len(names) == 1:
            q = names[0].lower()
            return [v for v in self.variables if q in v.name.lower()]

        by_lower = {v.name.lower(): v for v in self.variables}
        found: list[Variable] = []
        seen: set[str] = set()
        for name in names:
            key = name.lower()
            if key in seen:
                continue
            var = by_lower.get(key)
            if var is not None:
                found.append(var)
                seen.add(key)
        return found

    def missing_names(self, query: str) -> list[str]:
        """Return pasted names that are not in the store (multi-name queries only)."""
        names = parse_symbol_name_list(query)
        if len(names) <= 1:
            return []
        known = {v.name.lower() for v in self.variables}
        missing: list[str] = []
        seen: set[str] = set()
        for name in names:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            if key not in known:
                missing.append(name)
        return missing


def parse_symbol_name_list(text: str) -> list[str]:
    """Split pasted symbol names on newlines, commas, or semicolons."""
    if not text or not text.strip():
        return []
    # Normalize separators to newlines, then split
    normalized = text.replace(",", "\n").replace(";", "\n")
    parts = [p.strip() for p in normalized.splitlines()]
    # Also split remaining whitespace-only multi-token lines if a line has spaces
    # but keep names that legitimately contain no spaces (typical C symbols).
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        # If the line looks like several C identifiers separated by spaces/tabs
        if " " in part or "\t" in part:
            out.extend(tok for tok in part.replace("\t", " ").split(" ") if tok)
        else:
            out.append(part)
    return out


def decode_raw(
    data: bytes,
    datatype: DataType,
    little_endian: bool = True,
) -> int | float | None:
    if not data:
        return None
    endian = "<" if little_endian else ">"
    try:
        if datatype == DataType.UBYTE:
            return data[0]
        if datatype == DataType.SBYTE:
            return struct.unpack_from(f"{endian}b", data, 0)[0]
        if datatype == DataType.UWORD:
            return struct.unpack_from(f"{endian}H", data, 0)[0]
        if datatype == DataType.SWORD:
            return struct.unpack_from(f"{endian}h", data, 0)[0]
        if datatype == DataType.ULONG:
            return struct.unpack_from(f"{endian}I", data, 0)[0]
        if datatype == DataType.SLONG:
            return struct.unpack_from(f"{endian}i", data, 0)[0]
        if datatype == DataType.FLOAT32_IEEE:
            return struct.unpack_from(f"{endian}f", data, 0)[0]
        if datatype == DataType.FLOAT64_IEEE:
            return struct.unpack_from(f"{endian}d", data, 0)[0]
        if datatype == DataType.A_UINT64:
            return struct.unpack_from(f"{endian}Q", data, 0)[0]
        if datatype == DataType.A_INT64:
            return struct.unpack_from(f"{endian}q", data, 0)[0]
        # Fallback: unsigned integer from available bytes
        return int.from_bytes(data, "little" if little_endian else "big", signed=False)
    except struct.error:
        return None


def physical_value(raw: int | float | None, conversion: Conversion) -> Any:
    if raw is None:
        return None
    if conversion.is_identity:
        return raw
    try:
        return float(raw) * conversion.factor + conversion.offset
    except (TypeError, ValueError):
        return raw


def physical_to_raw(phys: float | int, conversion: Conversion) -> float:
    """Inverse of physical_value for linear / identity conversions."""
    if conversion.is_identity or conversion.factor == 0:
        return float(phys)
    return (float(phys) - conversion.offset) / conversion.factor


def encode_raw(
    value: int | float,
    datatype: DataType,
    size: int,
    little_endian: bool = True,
) -> bytes:
    """Pack a numeric value into ``size`` bytes for DOWNLOAD."""
    endian = "<" if little_endian else ">"
    if datatype == DataType.UBYTE:
        raw = struct.pack(f"{endian}B", int(value) & 0xFF)
    elif datatype == DataType.SBYTE:
        raw = struct.pack(f"{endian}b", int(value))
    elif datatype == DataType.UWORD:
        raw = struct.pack(f"{endian}H", int(value) & 0xFFFF)
    elif datatype == DataType.SWORD:
        raw = struct.pack(f"{endian}h", int(value))
    elif datatype == DataType.ULONG:
        raw = struct.pack(f"{endian}I", int(value) & 0xFFFFFFFF)
    elif datatype == DataType.SLONG:
        raw = struct.pack(f"{endian}i", int(value))
    elif datatype == DataType.FLOAT32_IEEE:
        raw = struct.pack(f"{endian}f", float(value))
    elif datatype == DataType.FLOAT64_IEEE:
        raw = struct.pack(f"{endian}d", float(value))
    elif datatype == DataType.A_UINT64:
        raw = struct.pack(f"{endian}Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    elif datatype == DataType.A_INT64:
        raw = struct.pack(f"{endian}q", int(value))
    else:
        n = max(1, size)
        raw = int(value).to_bytes(n, "little" if little_endian else "big", signed=False)
    if len(raw) < size:
        raw = raw + bytes(size - len(raw))
    return raw[:size]
