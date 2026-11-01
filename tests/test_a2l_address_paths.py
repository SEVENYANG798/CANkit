"""A2L multi-path address resolution + ELF overlay."""

from __future__ import annotations

from pathlib import Path

from xcp_pcan.symbols.a2l_address import overlay_elf_addresses, unresolved_address_count
from xcp_pcan.symbols.a2l_loader import load_a2l
from xcp_pcan.symbols.types import DataType, SymbolKind, SymbolStore, Variable

FIX = Path(__file__).parent / "fixtures" / "address_paths.a2l"


def test_address_resolution_paths():
    progress: list[tuple[str, float]] = []
    store = load_a2l(FIX, on_progress=lambda m, f: progress.append((m, f)))
    by = store.by_name()

    assert by["DirectEcu"].address == 0x20000010
    assert by["DirectEcu"].addr_ext == 1
    assert by["DirectEcu"].address_source == "ecu_address"

    assert by["ViaSymbolLink"].address == 0x40001000
    assert by["ViaSymbolLink"].symbol_link == "Linker_Rpm"
    assert by["ViaSymbolLink"].address_source == "symbol_link+link_map"

    assert by["ViaSymbolLinkOff"].address == 0x40001004

    assert by["OnlyLinkMap"].address == 0x40002000
    assert by["OnlyLinkMap"].address_source == "link_map"

    assert by["ViaMemSeg"].address == 0x30000100
    assert by["ViaMemSeg"].address_source == "memory_segment"

    assert by["CalViaAddr"].address == 0x2000ABCD
    assert by["CalViaAddr"].address_source == "address_keyword"
    assert by["CalViaAddr"].writable is True

    assert by["NeedsElf"].address == 0
    assert by["NeedsElf"].symbol_link == "ElfOnlySymbol"
    assert unresolved_address_count(store) >= 1
    assert progress and progress[-1][1] == 1.0


def test_overlay_elf_addresses():
    store = load_a2l(FIX)
    elf = SymbolStore(
        variables=[
            Variable(
                name="ElfOnlySymbol",
                address=0x50000001,
                size=2,
                datatype=DataType.UWORD,
                kind=SymbolKind.ELF,
                source="elf",
            )
        ]
    )
    n = overlay_elf_addresses(store, elf)
    assert n == 1
    assert store.by_name()["NeedsElf"].address == 0x50000001
    assert store.by_name()["NeedsElf"].address_source == "elf"
    # Existing A2L addresses must not be overwritten
    assert store.by_name()["DirectEcu"].address == 0x20000010
