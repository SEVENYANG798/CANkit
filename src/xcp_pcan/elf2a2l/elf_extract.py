"""Extract global variables (name, address, type) from ELF + DWARF."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from elftools.dwarf.descriptions import describe_attr_value
from elftools.dwarf.die import DIE
from elftools.dwarf.locationlists import LocationParser
from elftools.elf.elffile import ELFFile

from .types_map import (
    SIZE_FALLBACK,
    A2lTypeInfo,
    from_encoding,
    from_typename,
    type_for_object_size,
)

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4


@dataclass
class MemSection:
    name: str
    address: int
    size: int
    writable: bool
    executable: bool


def list_data_sections(elf: ELFFile) -> list[MemSection]:
    """Allocated, non-code sections. These become A2L MEMORY_SEGMENTs."""
    out: list[MemSection] = []
    for sec in elf.iter_sections():
        flags = int(sec["sh_flags"])
        addr = int(sec["sh_addr"])
        size = int(sec["sh_size"])
        if not addr or not size or not (flags & SHF_ALLOC):
            continue
        if flags & SHF_EXECINSTR:
            continue
        out.append(
            MemSection(
                name=sec.name,
                address=addr,
                size=size,
                writable=bool(flags & SHF_WRITE),
                executable=False,
            )
        )
    out.sort(key=lambda s: (s.address, s.name))
    return out


@dataclass
class Variable:
    name: str
    address: int
    size: int
    a2l_type: A2lTypeInfo
    section: str = ""
    typename: str = ""
    source: str = ""  # dwarf | symbol
    is_array: bool = False
    array_dims: list[int] = field(default_factory=list)


def _attr_value(die: DIE, name: str) -> Any:
    attr = die.attributes.get(name)
    if attr is None:
        return None
    return attr.value


def _attr_name(die: DIE, name: str = "DW_AT_name") -> str | None:
    val = _attr_value(die, name)
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _follow_type(die: DIE, max_depth: int = 32) -> DIE | None:
    """Resolve typedef / const / volatile / restrict wrappers to underlying type DIE."""
    cur = die
    for _ in range(max_depth):
        if cur is None:
            return None
        if cur.tag in (
            "DW_TAG_typedef",
            "DW_TAG_const_type",
            "DW_TAG_volatile_type",
            "DW_TAG_restrict_type",
            "DW_TAG_atomic_type",
        ):
            if "DW_AT_type" not in cur.attributes:
                return cur
            cur = cur.get_DIE_from_attribute("DW_AT_type")
            continue
        return cur
    return cur


def _byte_size(die: DIE | None) -> int | None:
    if die is None:
        return None
    sz = _attr_value(die, "DW_AT_byte_size")
    if sz is not None:
        return int(sz)
    return None


def _resolve_a2l_type(type_die: DIE | None, fallback_size: int | None = None) -> A2lTypeInfo:
    if type_die is None:
        return SIZE_FALLBACK.get(fallback_size or 1, SIZE_FALLBACK[1])

    base = _follow_type(type_die)
    if base is None:
        return SIZE_FALLBACK.get(fallback_size or 1, SIZE_FALLBACK[1])

    if base.tag == "DW_TAG_base_type":
        enc = _attr_value(base, "DW_AT_encoding")
        sz = _byte_size(base) or fallback_size
        info = from_encoding(int(enc) if enc is not None else None, sz)
        if info:
            return info
        return from_typename(_attr_name(base), sz) or SIZE_FALLBACK.get(sz or 1, SIZE_FALLBACK[1])

    if base.tag == "DW_TAG_enumeration_type":
        sz = _byte_size(base) or fallback_size or 4
        return from_encoding(0x07, sz) or SIZE_FALLBACK.get(sz, SIZE_FALLBACK[4])

    if base.tag == "DW_TAG_pointer_type":
        sz = _byte_size(base) or 4
        return from_encoding(0x07, sz) or SIZE_FALLBACK[4]

    # array / struct / union: treat as opaque blob of known size for top-level
    sz = _byte_size(base) or fallback_size
    name = _attr_name(base)
    info = from_typename(name, sz)
    if info:
        return info
    return SIZE_FALLBACK.get(sz or 1, SIZE_FALLBACK[1])


def _array_bounds(type_die: DIE | None) -> tuple[DIE | None, list[int]]:
    """Return (element_type_die, dimensions). Empty dims if not an array."""
    dims: list[int] = []
    cur = _follow_type(type_die) if type_die else None
    while cur is not None and cur.tag == "DW_TAG_array_type":
        count = None
        for child in cur.iter_children():
            if child.tag != "DW_TAG_subrange_type":
                continue
            upper = _attr_value(child, "DW_AT_upper_bound")
            count_attr = _attr_value(child, "DW_AT_count")
            if count_attr is not None:
                count = int(count_attr)
            elif upper is not None:
                count = int(upper) + 1
            break
        dims.append(count if count is not None else 0)
        if "DW_AT_type" not in cur.attributes:
            return None, dims
        cur = _follow_type(cur.get_DIE_from_attribute("DW_AT_type"))
    return cur, dims


def _struct_members(type_die: DIE | None) -> list[tuple[str, int, DIE]]:
    """Return list of (member_name, byte_offset, member_type_die)."""
    base = _follow_type(type_die)
    if base is None or base.tag not in ("DW_TAG_structure_type", "DW_TAG_class_type", "DW_TAG_union_type"):
        return []
    members: list[tuple[str, int, DIE]] = []
    for child in base.iter_children():
        if child.tag != "DW_TAG_member":
            continue
        name = _attr_name(child)
        if not name:
            continue
        offset = 0
        loc = child.attributes.get("DW_AT_data_member_location")
        if loc is not None:
            # usually constant offset
            if loc.form in ("DW_FORM_data1", "DW_FORM_data2", "DW_FORM_data4",
                            "DW_FORM_data8", "DW_FORM_sdata", "DW_FORM_udata"):
                offset = int(loc.value)
            elif loc.form == "DW_FORM_block1" and isinstance(loc.value, list) and loc.value:
                # DW_OP_plus_uconst
                offset = int(loc.value[-1]) if len(loc.value) >= 2 else 0
        if "DW_AT_type" not in child.attributes:
            continue
        mtype = child.get_DIE_from_attribute("DW_AT_type")
        members.append((name, offset, mtype))
    return members


def _addr_from_loc_expr(expr) -> int | None:
    """Pull DW_OP_addr from a parsed location expression."""
    if not expr:
        return None
    for op in expr:
        op_name = getattr(op, "op_name", None)
        args = getattr(op, "args", None)
        if op_name == "DW_OP_addr" and args:
            return int(args[0])
        # some versions yield (op_num, arg0, ...)
        if isinstance(op, (list, tuple)) and len(op) >= 2:
            # DW_OP_addr = 0x03
            if op[0] in (0x03, "DW_OP_addr"):
                return int(op[1])
    return None


def _parse_location_address(die: DIE, dwarfinfo, cu) -> int | None:
    """Extract absolute address from DW_AT_location if possible."""
    if "DW_AT_location" not in die.attributes:
        return None
    attr = die.attributes["DW_AT_location"]

    # Inline location expression (DW_FORM_exprloc / block*)
    if attr.form in ("DW_FORM_exprloc", "DW_FORM_block1", "DW_FORM_block2",
                     "DW_FORM_block4", "DW_FORM_block"):
        try:
            raw = attr.value
            if isinstance(raw, (bytes, bytearray)):
                raw = list(raw)
            if isinstance(raw, list) and raw and raw[0] == 0x03:
                # DW_OP_addr (0x03) + address bytes (4 or 8)
                addr_bytes = bytes(raw[1:])
                if len(addr_bytes) >= 8:
                    return int.from_bytes(addr_bytes[:8], "little")
                if len(addr_bytes) >= 4:
                    return int.from_bytes(addr_bytes[:4], "little")
        except Exception:
            pass

    try:
        ll = dwarfinfo.location_lists()
        parser = LocationParser(ll)
        if parser.attribute_has_location(attr, cu["version"]):
            loc = parser.parse_from_attribute(attr, cu["version"], die)
            if isinstance(loc, list) and loc:
                if hasattr(loc[0], "loc_expr"):
                    for entry in loc:
                        addr = _addr_from_loc_expr(getattr(entry, "loc_expr", None))
                        if addr is not None:
                            return addr
                else:
                    addr = _addr_from_loc_expr(loc)
                    if addr is not None:
                        return addr
    except Exception:
        pass

    try:
        desc = describe_attr_value(attr, die, cu.cu_offset)
        m = re.search(r"0x([0-9a-fA-F]+)", str(desc))
        if m:
            return int(m.group(1), 16)
    except Exception:
        pass
    return None


def _expand_leafs(
    name: str,
    address: int,
    type_die: DIE | None,
    flatten: bool,
    max_array: int,
    depth: int = 0,
) -> list[tuple[str, int, DIE | None, int, list[int]]]:
    """
    Expand to leaf variables.
    Returns list of (name, address, type_die_for_a2l, size, array_dims_remaining).
    """
    if depth > 16:
        sz = _byte_size(_follow_type(type_die)) or 1
        return [(name, address, type_die, sz, [])]

    base = _follow_type(type_die)
    elem, dims = _array_bounds(type_die)

    if dims:
        # expand first dimension only up to max_array; nested dims stay on element
        count = dims[0]
        if count <= 0 or count > max_array:
            # too large / unknown: emit as single opaque measurement of whole array
            sz = _byte_size(base) or 1
            return [(name, address, type_die, sz, dims)]
        elem_size = _byte_size(_follow_type(elem)) or 0
        if elem_size <= 0:
            # try total / count
            total = _byte_size(base)
            elem_size = (total // count) if total and count else 1
        out: list[tuple[str, int, DIE | None, int, list[int]]] = []
        for i in range(count):
            child_name = f"{name}[{i}]"
            child_addr = address + i * elem_size
            # rebuild type as remaining array or element
            if len(dims) > 1:
                out.extend(
                    _expand_leafs(child_name, child_addr, elem, flatten, max_array, depth + 1)
                )
            else:
                out.extend(
                    _expand_leafs(child_name, child_addr, elem, flatten, max_array, depth + 1)
                )
        return out

    if flatten and base is not None and base.tag in (
        "DW_TAG_structure_type", "DW_TAG_class_type", "DW_TAG_union_type"
    ):
        members = _struct_members(base)
        if members:
            out = []
            for mname, moff, mtype in members:
                out.extend(
                    _expand_leafs(
                        f"{name}.{mname}", address + moff, mtype, flatten, max_array, depth + 1
                    )
                )
            return out

    sz = _byte_size(base) or _byte_size(type_die) or 1
    return [(name, address, type_die, sz, [])]


def _section_for_addr(elf: ELFFile, addr: int) -> str:
    for sec in elf.iter_sections():
        if sec["sh_addr"] == 0 and sec["sh_size"] == 0:
            continue
        start = sec["sh_addr"]
        end = start + sec["sh_size"]
        if start <= addr < end:
            return sec.name
    return ""


def extract_from_dwarf(
    elf: ELFFile,
    *,
    include_static: bool = False,
    flatten: bool = True,
    max_array: int = 64,
) -> list[Variable]:
    if not elf.has_dwarf_info():
        return []

    dwarfinfo = elf.get_dwarf_info()
    results: list[Variable] = []
    seen: set[str] = set()

    for cu in dwarfinfo.iter_CUs():
        top = cu.get_top_DIE()
        for die in cu.iter_DIEs():
            if die.tag != "DW_TAG_variable":
                continue
            # skip locals (have DW_AT_frame_base parent typically) — only file/global scope
            if die.get_parent() is not None and die.get_parent() != top:
                # still allow if external
                if not _attr_value(die, "DW_AT_external"):
                    if not include_static:
                        continue
                    # file-scope static: parent is CU top
                    if die.get_parent() != top:
                        continue

            if _attr_value(die, "DW_AT_declaration"):
                continue

            name = _attr_name(die)
            if not name:
                continue

            addr = _parse_location_address(die, dwarfinfo, cu)
            if addr is None or addr == 0:
                continue

            type_die = None
            if "DW_AT_type" in die.attributes:
                type_die = die.get_DIE_from_attribute("DW_AT_type")

            for leaf_name, leaf_addr, leaf_type, leaf_size, dims in _expand_leafs(
                name, addr, type_die, flatten, max_array
            ):
                if leaf_name in seen:
                    continue
                seen.add(leaf_name)
                a2l_t = _resolve_a2l_type(leaf_type, leaf_size)
                typename = ""
                if leaf_type is not None:
                    followed = _follow_type(leaf_type)
                    typename = _attr_name(followed) or followed.tag if followed else ""
                results.append(
                    Variable(
                        name=leaf_name,
                        address=leaf_addr,
                        size=a2l_t.size if not dims else leaf_size,
                        a2l_type=a2l_t,
                        section=_section_for_addr(elf, leaf_addr),
                        typename=str(typename),
                        source="dwarf",
                        is_array=bool(dims),
                        array_dims=dims,
                    )
                )
    return results


def _section_ranges(elf: ELFFile) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    for sec in elf.iter_sections():
        start = int(sec["sh_addr"])
        size = int(sec["sh_size"])
        if start and size:
            ranges.append((start, start + size, sec.name))
    ranges.sort()
    return ranges


def _section_name(ranges: list[tuple[int, int, str]], addr: int) -> str:
    lo, hi = 0, len(ranges) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, name = ranges[mid]
        if addr < start:
            hi = mid - 1
        elif addr >= end:
            lo = mid + 1
        else:
            return name
    return ""


def annotate_types_from_dwarf(elf: ELFFile, variables: list[Variable]) -> int:
    """Fill C types from DWARF for symbols we already have. Does not expand structs.

    Symbol tables only store size, so float/sint/uint cannot be told apart without this.
    """
    if not elf.has_dwarf_info() or not variables:
        return 0
    # Tasking/HighTec often emit the C name in DWARF and "_name" in the symbol table.
    by_name: dict[str, Variable] = {}
    for var in variables:
        by_name.setdefault(var.name, var)
        if var.name.startswith("_"):
            by_name.setdefault(var.name[1:], var)
    dwarfinfo = elf.get_dwarf_info()
    filled_ids: set[int] = set()
    for cu in dwarfinfo.iter_CUs():
        for die in cu.iter_DIEs():
            if die.tag != "DW_TAG_variable":
                continue
            name = _attr_name(die)
            var = by_name.get(name) if name else None
            if var is None:
                continue
            if "DW_AT_type" not in die.attributes:
                continue
            type_die = die.get_DIE_from_attribute("DW_AT_type")
            elem, dims = _array_bounds(type_die)
            target = elem if dims else type_die
            info = _resolve_a2l_type(target, var.size if not dims else None)
            elem_sz = _byte_size(_follow_type(target)) if target is not None else None
            followed = _follow_type(target)
            var.typename = _attr_name(followed) or (followed.tag if followed else "")
            if dims and all(d > 0 for d in dims) and elem_sz and info.size == elem_sz:
                var.array_dims = dims
                var.a2l_type = info
                var.is_array = True
            else:
                var.array_dims = []
                var.a2l_type = info
                var.is_array = False
            var.source = "dwarf"
            filled_ids.add(id(var))
    return len(filled_ids)


def extract_from_symbols(elf: ELFFile, *, include_static: bool = False) -> list[Variable]:
    """STT_OBJECT symbols. This is the fast path for large ELFs (no DWARF walk)."""
    results: list[Variable] = []
    seen: set[str] = set()
    ranges = _section_ranges(elf)
    binds = ("STB_GLOBAL", "STB_WEAK", "STB_LOCAL") if include_static else ("STB_GLOBAL", "STB_WEAK")
    for sec_name in (".symtab", ".dynsym"):
        symtab = elf.get_section_by_name(sec_name)
        if symtab is None:
            continue
        for sym in symtab.iter_symbols():
            info = sym["st_info"]
            if info["type"] != "STT_OBJECT":
                continue
            if info["bind"] not in binds:
                continue
            name = sym.name
            if not name or name[0] in ".$":
                continue
            addr = int(sym["st_value"])
            if addr == 0:
                continue
            if name in seen:
                continue
            seen.add(name)
            size = int(sym["st_size"]) or 1
            results.append(
                Variable(
                    name=name,
                    address=addr,
                    size=size,
                    a2l_type=type_for_object_size(size),
                    section=_section_name(ranges, addr),
                    typename="",
                    source="symbol",
                )
            )
    return results


def extract_variables(
    elf_path: str,
    *,
    include_static: bool = False,
    flatten: bool = True,
    max_array: int = 64,
    prefer_dwarf: bool = False,
    with_types: bool = True,
) -> tuple[list[Variable], int, list[MemSection]]:
    """Return (variables, dwarf-typed count, data sections for MEMORY_SEGMENT)."""
    with open(elf_path, "rb") as f:
        elf = ELFFile(f)
        sections = list_data_sections(elf)
        sym_vars = extract_from_symbols(elf, include_static=include_static)
        if not prefer_dwarf:
            typed = 0
            if with_types:
                typed = annotate_types_from_dwarf(elf, sym_vars)
            sym_vars.sort(key=lambda x: (x.address, x.name))
            return sym_vars, typed, sections
        dwarf_vars = extract_from_dwarf(
            elf, include_static=include_static, flatten=flatten, max_array=max_array
        )

    by_name: dict[str, Variable] = {v.name: v for v in sym_vars}
    for v in dwarf_vars:
        by_name[v.name] = v
    if not dwarf_vars:
        return sorted(sym_vars, key=lambda x: (x.address, x.name)), 0, sections
    dwarf_roots = {v.name.split(".")[0].split("[")[0] for v in dwarf_vars}
    for v in sym_vars:
        root = v.name.split(".")[0].split("[")[0]
        if root not in dwarf_roots and v.name not in by_name:
            by_name[v.name] = v
    return sorted(by_name.values(), key=lambda x: (x.address, x.name)), len(dwarf_vars), sections


def filter_variables(
    vars_: list[Variable],
    *,
    name_regex: str | None = None,
    exclude_regex: str | None = r"^(__|_Z|_global_|\.)",
    sections: list[str] | None = None,
) -> list[Variable]:
    out = vars_
    if name_regex:
        pat = re.compile(name_regex)
        out = [v for v in out if pat.search(v.name)]
    if exclude_regex:
        epat = re.compile(exclude_regex)
        out = [v for v in out if not epat.search(v.name)]
    if sections:
        allowed = set(sections)
        out = [v for v in out if v.section in allowed or not v.section]
    return out
