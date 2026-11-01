"""Load acquisition symbol names from a folder of .txt lists."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from xcp_pcan.symbols.elf_addr_map import normalize_symbol_name
from xcp_pcan.symbols.types import Variable, parse_symbol_name_list

logger = logging.getLogger(__name__)

# One C identifier per line. Prose, paths, and punctuation are not symbols.
_SYMBOL_LINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class SymbolListGroup:
    """One txt file: the tag is the file name without extension."""

    name: str
    names: list[str]


def load_symbol_list_dir(folder: Path) -> list[SymbolListGroup]:
    """Read every ``*.txt`` in ``folder`` (not recursive). Empty files are skipped."""
    groups: list[SymbolListGroup] = []
    for path in sorted(folder.glob("*.txt"), key=lambda p: p.name.lower()):
        names = _names_in_file(path)
        if names:
            groups.append(SymbolListGroup(name=path.stem, names=names))
    return groups


def tag_label(name: str) -> str:
    """Display label: first character uppercased (``batt`` → ``Batt``)."""
    name = name.strip()
    if not name:
        return name
    return name[0].upper() + name[1:]


def names_matching(names: list[str], query: str) -> list[str]:
    """Apply the symbol-filter rules to names that already belong to a list."""
    tokens = parse_symbol_name_list(query)
    if not tokens:
        return list(names)
    if len(tokens) == 1:
        q = tokens[0].lower()
        return [name for name in names if q in name.lower()]
    wanted = {token.lower() for token in tokens}
    return [name for name in names if name.lower() in wanted]


def build_norm_index(by_lower: dict[str, Variable]) -> dict[str, list[Variable]]:
    """Normalize-name index for underscore-tolerant lookups."""
    by_norm: dict[str, list[Variable]] = {}
    for var in by_lower.values():
        by_norm.setdefault(normalize_symbol_name(var.name).lower(), []).append(var)
    return by_norm


def resolve_names(
    names: list[str],
    by_lower: dict[str, Variable],
    by_norm: dict[str, list[Variable]] | None = None,
) -> tuple[list[tuple[str, Variable]], list[str]]:
    """Map list names onto loaded symbols. Order follows the txt file.

    Exact match wins. Otherwise a name matches when the only difference is
    leading underscores (``BCMO_Foo`` → ``_BCMO_Foo``), and that normalized
    name is unique. A loose substring match is not used.
    """
    index = by_norm if by_norm is not None else build_norm_index(by_lower)

    found: list[tuple[str, Variable]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        var = _lookup(key, by_lower, index)
        if var is None:
            missing.append(name)
        else:
            found.append((name, var))
    return found, missing


def _lookup(
    key: str,
    by_lower: dict[str, Variable],
    by_norm: dict[str, list[Variable]],
) -> Variable | None:
    exact = by_lower.get(key)
    if exact is not None:
        return exact
    hits = by_norm.get(normalize_symbol_name(key).lower(), [])
    if len(hits) == 1:
        return hits[0]
    return None


def _names_in_file(path: Path) -> list[str]:
    """Return symbol names, or nothing if the file is not a symbol list.

    A list is only blank lines, ``#`` comments, and one identifier per line.
    A readme such as ``QUICKSTART.txt`` fails that and is ignored entirely.
    """
    text = _read_text(path)
    names: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if _SYMBOL_LINE.fullmatch(line) is None:
            logger.info("Skipped %s: not a symbol list", path.name)
            return []
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(line)
    return names


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
