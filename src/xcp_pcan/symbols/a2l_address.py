"""A2L address resolution helpers (ECU_ADDRESS, LINK_MAP, MEMORY_SEGMENT, SYMBOL_LINK)."""

from __future__ import annotations

from dataclasses import dataclass

from xcp_pcan.symbols.types import SymbolStore, Variable


@dataclass(frozen=True)
class ResolvedAddress:
    address: int
    addr_ext: int = 0
    source: str = ""
    symbol_link: str = ""
    symbol_link_offset: int = 0


def _parse_number(tok: str) -> int | float:
    if tok.lower().startswith("0x"):
        return int(tok, 16)
    if "." in tok or "e" in tok.lower():
        return float(tok)
    return int(tok)


def _is_int_token(tok: str) -> bool:
    t = tok.lower()
    if t.startswith("0x"):
        try:
            int(t, 16)
            return True
        except ValueError:
            return False
    if t.startswith("-"):
        t = t[1:]
    return t.isdigit()


def parse_memory_segments(tokens: list[str], find_blocks) -> dict[str, tuple[int, int]]:
    """Map MEMORY_SEGMENT name → (base_address, size)."""
    out: dict[str, tuple[int, int]] = {}
    for block in find_blocks(tokens, "MEMORY_SEGMENT"):
        if len(block) < 6:
            continue
        name = block[0]
        # name LongId PrgType MemoryType Attribute address size …
        addr_tok = block[5]
        size_tok = block[6] if len(block) > 6 else "0"
        try:
            if not _is_int_token(addr_tok):
                continue
            base = int(_parse_number(addr_tok))
            size = int(_parse_number(size_tok)) if _is_int_token(size_tok) else 0
            out[name] = (base, size)
        except (ValueError, TypeError):
            continue
    return out


def parse_link_map(tokens: list[str]) -> dict[str, tuple[int, int]]:
    """Parse LINK_MAP entries → linker_name → (address, ext).

    Typical CANape / ASAP2 IF_DATA form::

        LINK_MAP \"Symbol\" 0x20000000 0x0 0 0 0 0 0 0
    """
    out: dict[str, tuple[int, int]] = {}
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].upper() == "LINK_MAP" and i + 2 < n:
            name = tokens[i + 1]
            addr_tok = tokens[i + 2]
            ext = 0
            if i + 3 < n and _is_int_token(tokens[i + 3]):
                try:
                    ext = int(_parse_number(tokens[i + 3])) & 0xFF
                except (ValueError, TypeError):
                    ext = 0
            try:
                if _is_int_token(addr_tok):
                    out[name] = (int(_parse_number(addr_tok)), ext)
            except (ValueError, TypeError):
                pass
            i += 3
            continue
        i += 1
    return out


def parse_symbol_link(block: list[str]) -> tuple[str, int] | None:
    """Return (linker_name, offset) from SYMBOL_LINK if present."""
    for i, tok in enumerate(block):
        if tok.upper() == "SYMBOL_LINK" and i + 1 < len(block):
            name = block[i + 1]
            offset = 0
            if i + 2 < len(block) and _is_int_token(block[i + 2]):
                try:
                    offset = int(_parse_number(block[i + 2]))
                except (ValueError, TypeError):
                    offset = 0
            return name, offset
    return None


