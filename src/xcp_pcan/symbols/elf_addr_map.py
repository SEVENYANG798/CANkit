"""Build a linker-name → address map from ELF (symtab + optional DWARF members).

Green Hills / MICROSAR A2Ls often use SYMBOL_LINK names like::

    Rte_CalprmRom_Foo_GROUP.Member_Val

while the ELF symtab only has::

    _Rte_CalprmRom_Foo_GROUP
    _Rte_CalprmRom_Foo_GROUP..D.3A.5Cpath...

Member addresses are recovered via DWARF ``DW_TAG_member`` offsets when possible.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]


def _report(progress: ProgressCallback | None, message: str, fraction: float) -> None:
    from xcp_pcan.util.cancel import report_progress

    report_progress(progress, message, fraction)


def _yield_gil() -> None:
    time.sleep(0.002)


def normalize_symbol_name(name: str) -> str:
    """Strip GHS path suffix and leading underscores."""
    if not name:
        return name
    if ".." in name:
        name = name.split("..", 1)[0]
    while name.startswith("_"):
        name = name[1:]
    return name


def _die_name(die) -> str | None:
    if "DW_AT_name" not in die.attributes:
        return None
    name = die.attributes["DW_AT_name"].value
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")
    return name


def _member_offset(die) -> int | None:
    attr = die.attributes.get("DW_AT_data_member_location")
    if attr is None:
        return None
    val = attr.value
    if isinstance(val, int):
        return int(val)
    # exprloc: commonly [DW_OP_plus_uconst, offset]
    if isinstance(val, list) and val:
        # pyelftools may give list of ints or Operation objects
        try:
            if len(val) >= 2 and int(val[0]) in (0x23,):  # DW_OP_plus_uconst
                return int(val[1])
        except Exception:  # noqa: BLE001
            pass
        try:
            # Operation objects with .op_name
            op0 = val[0]
            if getattr(op0, "op_name", "") == "DW_OP_plus_uconst":
                return int(val[1])
        except Exception:  # noqa: BLE001
            pass
    return None


def _unwrap_type(die, depth: int = 0):
    if die is None or depth > 12:
        return None
    tag = die.tag
    if tag in (
        "DW_TAG_structure_type",
        "DW_TAG_class_type",
        "DW_TAG_union_type",
    ):
        return die
    if tag in (
        "DW_TAG_typedef",
        "DW_TAG_const_type",
        "DW_TAG_volatile_type",
        "DW_TAG_restrict_type",
    ):
        if "DW_AT_type" not in die.attributes:
            return None
        try:
            return _unwrap_type(die.get_DIE_from_attribute("DW_AT_type"), depth + 1)
        except Exception:  # noqa: BLE001
            return None
    if "DW_AT_type" in die.attributes:
        try:
            return _unwrap_type(die.get_DIE_from_attribute("DW_AT_type"), depth + 1)
        except Exception:  # noqa: BLE001
            return None
    return None


def _iter_struct_members(struct_die) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if struct_die is None or not struct_die.has_children:
        return out
    try:
        for child in struct_die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            name = _die_name(child)
            if not name:
                continue
            off = _member_offset(child)
            if off is None:
                continue
            out.append((name, off))
    except Exception:  # noqa: BLE001
        return out
    return out


def _add_alias(out: dict[str, int], name: str, addr: int) -> None:
    if not name or addr == 0:
        return
    # Prefer first non-zero; don't overwrite with same
    if name not in out:
        out[name] = addr
    norm = normalize_symbol_name(name)
    if norm and norm not in out:
        out[norm] = addr


def build_elf_address_map(
    elf_path: str | Path,
    *,
    on_progress: ProgressCallback | None = None,
    use_dwarf_members: bool = True,
) -> dict[str, int]:
    """Return linker-name → address suitable for A2L SYMBOL_LINK matching."""
    elf_path = Path(elf_path)
    out: dict[str, int] = {}
    _report(on_progress, f"Reading ELF {elf_path.name}…", 0.05)

    with elf_path.open("rb") as f:
        elffile = ELFFile(f)
        _report(on_progress, "Reading symbol tables…", 0.15)
        for section in elffile.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for sym in section.iter_symbols():
                name = sym.name
                if not name or name.startswith("$"):
                    continue
                if sym["st_info"]["type"] not in ("STT_OBJECT", "STT_NOTYPE"):
                    continue
                if sym["st_shndx"] == "SHN_UNDEF":
                    continue
                addr = int(sym["st_value"])
                if addr == 0:
                    continue
                _add_alias(out, name, addr)
        _yield_gil()
        logger.info("ELF symtab aliases: %d names from %s", len(out), elf_path.name)

        if not use_dwarf_members or not elffile.has_dwarf_info():
            _report(on_progress, f"ELF map ready ({len(out)} names)", 1.0)
            return out

        _report(on_progress, "Scanning DWARF struct members…", 0.35)
        try:
            dwarfinfo = elffile.get_dwarf_info()
        except Exception:  # noqa: BLE001
            logger.exception("DWARF unavailable")
            _report(on_progress, f"ELF map ready ({len(out)} names)", 1.0)
            return out

        cu_list = list(dwarfinfo.iter_CUs())
        total = max(len(cu_list), 1)
        members_added = 0
        for cu_i, cu in enumerate(cu_list):
            if cu_i % 5 == 0:
                _yield_gil()
                _report(
                    on_progress,
                    f"DWARF CU {cu_i}/{total}… ({members_added} members)",
                    0.35 + 0.6 * (cu_i / total),
                )
            try:
                for die in cu.iter_DIEs():
                    if die.tag != "DW_TAG_variable":
                        continue
                    vname = _die_name(die)
                    if not vname:
                        continue
                    # Need a base address from symtab (normalized)
                    base = out.get(vname)
                    if base is None:
                        base = out.get(normalize_symbol_name(vname))
                    if base is None:
                        continue
                    if "DW_AT_type" not in die.attributes:
                        continue
                    try:
                        type_die = die.get_DIE_from_attribute("DW_AT_type")
                    except Exception:  # noqa: BLE001
                        continue
                    struct_die = _unwrap_type(type_die)
                    if struct_die is None:
                        continue
                    base_names = {vname, normalize_symbol_name(vname)}
                    for member, off in _iter_struct_members(struct_die):
                        addr = base + off
                        for bn in base_names:
                            if not bn:
                                continue
                            _add_alias(out, f"{bn}.{member}", addr)
                            members_added += 1
            except Exception:  # noqa: BLE001
                logger.debug("Skipping DWARF CU", exc_info=True)
                continue

        logger.info(
            "ELF address map: %d names (%d DWARF members added) from %s",
            len(out),
            members_added,
            elf_path.name,
        )

    _report(on_progress, f"ELF map ready ({len(out)} names)", 1.0)
    return out
