"""Update existing A2L addresses / merge missing MEASUREMENTs from ELF vars."""

from __future__ import annotations

import re
from pathlib import Path

from .a2l_gen import measurements_snippet
from .elf_extract import Variable
from .types_map import STANDARD_COMPU_METHODS, STANDARD_RECORD_LAYOUTS

# Match ECU_ADDRESS lines with optional @ECU_Address@name@ placeholder
# Allow optional CR for Windows CRLF files.
_RE_ECU_ADDR_LINE = re.compile(
    r"(^[ \t]*(?:/\*[^*]*\*/[ \t]*)?ECU_ADDRESS[ \t]+)(0x[0-9A-Fa-f]+)([ \t]*(?:/\*.*?\*/)?[ \t]*\r?$)",
    re.MULTILINE,
)
# CHARACTERISTIC style: address is a positional field after VALUE
_RE_CHAR_ADDR = re.compile(
    r"(^[ \t]*/\* ECU Address\s+\*/[ \t]*)(0x[0-9A-Fa-f]+)([ \t]*(?:/\*.*?\*/)?[ \t]*\r?$)",
    re.MULTILINE,
)
_RE_PLACEHOLDER = re.compile(r"@ECU_Address@(.+?)@")
_RE_MEAS_NAME = re.compile(
    r"/begin\s+MEASUREMENT\b.*?^[ \t]*(?:/\* Name\s+\*/[ \t]*)?(\S+)[ \t]*$",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_RE_BLOCK = re.compile(
    r"/begin\s+(MEASUREMENT|CHARACTERISTIC)\b(.*?)/end\s+\1\b",
    re.DOTALL | re.IGNORECASE,
)
_RE_NAME_IN_BLOCK = re.compile(
    r"^[ \t]*(?:/\*[^*]*\*/[ \t]*)?([A-Za-z_][\w.\[\]]*)[ \t]*$",
    re.MULTILINE,
)


def _addr_map(variables: list[Variable]) -> dict[str, int]:
    return {v.name: v.address for v in variables}


def _find_name_in_block(block_body: str) -> str | None:
    """First identifier-like token after /begin XXX (skipping comments)."""
    for line in block_body.splitlines():
        s = line.strip()
        if not s or s.startswith("/*"):
            # "/* Name */      foo"
            m = re.match(r"/\*[^*]*\*/[ \t]+(\S+)", s)
            if m:
                return m.group(1)
            continue
        if s.startswith("/"):
            continue
        # bare name
        if re.match(r"^[A-Za-z_][\w.\[\]]*$", s):
            return s
    return None


def update_addresses(a2l_text: str, variables: list[Variable]) -> tuple[str, dict]:
    """
    Replace ECU addresses in MEASUREMENT / CHARACTERISTIC blocks when the
    symbol name is found in variables (or in @ECU_Address@name@ comment).
    """
    amap = _addr_map(variables)
    stats = {"updated": 0, "missing": 0, "unchanged": 0}

    def repl_block(m: re.Match) -> str:
        kind = m.group(1).upper()
        body = m.group(2)
        full = m.group(0)

        name = None
        ph = _RE_PLACEHOLDER.search(full)
        if ph:
            name = ph.group(1)
        if not name:
            name = _find_name_in_block(body)
        if not name or name not in amap:
            if name:
                stats["missing"] += 1
            return full

        new_addr = f"0x{amap[name]:08X}"

        def sub_addr(mm: re.Match) -> str:
            old = mm.group(2)
            if old.lower() == new_addr.lower():
                stats["unchanged"] += 1
                return mm.group(0)
            stats["updated"] += 1
            return f"{mm.group(1)}{new_addr}{mm.group(3)}"

        if kind == "MEASUREMENT":
            new_full, n = _RE_ECU_ADDR_LINE.subn(sub_addr, full)
            if n == 0:
                # some files may only have placeholder comment without ECU_ADDRESS keyword style
                return full
            return new_full
        # CHARACTERISTIC
        new_full, n = _RE_CHAR_ADDR.subn(sub_addr, full)
        if n == 0:
            # also try generic 0x after VALUE line — already handled by CHAR_ADDR
            return full
        return new_full

    new_text = _RE_BLOCK.sub(repl_block, a2l_text)
    return new_text, stats


def existing_measurement_names(a2l_text: str) -> set[str]:
    names: set[str] = set()
    for m in _RE_BLOCK.finditer(a2l_text):
        if m.group(1).upper() != "MEASUREMENT":
            continue
        name = _find_name_in_block(m.group(2))
        if name:
            names.add(name)
        ph = _RE_PLACEHOLDER.search(m.group(0))
        if ph:
            names.add(ph.group(1))
    return names


def _ensure_compu_and_layouts(a2l_text: str) -> str:
    """Append missing standard COMPU_METHOD / RECORD_LAYOUT before /end MODULE."""
    missing_cm = [
        (n, f, u)
        for n, f, u in STANDARD_COMPU_METHODS
        if not re.search(rf"^\s*(?:/\*[^*]*\*/\s*)?{re.escape(n)}\s*$", a2l_text, re.M)
    ]
    missing_rl = [
        (n, d)
        for n, d in STANDARD_RECORD_LAYOUTS
        if not re.search(rf"/begin\s+RECORD_LAYOUT\s+{re.escape(n)}\b", a2l_text, re.I)
    ]

    if not missing_cm and not missing_rl:
        return a2l_text

    insert_parts: list[str] = ["\n  /* --- added by toa2l --- */\n"]
    for name, fmt, unit in missing_cm:
        insert_parts.append(
            "\n".join(
                [
                    "  /begin COMPU_METHOD",
                    f"    /* Name of CompuMethod    */      {name}",
                    '    /* Long identifier        */      "Q = V"',
                    "    /* Conversion Type        */      IDENTICAL",
                    f'    /* Format                 */      "{fmt}"',
                    f'    /* Units                  */      "{unit}"',
                    "  /end COMPU_METHOD",
                    "",
                ]
            )
        )
    for name, dtype in missing_rl:
        insert_parts.append(
            "\n".join(
                [
                    f"  /begin  RECORD_LAYOUT {name}",
                    f"    FNC_VALUES 1 {dtype} COLUMN_DIR DIRECT",
                    "  /end  RECORD_LAYOUT ",
                    "",
                ]
            )
        )
    blob = "\n".join(insert_parts)

    # insert before last /end MODULE
    idx = a2l_text.rfind("/end MODULE")
    if idx < 0:
        return a2l_text + blob
    return a2l_text[:idx] + blob + "\n" + a2l_text[idx:]


def merge_measurements(
    a2l_text: str,
    variables: list[Variable],
    *,
    add_missing: bool = True,
) -> tuple[str, dict]:
    """Update addresses and optionally append MEASUREMENTs not already present."""
    text, stats = update_addresses(a2l_text, variables)
    stats["added"] = 0
    if not add_missing:
        return text, stats

    existing = existing_measurement_names(text)
    to_add = [v for v in variables if v.name not in existing]
    if not to_add:
        return text, stats

    snippet = "\n  /* --- MEASUREMENTs added by toa2l from ELF --- */\n\n"
    snippet += measurements_snippet(to_add)
    stats["added"] = len(to_add)

    idx = text.rfind("/end MODULE")
    if idx < 0:
        text = text + "\n" + snippet
    else:
        text = text[:idx] + snippet + "\n" + text[idx:]

    text = _ensure_compu_and_layouts(text)
    return text, stats


def load_a2l(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    text = None
    for enc in ("utf-8", "utf-8-sig", "latin-1", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    # Normalize newlines so address regexes are reliable
    return text.replace("\r\n", "\n").replace("\r", "\n")


def save_a2l(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="\n")
