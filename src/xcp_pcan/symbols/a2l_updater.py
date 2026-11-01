"""Update A2L ECU_ADDRESS values from an ELF symbol table (ASAP2 Updater style).

Matches MEASUREMENT / CHARACTERISTIC / AXIS_PTS via SYMBOL_LINK (preferred) or
object name, then rewrites or inserts ``ECU_ADDRESS``.

Uses a line-oriented scanner (supports name on the next line) and periodically
releases the GIL so the Qt UI stays responsive on large files.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from xcp_pcan.symbols.elf_addr_map import build_elf_address_map, normalize_symbol_name

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]

_GIL_YIELD_EVERY_LINES = 64
_GIL_YIELD_SLEEP_S = 0.002


def _report(progress: ProgressCallback | None, message: str, fraction: float) -> None:
    from xcp_pcan.util.cancel import report_progress

    report_progress(progress, message, fraction)


def _yield_gil() -> None:
    time.sleep(_GIL_YIELD_SLEEP_S)


_BLOCK_KINDS = frozenset({"MEASUREMENT", "CHARACTERISTIC", "AXIS_PTS"})

# /begin KIND   or   /begin KIND Name …
_BEGIN_RE = re.compile(
    r"^(?P<prefix>\s*)/begin\s+(?P<kind>MEASUREMENT|CHARACTERISTIC|AXIS_PTS)"
    r"(?:\s+(?P<name>\"[^\"]+\"|\S+))?",
    re.IGNORECASE,
)
_END_RE = re.compile(
    r"^\s*/end\s+(?P<kind>MEASUREMENT|CHARACTERISTIC|AXIS_PTS)\b",
    re.IGNORECASE,
)
_NAME_LINE_RE = re.compile(r'^\s*(?P<name>"[^"]+"|\S+)')

_SYMBOL_LINK_RE = re.compile(
    r"SYMBOL_LINK\s+(?P<name>\"[^\"]+\"|\S+)"
    r"(?:\s+(?P<offset>0x[0-9A-Fa-f]+|-?\d+))?",
    re.IGNORECASE,
)

_ECU_ADDRESS_RE = re.compile(
    r"(?P<kw>ECU_ADDRESS)\s+(?P<addr>0x[0-9A-Fa-f]+|-?\d+)",
    re.IGNORECASE,
)

# CHARACTERISTIC positional address: VALUE|CURVE|MAP … <addr> <deposit> …
_CHAR_POS_ADDR_RE = re.compile(
    r"(?P<head>\b(?:VALUE|ASCII|VAL_BLK|CURVE|MAP|CUBOID|CUBE_4|CUBE_5)\s+)"
    r"(?P<addr>0x[0-9A-Fa-f]+|\d+)",
    re.IGNORECASE,
)

_LINK_MAP_RE = re.compile(
    r"(?P<head>LINK_MAP\s+(?P<qname>\"[^\"]+\"|\S+)\s+)"
    r"(?P<addr>0x[0-9A-Fa-f]+|\d+)",
    re.IGNORECASE,
)


@dataclass
class A2lAddressUpdate:
    object_name: str
    kind: str
    linker_name: str
    old_address: int | None
    new_address: int
    action: str  # "updated" | "inserted" | "unchanged"


@dataclass
class A2lUpdateReport:
    updated: list[A2lAddressUpdate] = field(default_factory=list)
    inserted: list[A2lAddressUpdate] = field(default_factory=list)
    unchanged: list[A2lAddressUpdate] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    output_path: str = ""
    objects_seen: int = 0

    @property
    def changed_count(self) -> int:
        return len(self.updated) + len(self.inserted)

    def summary(self) -> str:
        return (
            f"Objects {self.objects_seen}: "
            f"updated {len(self.updated)}, inserted {len(self.inserted)}, "
            f"unchanged {len(self.unchanged)}, missing in ELF {len(self.missing)}"
        )


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token


def _parse_int(tok: str) -> int:
    tok = tok.strip()
    if tok.lower().startswith("0x"):
        return int(tok, 16)
    return int(tok, 10)


def _format_address(addr: int, *, prefer_hex: bool = True) -> str:
    if prefer_hex:
        return f"0x{addr:X}"
    return str(addr)


def _decode_a2l_bytes(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _build_elf_lookup(elf: dict[str, int]) -> dict[str, tuple[str, int]]:
    return {k.lower(): (k, v) for k, v in elf.items()}


def _lookup_addr(
    names: list[str],
    elf: dict[str, int],
    elf_lower: dict[str, tuple[str, int]],
) -> tuple[str, int] | None:
    tried: list[str] = []
    for name in names:
        if not name:
            continue
        variants = [name, normalize_symbol_name(name)]
        # Also try with a single leading underscore (GHS)
        if not name.startswith("_"):
            variants.append("_" + name)
        for cand in variants:
            if not cand or cand in tried:
                continue
            tried.append(cand)
            if cand in elf:
                return cand, elf[cand]
            hit = elf_lower.get(cand.lower())
            if hit is not None:
                return hit
    return None


def _apply_addr_token(
    body: str,
    match: re.Match[str],
    new_addr: int,
    *,
    head_group: str = "head",
    addr_group: str = "addr",
) -> str:
    old_tok = match.group(addr_group)
    prefer_hex = old_tok.lower().startswith("0x")
    new_tok = _format_address(new_addr, prefer_hex=prefer_hex)
    return body[: match.start()] + match.group(head_group) + new_tok + body[match.end() :]


def _rewrite_link_maps(body: str, linker_name: str, new_addr: int) -> tuple[str, int]:
    """Update LINK_MAP lines whose name matches ``linker_name``. Returns (body, n_changed)."""
    changed = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal changed
        name = _unquote(m.group("qname"))
        if name != linker_name and normalize_symbol_name(name) != normalize_symbol_name(
            linker_name
        ):
            return m.group(0)
        try:
            old = _parse_int(m.group("addr"))
        except ValueError:
            old = None
        if old == new_addr:
            return m.group(0)
        changed += 1
        prefer_hex = m.group("addr").lower().startswith("0x")
        return m.group("head") + _format_address(new_addr, prefer_hex=prefer_hex)

    return _LINK_MAP_RE.sub(_sub, body), changed


def _detect_indent(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            leading = line[: len(line) - len(line.lstrip(" \t"))]
            if leading:
                return leading
    return "      "


def _rewrite_block(
    kind: str,
    object_name: str,
    body: str,
    elf: dict[str, int],
    elf_lower: dict[str, tuple[str, int]],
    report: A2lUpdateReport,
) -> str:
    linker = object_name
    offset = 0
    m_sl = _SYMBOL_LINK_RE.search(body)
    if m_sl:
        linker = _unquote(m_sl.group("name"))
        if m_sl.group("offset"):
            try:
                offset = _parse_int(m_sl.group("offset"))
            except ValueError:
                offset = 0

    candidates = [linker]
    if object_name and object_name not in candidates:
        candidates.append(object_name)

    hit = _lookup_addr(candidates, elf, elf_lower)
    if hit is None:
        report.missing.append(f"{kind} {object_name}")
        return body

    linker_name, base = hit
    new_addr = base + offset
    changed_any = False
    old_primary: int | None = None

    # 1) ECU_ADDRESS (MEASUREMENT / some CHARACTERISTIC)
    m_ecu = _ECU_ADDRESS_RE.search(body)
    if m_ecu:
        try:
            old_primary = _parse_int(m_ecu.group("addr"))
        except ValueError:
            old_primary = None
        if old_primary != new_addr:
            prefer_hex = m_ecu.group("addr").lower().startswith("0x")
            body = (
                body[: m_ecu.start()]
                + f"{m_ecu.group('kw')} {_format_address(new_addr, prefer_hex=prefer_hex)}"
                + body[m_ecu.end() :]
            )
            changed_any = True

    # 2) CHARACTERISTIC positional address (VALUE|MAP … <addr>)
    if kind == "CHARACTERISTIC":
        m_pos = _CHAR_POS_ADDR_RE.search(body)
        if m_pos:
            try:
                old_pos = _parse_int(m_pos.group("addr"))
            except ValueError:
                old_pos = None
            if old_primary is None:
                old_primary = old_pos
            if old_pos != new_addr:
                body = _apply_addr_token(body, m_pos, new_addr)
                changed_any = True

    # 3) IF_DATA / CANAPE_EXT LINK_MAP
    body, n_link = _rewrite_link_maps(body, linker, new_addr)
    if n_link:
        changed_any = True

    # 4) If nothing to rewrite existed, insert ECU_ADDRESS (MEASUREMENT-style)
    if (
        m_ecu is None
        and not (kind == "CHARACTERISTIC" and _CHAR_POS_ADDR_RE.search(body))
        and n_link == 0
    ):
        # Re-check characteristic — body may still lack positional if exotic type
        indent = _detect_indent(body)
        new_tok = _format_address(new_addr, prefer_hex=True)
        insert = f"\n{indent}ECU_ADDRESS {new_tok}"
        stripped = body.rstrip("\n")
        trailing_nl = body[len(stripped) :]
        if not trailing_nl:
            trailing_nl = "\n"
        report.inserted.append(
            A2lAddressUpdate(
                object_name=object_name,
                kind=kind,
                linker_name=linker_name,
                old_address=None,
                new_address=new_addr,
                action="inserted",
            )
        )
        return stripped + insert + trailing_nl

    entry = A2lAddressUpdate(
        object_name=object_name,
        kind=kind,
        linker_name=linker_name,
        old_address=old_primary,
        new_address=new_addr,
        action="updated" if changed_any else "unchanged",
    )
    if changed_any:
        report.updated.append(entry)
    else:
        report.unchanged.append(entry)
    return body


def _split_line_ending(line_with_ending: str) -> tuple[str, str]:
    if line_with_ending.endswith("\r\n"):
        return line_with_ending[:-2], "\r\n"
    if line_with_ending.endswith("\n"):
        return line_with_ending[:-1], "\n"
    if line_with_ending.endswith("\r"):
        return line_with_ending[:-1], "\r"
    return line_with_ending, ""


def update_a2l_text(
    a2l_text: str,
    elf_addresses: dict[str, int],
    *,
    on_progress: ProgressCallback | None = None,
) -> tuple[str, A2lUpdateReport]:
    """Rewrite A2L text using a name→address map. Returns (new_text, report)."""
    report = A2lUpdateReport()
    elf_lower = _build_elf_lookup(elf_addresses)
    lines = a2l_text.splitlines(keepends=True)
    total_lines = max(len(lines), 1)
    out_parts: list[str] = []

    i = 0
    n = len(lines)
    while i < n:
        raw_line = lines[i]
        line, _ending = _split_line_ending(raw_line)
        m_begin = _BEGIN_RE.match(line)
        if m_begin is None:
            out_parts.append(raw_line)
            i += 1
            if i % _GIL_YIELD_EVERY_LINES == 0:
                _yield_gil()
                _report(on_progress, f"Scanning A2L… ({i}/{total_lines})", i / total_lines)
            continue

        kind = m_begin.group("kind").upper()
        name_tok = m_begin.group("name")
        body_parts: list[str] = []
        i += 1

        # Keep "/begin KIND Name" as header; anything after the name on the same
        # line belongs to the body (old regex behaviour).
        header = line[: m_begin.end()] + _ending
        same_line_rest = line[m_begin.end() :]
        if same_line_rest.strip():
            body_parts.append(same_line_rest + _ending)

        # ASAP2 allows: /begin MEASUREMENT\n  Name\n  ...
        if not name_tok:
            while i < n:
                raw_next = lines[i]
                next_line, next_ending = _split_line_ending(raw_next)
                if not next_line.strip():
                    body_parts.append(raw_next)
                    i += 1
                    continue
                m_name = _NAME_LINE_RE.match(next_line)
                if m_name is None:
                    break
                name_tok = m_name.group("name")
                # Keep the name line in the body so the file structure is preserved.
                body_parts.append(raw_next)
                i += 1
                break

        object_name = _unquote(name_tok) if name_tok else ""
        depth = 1
        footer = ""
        while i < n and depth > 0:
            raw_inner = lines[i]
            inner, _ = _split_line_ending(raw_inner)
            m_b = _BEGIN_RE.match(inner)
            if m_b is not None and m_b.group("kind").upper() == kind:
                depth += 1
                body_parts.append(raw_inner)
                i += 1
                continue
            m_e = _END_RE.match(inner)
            if m_e is not None and m_e.group("kind").upper() == kind:
                depth -= 1
                if depth == 0:
                    footer = raw_inner
                    i += 1
                    break
            body_parts.append(raw_inner)
            i += 1
            if i % _GIL_YIELD_EVERY_LINES == 0:
                _yield_gil()

        body = "".join(body_parts)
        # If name was embedded only in body first line (multi-line begin), ECU_ADDRESS
        # still lives in body — fine. Rewrite uses object_name for ELF lookup.
        new_body = _rewrite_block(kind, object_name, body, elf_addresses, elf_lower, report)
        out_parts.append(header)
        out_parts.append(new_body)
        if footer:
            out_parts.append(footer)
        report.objects_seen += 1
        if report.objects_seen % 25 == 0 or i >= n:
            _yield_gil()
            _report(
                on_progress,
                f"Updating… {report.objects_seen} objects (line {i}/{total_lines})",
                i / total_lines,
            )

    logger.info(
        "A2L scan: %d object(s); %s",
        report.objects_seen,
        report.summary(),
    )
    _report(on_progress, "Update pass complete", 1.0)
    return "".join(out_parts), report


def update_a2l_from_elf(
    a2l_path: str | Path,
    elf_path: str | Path,
    output_path: str | Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> A2lUpdateReport:
    """Load ELF addresses and write an updated A2L copy to ``output_path``."""
    a2l_path = Path(a2l_path)
    output_path = Path(output_path)

    _report(on_progress, f"Reading {a2l_path.name}…", 0.05)
    raw = a2l_path.read_bytes()
    _yield_gil()
    text = _decode_a2l_bytes(raw)
    del raw
    _yield_gil()

    last_elf_yield = time.monotonic()

    def _elf_progress(msg: str, frac: float) -> None:
        nonlocal last_elf_yield
        now = time.monotonic()
        if now - last_elf_yield >= 0.05:
            _yield_gil()
            last_elf_yield = now
        _report(on_progress, msg, 0.10 + 0.45 * frac)

    _report(on_progress, f"Reading ELF {Path(elf_path).name}…", 0.10)
    elf_map = build_elf_address_map(elf_path, on_progress=_elf_progress)
    logger.info("ELF address map: %d symbol(s) from %s", len(elf_map), elf_path)
    _yield_gil()

    def _rewrite_progress(msg: str, frac: float) -> None:
        _report(on_progress, msg, 0.60 + 0.25 * frac)

    _report(on_progress, "Updating ECU_ADDRESS…", 0.60)
    new_text, report = update_a2l_text(text, elf_map, on_progress=_rewrite_progress)
    del text
    _yield_gil()

    if report.objects_seen == 0:
        logger.warning(
            "No MEASUREMENT/CHARACTERISTIC/AXIS_PTS blocks found in %s — "
            "check A2L format/encoding",
            a2l_path,
        )

    _report(on_progress, f"Writing {output_path.name}…", 0.90)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    chunk = 256 * 1024
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fh:
            for off in range(0, len(new_text), chunk):
                fh.write(new_text[off : off + chunk])
                _yield_gil()
                _report(
                    on_progress,
                    f"Writing {output_path.name}…",
                    0.90 + 0.09 * min(1.0, (off + chunk) / max(len(new_text), 1)),
                )
        tmp_path.replace(output_path)
    except Exception:
        try:
            if tmp_path.is_file():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    report.output_path = str(output_path)
    _report(on_progress, report.summary(), 1.0)
    logger.info("A2L update written to %s — %s", output_path, report.summary())
    return report
