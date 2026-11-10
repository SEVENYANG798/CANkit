"""ELF symbol loader using pyelftools (symbol table + basic DWARF types)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from xcp_pcan.symbols import elf_cache
from xcp_pcan.symbols.types import (
    Conversion,
    DataType,
    SymbolKind,
    SymbolStore,
    Variable,
    datatype_size,
    parse_datatype,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]


def _report(progress: ProgressCallback | None, message: str, fraction: float) -> None:
    from xcp_pcan.util.cancel import report_progress

    report_progress(progress, message, fraction)


def _datatype_from_size(size: int) -> DataType:
    return {
        1: DataType.UBYTE,
        2: DataType.UWORD,
        4: DataType.ULONG,
        8: DataType.A_UINT64,
    }.get(size, DataType.UNKNOWN)


def _dwarf_type_size(die, dwarfinfo, depth: int = 0) -> int | None:
    if die is None or depth > 16:
        return None
    if "DW_AT_byte_size" in die.attributes:
        return int(die.attributes["DW_AT_byte_size"].value)
    if "DW_AT_type" in die.attributes:
        try:
            type_die = die.get_DIE_from_attribute("DW_AT_type")
            return _dwarf_type_size(type_die, dwarfinfo, depth + 1)
        except Exception:  # noqa: BLE001
            return None
    return None


def _dwarf_sizes(elffile: ELFFile) -> dict[str, int]:
    """Map variable name -> byte size from DWARF if available."""
    sizes: dict[str, int] = {}
    if not elffile.has_dwarf_info():
        return sizes
    try:
        dwarfinfo = elffile.get_dwarf_info()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read DWARF")
        return sizes

    for cu in dwarfinfo.iter_CUs():
        try:
            for die in cu.iter_DIEs():
                if die.tag not in ("DW_TAG_variable",):
                    continue
                if "DW_AT_name" not in die.attributes:
                    continue
                name = die.attributes["DW_AT_name"].value
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                size = _dwarf_type_size(die, dwarfinfo)
                if size:
                    sizes[name] = size
        except Exception:  # noqa: BLE001
            logger.debug("Skipping CU due to DWARF parse issue", exc_info=True)
    return sizes


def load_elf(
    path: str | Path,
    *,
    on_progress: ProgressCallback | None = None,
    use_cache: bool = True,
) -> SymbolStore:
    path = Path(path)
    if use_cache:
        cached = elf_cache.try_load(path)
        if cached is not None:
            _report(
                on_progress,
                f"Loaded {path.name} from cache ({len(cached.variables)} symbols)",
                1.0,
            )
            logger.info("ELF cache hit: %s (%d symbols)", path, len(cached.variables))
            return cached

    variables: list[Variable] = []
    _report(on_progress, f"Reading {path.name}…", 0.05)
    with path.open("rb") as f:
        elffile = ELFFile(f)
        _report(on_progress, "Scanning DWARF sizes…", 0.2)
        dwarf_sizes = _dwarf_sizes(elffile)
        _report(on_progress, "Reading symbol tables…", 0.45)

        sym_i = 0
        for section in elffile.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for sym in section.iter_symbols():
                sym_i += 1
                if on_progress is not None and sym_i % 5000 == 0:
                    _report(
                        on_progress,
                        f"Reading symbol tables… ({sym_i})",
                        min(0.85, 0.45 + 0.001 * (sym_i // 5000)),
                    )
                name = sym.name
                if not name or name.startswith("$"):
                    continue
                # STT_OBJECT = 1 (data object)
                if sym["st_info"]["type"] not in ("STT_OBJECT", "STT_NOTYPE"):
                    continue
                addr = int(sym["st_value"])
                size = int(sym["st_size"]) or dwarf_sizes.get(name, 0)
                if addr == 0 and size == 0:
                    continue
                # Skip undefined
                if sym["st_shndx"] == "SHN_UNDEF":
                    continue
                if size <= 0:
                    size = dwarf_sizes.get(name, 4)
                datatype = _datatype_from_size(size)
                # Prefer known dwarf mapping name hints
                if name.endswith("_f32") or name.endswith("Float"):
                    datatype = parse_datatype("FLOAT32_IEEE")
                    size = datatype_size(datatype)
                variables.append(
                    Variable(
                        name=name,
                        address=addr,
                        size=size,
                        datatype=datatype,
                        conversion=Conversion(is_identity=True),
                        source="elf",
                        kind=SymbolKind.ELF,
                        writable=True,
                        address_source="elf",
                    )
                )

    _report(on_progress, "Deduplicating…", 0.9)
    # Deduplicate by name (prefer larger size / first with address)
    by_name: dict[str, Variable] = {}
    for v in variables:
        prev = by_name.get(v.name)
        if prev is None or (prev.address == 0 and v.address != 0):
            by_name[v.name] = v
    out = sorted(by_name.values(), key=lambda v: v.name.lower())
    store = SymbolStore(variables=out, source_path=str(path))
    if use_cache:
        elf_cache.save(path, store)
        _report(on_progress, f"Done — {len(out)} symbols (cached)", 1.0)
    else:
        _report(on_progress, f"Done — {len(out)} symbols", 1.0)
    return store
