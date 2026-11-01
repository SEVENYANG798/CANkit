"""Symbol loaders (A2L / ELF)."""

from xcp_pcan.symbols.a2l_address import overlay_elf_addresses, unresolved_address_count
from xcp_pcan.symbols.a2l_loader import load_a2l
from xcp_pcan.symbols.a2l_updater import A2lUpdateReport, update_a2l_from_elf, update_a2l_text
from xcp_pcan.symbols.elf_loader import load_elf
from xcp_pcan.symbols.types import (
    Conversion,
    DataType,
    SymbolKind,
    SymbolStore,
    Variable,
    XcpTransportHints,
)

__all__ = [
    "A2lUpdateReport",
    "Conversion",
    "DataType",
    "SymbolKind",
    "SymbolStore",
    "Variable",
    "XcpTransportHints",
    "load_a2l",
    "load_elf",
    "overlay_elf_addresses",
    "unresolved_address_count",
    "update_a2l_from_elf",
    "update_a2l_text",
]
