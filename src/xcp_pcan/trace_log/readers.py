"""Load ASC / BLF / CSV CAN traces into TraceFrame sequences."""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from can.io import ASCReader, BLFReader

from xcp_pcan.trace_log.types import TraceFormat, TraceFrame

ProgressCb = Callable[[int, str], None]

_HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{2}$")
_ARB_HEX = re.compile(r"^[0-9A-Fa-f]+$")


def sniff_format(path: str | Path) -> TraceFormat:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix == "asc":
        return TraceFormat.ASC
    if suffix == "blf":
        return TraceFormat.BLF
    if suffix == "csv":
        return TraceFormat.CSV
    raise ValueError(f"unsupported trace format: {Path(path).suffix!r}")


def iter_trace(
    path: str | Path,
    *,
    fmt: TraceFormat | None = None,
    progress: ProgressCb | None = None,
) -> Iterator[TraceFrame]:
    path = Path(path)
    fmt = fmt or sniff_format(path)
    if fmt is TraceFormat.ASC:
        yield from _iter_asc(path, progress=progress)
    elif fmt is TraceFormat.BLF:
        yield from _iter_blf(path, progress=progress)
    elif fmt is TraceFormat.CSV:
        yield from _iter_csv(path, progress=progress)
    else:
        raise ValueError(f"unsupported format: {fmt}")


def load_trace(
    path: str | Path,
    *,
    fmt: TraceFormat | None = None,
    progress: ProgressCb | None = None,
) -> list[TraceFrame]:
    return list(iter_trace(path, fmt=fmt, progress=progress))


def _report(progress: ProgressCb | None, count: int, note: str = "") -> None:
    if progress is None or (count != 0 and count % 5000 != 0):
        return
    from xcp_pcan.util.cancel import OperationCancelled

    try:
        progress(count, note)
    except OperationCancelled:
        raise
    except Exception:  # noqa: BLE001
        pass


def _from_can_msg(index: int, msg) -> TraceFrame | None:
    arb = getattr(msg, "arbitration_id", None)
    if arb is None:
        return None
    data = bytes(getattr(msg, "data", b"") or b"")
    return TraceFrame(
        index=index,
        timestamp=float(getattr(msg, "timestamp", 0.0) or 0.0),
        arbitration_id=int(arb),
        data=data,
        is_extended=bool(getattr(msg, "is_extended_id", False)),
        is_fd=bool(getattr(msg, "is_fd", False)),
        bitrate_switch=bool(getattr(msg, "bitrate_switch", False)),
        is_rx=not bool(getattr(msg, "is_rx", True) is False),
        is_error=bool(getattr(msg, "is_error_frame", False)),
        channel=str(getattr(msg, "channel", "") or ""),
    )


def _iter_asc(path: Path, *, progress: ProgressCb | None) -> Iterator[TraceFrame]:
    index = 0
    with ASCReader(path) as reader:
        for msg in reader:
            frame = _from_can_msg(index, msg)
            if frame is None:
                continue
            # python-can ASC marks direction via is_rx when present
            if hasattr(msg, "is_rx"):
                frame.is_rx = bool(msg.is_rx)
            yield frame
            index += 1
            _report(progress, index, "ASC")


def _iter_blf(path: Path, *, progress: ProgressCb | None) -> Iterator[TraceFrame]:
    index = 0
    t0: float | None = None
    try:
        reader_cm = BLFReader(path)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Cannot open BLF {path.name}: {exc}") from exc
    with reader_cm as reader:
        for msg in reader:
            frame = _from_can_msg(index, msg)
            if frame is None:
                continue
            if t0 is None:
                t0 = frame.timestamp
            # BLF absolute epoch → relative to first frame for readable columns
            frame.timestamp = frame.timestamp - t0
            if hasattr(msg, "is_rx"):
                frame.is_rx = bool(msg.is_rx)
            ch = getattr(msg, "channel", None)
            if ch is not None and ch != "":
                frame.channel = str(ch)
            yield frame
            index += 1
            _report(progress, index, "BLF")
    if index == 0:
        raise RuntimeError(
            f"BLF {path.name} opened but contains no CAN frames "
            "(only non-CAN objects, or unsupported object types)."
        )


