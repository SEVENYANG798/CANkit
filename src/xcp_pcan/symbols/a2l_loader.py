"""Lightweight A2L loader focused on MEASUREMENT symbols and linear COMPU_METHOD."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from xcp_pcan.symbols.a2l_address import (
    overlay_elf_addresses,
    parse_link_map,
    parse_memory_segments,
    resolve_address_in_block,
    unresolved_address_count,
)
from xcp_pcan.symbols.types import (
    Conversion,
    SymbolKind,
    SymbolStore,
    Variable,
    XcpTransportHints,
    datatype_size,
    parse_datatype,
)

ProgressCallback = Callable[[str, float], None]

# Tokenize: strings, comments, keywords, numbers. Named groups avoid false skips.
_TOKEN_RE = re.compile(
    r'(?P<string>"(?:[^"\\]|\\.)*")'
    r"|(?P<block_comment>/\*[^*]*\*+(?:[^/*][^*]*\*+)*/)"
    r"|(?P<line_comment>//[^\n]*)"
    r"|(?P<keyword>/begin|/end|[A-Za-z_][\w.]*)"
    r"|(?P<number>-?0x[0-9A-Fa-f]+|-?\d+\.\d+[eE][+-]?\d+|-?\d+\.\d+|-?\d+)"
    r"|(?P<other>\S)",
)


def _tokenize(text: str, on_progress: ProgressCallback | None = None) -> list[str]:
    tokens: list[str] = []
    total = max(len(text), 1)
    for i, m in enumerate(_TOKEN_RE.finditer(text)):
        kind = m.lastgroup
        if kind in {"block_comment", "line_comment"}:
            continue
        tok = m.group(0)
        if kind == "string":
            tokens.append(tok[1:-1])
        elif kind == "other" and tok in "{}[](),;":
            continue
        else:
            tokens.append(tok)
        # Cancel / UI pump during long tokenizes (large A2L).
        if on_progress is not None and i > 0 and i % 40000 == 0:
            _report(on_progress, "Tokenizing A2L…", 0.12 + 0.14 * (m.start() / total))
            _yield_gil()
    return tokens


def _parse_number(tok: str) -> int | float:
    if tok.lower().startswith("0x"):
        return int(tok, 16)
    if "." in tok or "e" in tok.lower():
        return float(tok)
    return int(tok)


def _find_blocks(tokens: list[str], block_name: str) -> list[list[str]]:
    """Return token slices inside each /begin NAME ... /end NAME."""
    blocks: list[list[str]] = []
    i = 0
    n = len(tokens)
    upper = block_name.upper()
    while i < n - 1:
        if tokens[i] == "/begin" and tokens[i + 1].upper() == upper:
            i += 2
            start = i
            depth = 1
            while i < n - 1 and depth > 0:
                if tokens[i] == "/begin":
                    depth += 1
                    i += 2
                    continue
                if tokens[i] == "/end" and tokens[i + 1].upper() == upper and depth == 1:
                    blocks.append(tokens[start:i])
                    i += 2
                    depth = 0
                    break
                if tokens[i] == "/end":
                    depth -= 1
                    i += 2
                    continue
                i += 1
            continue
        i += 1
    return blocks


def _keyword_value(block: list[str], keyword: str) -> str | None:
    key = keyword.upper()
    for i, tok in enumerate(block):
        if tok.upper() == key and i + 1 < len(block):
            return block[i + 1]
    return None


def _load_compu_methods(tokens: list[str]) -> dict[str, Conversion]:
    methods: dict[str, Conversion] = {
        "NO_COMPU_METHOD": Conversion(name="NO_COMPU_METHOD", is_identity=True),
    }
    for block in _find_blocks(tokens, "COMPU_METHOD"):
        if not block:
            continue
        name = block[0]
        # Format often: name "long_id" conversion_type "format" "unit" ...
        unit = ""
        factor = 1.0
        offset = 0.0
        is_identity = True
        # Find unit string after conversion type (COEFFS / IDENTICAL / ...)
        conversion_type = block[2].upper() if len(block) > 2 else "IDENTICAL"
        # Unit is typically the 5th token (index 4) as quoted string already unquoted
        if len(block) > 4:
            unit = block[4]
        if conversion_type == "LINEAR":
            # /begin COMPU_METHOD ... LINEAR "fmt" "unit" coeffs a b  phys = a*raw + b?
            # ASAP2 LINEAR: coeffs are often after COEFFS keyword in RAT_FUNC;
            # For LINEAR: two coeffs — phys = (raw * a) + b  (a=factor, b=offset)
            coeffs_idx = None
            for i, t in enumerate(block):
                if t.upper() == "COEFFS_LINEAR" and i + 2 < len(block):
                    coeffs_idx = i + 1
                    break
            if coeffs_idx is not None:
                try:
                    factor = float(_parse_number(block[coeffs_idx]))
                    offset = float(_parse_number(block[coeffs_idx + 1]))
                    is_identity = factor == 1.0 and offset == 0.0
                except (ValueError, TypeError):
                    pass
        elif conversion_type == "RAT_FUNC":
            # COEFFS a b c d e f ; phys = (a*x^2 + b*x + c) / (d*x^2 + e*x + f)
            # Common calibration case: a=0,b=factor,c=offset,d=0,e=0,f=1 → phys = factor*x + offset
            for i, t in enumerate(block):
                if t.upper() == "COEFFS" and i + 6 < len(block):
                    try:
                        a = float(_parse_number(block[i + 1]))
                        b = float(_parse_number(block[i + 2]))
                        c = float(_parse_number(block[i + 3]))
                        d = float(_parse_number(block[i + 4]))
                        e = float(_parse_number(block[i + 5]))
                        f = float(_parse_number(block[i + 6]))
                        if a == 0 and d == 0 and e == 0 and f == 1:
                            factor = b
                            offset = c
                            is_identity = factor == 1.0 and offset == 0.0
                    except (ValueError, TypeError):
                        pass
                    break
        elif conversion_type == "IDENTICAL":
            is_identity = True
        methods[name] = Conversion(
            name=name, factor=factor, offset=offset, unit=unit, is_identity=is_identity
        )
    return methods


def _report(progress: ProgressCallback | None, message: str, fraction: float) -> None:
    from xcp_pcan.util.cancel import report_progress

    report_progress(progress, message, fraction)


def _yield_gil() -> None:
    """Briefly release the GIL so the Qt UI thread can process events."""
    time.sleep(0.002)


_DATATYPE_TOKENS = {
    "UBYTE",
    "SBYTE",
    "UWORD",
    "SWORD",
    "ULONG",
    "SLONG",
    "FLOAT32_IEEE",
    "FLOAT64_IEEE",
    "A_UINT64",
    "A_INT64",
    "UINT8",
    "INT8",
    "UINT16",
    "INT16",
    "UINT32",
    "INT32",
    "FLOAT32",
    "FLOAT64",
}


def _parse_symbol_block(
    block: list[str],
    *,
    methods: dict[str, Conversion],
    kind: SymbolKind,
    writable: bool,
    link_map: dict[str, tuple[int, int]] | None = None,
    memory_segments: dict[str, tuple[int, int]] | None = None,
) -> Variable | None:
    if len(block) < 4:
        return None
    name = block[0]

    char_type = ""
    if kind == SymbolKind.CHARACTERISTIC:
        # name, long_id, Type(VALUE/CURVE/MAP/VAL_BLK/…), …
        char_type = (block[2] if len(block) > 2 else "VALUE").upper()
        conv_name = _characteristic_conversion_name(block, methods)
        datatype = _characteristic_datatype(block)
        # Only scalar VALUE characteristics are safe to write as a single cell.
        writable = writable and char_type == "VALUE"
    else:
        # MEASUREMENT: name, long_id, datatype, conversion, …
        datatype = parse_datatype(block[2]) if len(block) > 2 else parse_datatype("UBYTE")
        conv_name = block[3] if len(block) > 3 else "NO_COMPU_METHOD"

    conversion = methods.get(conv_name, Conversion(name=conv_name))
    resolved = resolve_address_in_block(
        block,
        link_map=link_map or {},
        memory_segments=memory_segments or {},
        symbol_name=name,
    )
    size = datatype_size(datatype)
    size_tok = _keyword_value(block, "ARRAY_SIZE")
    if size_tok is not None:
        try:
            size = int(_parse_number(size_tok)) * datatype_size(datatype)
        except (ValueError, TypeError):
            pass
    unit = conversion.unit
    u = _keyword_value(block, "UNIT")
    if u:
        unit = u
    return Variable(
        name=name,
        address=resolved.address,
        size=size,
        datatype=datatype,
        addr_ext=resolved.addr_ext,
        unit=unit,
        conversion=conversion,
        source="a2l",
        comment=block[1] if len(block) > 1 else "",
        kind=kind,
        writable=writable,
        characteristic_type=char_type,
        symbol_link=resolved.symbol_link,
        address_source=resolved.source,
    )


def _characteristic_conversion_name(block: list[str], methods: dict[str, Conversion]) -> str:
    ckw = _keyword_value(block, "CONVERSION")
    if ckw:
        return ckw
    for tok in block[2:]:
        if tok in methods:
            return tok
    # Classic ASAP2: Type then Conversion name before limits/address.
    if len(block) > 3 and not _looks_like_number(block[3]):
        return block[3]
    return "NO_COMPU_METHOD"


def _characteristic_datatype(block: list[str]):
    for tok in block:
        key = tok.upper().replace("-", "_")
        if key in _DATATYPE_TOKENS:
            return parse_datatype(tok)
        layout_dt = _datatype_from_record_layout(tok)
        if layout_dt is not None:
            return layout_dt
    return parse_datatype("ULONG")


def _datatype_from_record_layout(token: str):
    """Map RECORD_LAYOUT names like Record_UBYTE / Record_BOOLEAN → DataType."""
    key = token.upper().replace("-", "_")
    if not key.startswith("RECORD_"):
        return None
    suffix = key[len("RECORD_") :]
    aliases = {
        "BOOLEAN": "UBYTE",
        "BOOL": "UBYTE",
        "UINT8": "UBYTE",
        "INT8": "SBYTE",
        "UINT16": "UWORD",
        "INT16": "SWORD",
        "UINT32": "ULONG",
        "INT32": "SLONG",
        "FLOAT32": "FLOAT32_IEEE",
        "FLOAT64": "FLOAT64_IEEE",
    }
    return parse_datatype(aliases.get(suffix, suffix))


def _looks_like_number(token: str) -> bool:
    t = token.strip().lower()
    if t.startswith("0x"):
        return True
    try:
        float(t)
        return True
    except ValueError:
        return False


def _first_int_after(block: list[str], keyword: str) -> int | None:
    key = keyword.upper()
    for i, tok in enumerate(block):
        if tok.upper() == key and i + 1 < len(block):
            try:
                return int(_parse_number(block[i + 1]))
            except (ValueError, TypeError):
                return None
    return None


def _nested_block_first_int(tokens: list[str], block_name: str) -> int | None:
    for block in _find_blocks(tokens, block_name):
        for tok in block:
            try:
                if tok.lower().startswith("0x") or tok.lstrip("-").isdigit():
                    return int(_parse_number(tok))
            except (ValueError, TypeError):
                continue
    return None


def parse_xcp_transport_hints(tokens: list[str]) -> XcpTransportHints:
    """Extract CAN IDs / baudrate from IF_DATA XCP / XCP_ON_CAN when present."""
    hints = XcpTransportHints()

    # Prefer dedicated XCP_ON_CAN / XCP_ON_CAN_FD sections
    can_blocks: list[list[str]] = []
    can_blocks.extend(_find_blocks(tokens, "XCP_ON_CAN"))
    can_blocks.extend(_find_blocks(tokens, "XCP_ON_CAN_FD"))
    if not can_blocks:
        # Fallback: scan IF_DATA bodies that mention XCP
        for block in _find_blocks(tokens, "IF_DATA"):
            if block and block[0].upper() == "XCP":
                can_blocks.append(block)

    for block in can_blocks:
        # Rebuild a mini token stream for nested /begin searches
        # Nested blocks were flattened into parent by _find_blocks content —
        # so also search globally for CAN_ID_* blocks.
        master = _first_int_after(block, "CAN_ID_MASTER")
        slave = _first_int_after(block, "CAN_ID_SLAVE")
        baud = _first_int_after(block, "BAUDRATE")
        # Some files use CAN_ID_MASTER as first standalone numbers after version
        if master is not None:
            hints.master_can_id = master
        if slave is not None:
            hints.slave_can_id = slave
        if baud is not None and baud > 1000:
            # Heuristic: arbitration baud is typically >= 10k; avoid tiny version fields
            if hints.bitrate is None or baud <= 1_000_000:
                hints.bitrate = baud

        # CAN FD markers inside the block text
        upper_joined = " ".join(t.upper() for t in block)
        if "CAN_FD" in upper_joined or "XCP_ON_CAN_FD" in upper_joined:
            hints.fd = True
        data_br = _first_int_after(block, "BAUDRATE_DATA")
        if data_br is None:
            data_br = _first_int_after(block, "DATA_BAUDRATE")
        if data_br is not None and data_br >= 100_000:
            hints.data_bitrate = data_br
            hints.fd = True

    # Nested ASAP2 style: /begin CAN_ID_MASTER ... /end
    if hints.master_can_id is None:
        hints.master_can_id = _nested_block_first_int(tokens, "CAN_ID_MASTER")
    if hints.slave_can_id is None:
        hints.slave_can_id = _nested_block_first_int(tokens, "CAN_ID_SLAVE")

    # Global BAUDRATE near XCP_ON_CAN (already handled); also CAN_FD section
    for fd_block in _find_blocks(tokens, "CAN_FD"):
        hints.fd = True
        data_br = _first_int_after(fd_block, "BAUDRATE")
        if data_br is None:
            data_br = _first_int_after(fd_block, "BAUDRATE_DATA")
        if data_br is not None and data_br >= 100_000:
            hints.data_bitrate = data_br

    return hints


def load_a2l(
    path: str | Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> SymbolStore:
    """Load MEASUREMENT / CHARACTERISTIC symbols from an ASAP2 A2L file.

    Address resolution order per symbol:
    ECU_ADDRESS → ADDRESS → SYMBOL_LINK+LINK_MAP → LINK_MAP by name →
    REF_MEMORY_SEGMENT(+OFFSET). Remaining zeros can be filled later via
    ``overlay_elf_addresses`` / an ELF file.
    """
    path = Path(path)
    _report(on_progress, f"Reading {path.name}…", 0.02)
    text = path.read_text(encoding="utf-8", errors="replace")
    _yield_gil()
    _report(on_progress, "Tokenizing A2L…", 0.12)
    tokens = _tokenize(text, on_progress=on_progress)
    del text
    _yield_gil()
    _report(on_progress, "Parsing COMPU_METHOD…", 0.28)
    methods = _load_compu_methods(tokens)
    _yield_gil()
    _report(on_progress, "Indexing LINK_MAP / MEMORY_SEGMENT…", 0.40)
    link_map = parse_link_map(tokens)
    memory_segments = parse_memory_segments(tokens, _find_blocks)
    _yield_gil()

    variables: list[Variable] = []
    seen: set[str] = set()

    _report(on_progress, "Parsing MEASUREMENT…", 0.55)
    meas_blocks = _find_blocks(tokens, "MEASUREMENT")
    for idx, block in enumerate(meas_blocks):
        if idx % 100 == 0 and meas_blocks:
            _yield_gil()
            _report(
                on_progress,
                f"MEASUREMENT {idx}/{len(meas_blocks)}…",
                0.55 + 0.15 * (idx / max(1, len(meas_blocks))),
            )
        var = _parse_symbol_block(
            block,
            methods=methods,
            kind=SymbolKind.MEASUREMENT,
            writable=False,
            link_map=link_map,
            memory_segments=memory_segments,
        )
        if var is None or var.name in seen:
            continue
        variables.append(var)
        seen.add(var.name)

    _report(on_progress, "Parsing CHARACTERISTIC…", 0.75)
    char_blocks = _find_blocks(tokens, "CHARACTERISTIC")
    for idx, block in enumerate(char_blocks):
        if idx % 100 == 0 and char_blocks:
            _yield_gil()
            _report(
                on_progress,
                f"CHARACTERISTIC {idx}/{len(char_blocks)}…",
                0.75 + 0.15 * (idx / max(1, len(char_blocks))),
            )
        var = _parse_symbol_block(
            block,
            methods=methods,
            kind=SymbolKind.CHARACTERISTIC,
            writable=True,
            link_map=link_map,
            memory_segments=memory_segments,
        )
        if var is None:
            continue
        if var.name in seen:
            # Prefer writable characteristic over measurement of same name
            variables = [v for v in variables if v.name != var.name]
        variables.append(var)
        seen.add(var.name)

    _report(on_progress, "Sorting / transport hints…", 0.93)
    variables.sort(key=lambda v: v.name.lower())
    transport = parse_xcp_transport_hints(tokens)
    store = SymbolStore(
        variables=variables,
        source_path=str(path),
        transport=transport if transport.any_set() else None,
    )
    missing = unresolved_address_count(store)
    _report(
        on_progress,
        f"Done — {len(variables)} symbols"
        + (f", {missing} without address" if missing else ""),
        1.0,
    )
    return store


# Re-export for callers / tests
__all__ = [
    "load_a2l",
    "parse_xcp_transport_hints",
    "overlay_elf_addresses",
    "unresolved_address_count",
]
