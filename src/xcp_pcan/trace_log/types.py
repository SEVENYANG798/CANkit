"""Normalized trace frame for ASC / BLF / CSV viewers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TraceFormat(str, Enum):
    ASC = "asc"
    BLF = "blf"
    CSV = "csv"


@dataclass(slots=True)
class TraceFrame:
    index: int
    timestamp: float
    arbitration_id: int
    data: bytes
    is_extended: bool = False
    is_fd: bool = False
    is_rx: bool = True
    is_error: bool = False
    channel: str = ""
    name: str = ""

    @property
    def dlc(self) -> int:
        return len(self.data)

    def id_hex(self) -> str:
        width = 8 if self.is_extended else 3
        return f"0x{self.arbitration_id:0{width}X}"

    def data_hex(self) -> str:
        return self.data.hex(" ").upper()