def resolve_address_in_block(
    block: list[str],
    *,
    link_map: dict[str, tuple[int, int]],
    memory_segments: dict[str, tuple[int, int]],
    symbol_name: str = "",
) -> ResolvedAddress:
    """Resolve address for one MEASUREMENT / CHARACTERISTIC token block."""
    address = 0
    addr_ext = 0
    source = ""
    symbol_link = ""
    link_offset = 0

    # 1) ECU_ADDRESS / ECU_ADDRESS_EXTENSION (canonical ASAP2)
    for i, tok in enumerate(block):
        up = tok.upper()
        if up == "ECU_ADDRESS" and i + 1 < len(block) and _is_int_token(block[i + 1]):
            try:
                address = int(_parse_number(block[i + 1]))
                source = "ecu_address"
            except (ValueError, TypeError):
                pass
        if up == "ECU_ADDRESS_EXTENSION" and i + 1 < len(block) and _is_int_token(block[i + 1]):
            try:
                addr_ext = int(_parse_number(block[i + 1])) & 0xFF
            except (ValueError, TypeError):
                pass
        # Dialect: bare ADDRESS keyword
        if up == "ADDRESS" and i + 1 < len(block) and _is_int_token(block[i + 1]) and not source:
            try:
                address = int(_parse_number(block[i + 1]))
                source = "address_keyword"
            except (ValueError, TypeError):
                pass

    # 2) SYMBOL_LINK → LINK_MAP (or keep link for ELF overlay)
    sl = parse_symbol_link(block)
    if sl is not None:
        symbol_link, link_offset = sl
        if address == 0 and symbol_link in link_map:
            base, ext = link_map[symbol_link]
            address = base + link_offset
            if addr_ext == 0:
                addr_ext = ext
            source = "symbol_link+link_map"
        elif address == 0 and symbol_name and symbol_name in link_map:
            base, ext = link_map[symbol_name]
            address = base + link_offset
            if addr_ext == 0:
                addr_ext = ext
            source = "name+link_map"

    # 3) LINK_MAP by measurement/characteristic name (no SYMBOL_LINK)
    if address == 0 and symbol_name and symbol_name in link_map:
        base, ext = link_map[symbol_name]
        address = base
        if addr_ext == 0:
            addr_ext = ext
        source = "link_map"

    # 4) REF_MEMORY_SEGMENT + optional OFFSET / ADDRESS_OFFSET
    if address == 0 and memory_segments:
        ref = None
        for i, tok in enumerate(block):
            if tok.upper() != "REF_MEMORY_SEGMENT" or i + 1 >= len(block):
                continue
            candidate = block[i + 1]
            # Skip if the token is actually a LongIdentifier string that happens
            # to equal the keyword (prefer a real segment name).
            if candidate in memory_segments:
                ref = candidate
                break
            if candidate.upper() not in {
                "UBYTE",
                "SBYTE",
                "UWORD",
                "SWORD",
                "ULONG",
                "SLONG",
                "FLOAT32_IEEE",
                "FLOAT64_IEEE",
            }:
                # Unknown name — still try (segment may be defined elsewhere)
                ref = candidate
                break
        if ref and ref in memory_segments:
            base, _size = memory_segments[ref]
            offset = 0
            for key in ("OFFSET", "ADDRESS_OFFSET", "START_ADDRESS"):
                for i, tok in enumerate(block):
                    if tok.upper() == key and i + 1 < len(block) and _is_int_token(block[i + 1]):
                        try:
                            offset = int(_parse_number(block[i + 1]))
                        except (ValueError, TypeError):
                            offset = 0
                        break
            address = base + offset
            source = "memory_segment"

    return ResolvedAddress(
        address=address,
        addr_ext=addr_ext,
        source=source,
        symbol_link=symbol_link,
        symbol_link_offset=link_offset,
    )


def overlay_elf_addresses(a2l_store: SymbolStore, elf_store: SymbolStore) -> int:
    """Fill zero A2L addresses from ELF by SYMBOL_LINK or name.

    Returns how many variables were updated. Existing non-zero addresses are kept.
    """
    elf_by = elf_store.by_name()
    elf_lower = {k.lower(): v for k, v in elf_by.items()}
    updated = 0
    for var in a2l_store.variables:
        if var.address != 0:
            continue
        candidates: list[str] = []
        if var.symbol_link:
            candidates.append(var.symbol_link)
        candidates.append(var.name)
        hit: Variable | None = None
        for name in candidates:
            hit = elf_by.get(name) or elf_lower.get(name.lower())
            if hit is not None:
                break
        if hit is None or hit.address == 0:
            continue
        var.address = hit.address
        if var.size <= 0 and hit.size:
            var.size = hit.size
        var.address_source = "elf"
        if var.source == "a2l":
            var.source = "a2l+elf"
        updated += 1
    return updated


def unresolved_address_count(store: SymbolStore) -> int:
    return sum(1 for v in store.variables if v.address == 0)