def _parse_arb_id(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.lower().startswith("0x"):
        return int(text, 16)
    if _ARB_HEX.fullmatch(text):
        # Digits-only → decimal (CANoe/TSMaster); any A–F → hex.
        if any(c in "ABCDEFabcdef" for c in text):
            return int(text, 16)
        return int(text, 10)
    return None


def _parse_hex_bytes(parts: list[str]) -> bytes:
    out: list[int] = []
    for part in parts:
        for token in part.replace(",", " ").split():
            tok = token.strip()
            if not tok or tok.upper() in {"F", "T", "TRUE", "FALSE"}:
                continue
            if _HEX_BYTE.fullmatch(tok):
                out.append(int(tok, 16))
    return bytes(out)


def _truthy_flag(raw: str) -> bool:
    return (raw or "").strip().upper() in {"T", "TRUE", "1", "Y", "YES"}


def _iter_csv(path: Path, *, progress: ProgressCb | None) -> Iterator[TraceFrame]:
    """CANoe / TSMaster style export with Arb ID + B1..B8 / MoreBytes."""
    index = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return
        fields = {name.strip(): name for name in reader.fieldnames if name}

        def col(*aliases: str) -> str | None:
            for alias in aliases:
                if alias in fields:
                    return fields[alias]
            lower = {k.lower(): v for k, v in fields.items()}
            for alias in aliases:
                hit = lower.get(alias.lower())
                if hit:
                    return hit
            return None

        c_abs = col("Abs Time(Sec)", "Abs Time (Sec)", "Time", "Timestamp")
        c_arb = col("Arb ID", "ID", "Can ID", "CAN ID")
        c_tx = col("Tx")
        c_er = col("Er", "Error")
        c_net = col("Network", "Channel", "Chn")
        c_name = col("Description", "Name", "Msg Name")
        c_xtd = col("Xtd", "Extended")
        c_b = [col(f"B{i}") for i in range(1, 9)]
        c_more = col("MoreBytes", "More Bytes", "Data")

        for row in reader:
            arb_key = c_arb
            if not arb_key:
                continue
            arb = _parse_arb_id(row.get(arb_key, ""))
            if arb is None:
                continue
            ts = 0.0
            if c_abs:
                try:
                    ts = float((row.get(c_abs) or "0").strip() or 0)
                except ValueError:
                    ts = 0.0

            byte_parts: list[str] = []
            b1 = (row.get(c_b[0], "") if c_b[0] else "") or ""
            rest = [(row.get(c, "") if c else "") or "" for c in c_b[1:]]
            more = (row.get(c_more, "") if c_more else "") or ""
            # Payload either packed into B1 ("AA BB …") or split across B1..B8.
            if b1 and (" " in b1.strip() or len(b1.strip()) > 2) and not any(
                x.strip() for x in rest
            ):
                byte_parts = [b1, more]
            else:
                byte_parts = [b1, *rest, more]
            data = _parse_hex_bytes(byte_parts)

            is_tx = _truthy_flag(row.get(c_tx, "") if c_tx else "")
            is_err = _truthy_flag(row.get(c_er, "") if c_er else "")
            is_xtd = _truthy_flag(row.get(c_xtd, "") if c_xtd else "")
            channel = (row.get(c_net, "") if c_net else "").strip()
            name = (row.get(c_name, "") if c_name else "").strip()

            yield TraceFrame(
                index=index,
                timestamp=ts,
                arbitration_id=arb,
                data=data,
                is_extended=is_xtd or arb > 0x7FF,
                is_fd=len(data) > 8,
                is_rx=not is_tx,
                is_error=is_err,
                channel=channel,
                name=name,
            )
            index += 1
            _report(progress, index, "CSV")
