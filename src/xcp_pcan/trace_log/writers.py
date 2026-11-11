"""Save TraceFrame sequences as ASC / BLF / CSV."""

from __future__ import annotations

import csv
from pathlib import Path

from can import Message
from can.io import ASCWriter, BLFWriter

from xcp_pcan.trace_log.types import TraceFormat, TraceFrame


def sniff_save_format(path: str | Path) -> TraceFormat:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix == "asc":
        return TraceFormat.ASC
    if suffix == "blf":
        return TraceFormat.BLF
    if suffix == "csv":
        return TraceFormat.CSV
    raise ValueError(f"unsupported save format: {Path(path).suffix!r}")


def _channel_int(raw: str) -> int:
    text = (raw or "").strip()
    if text.isdigit():
        return int(text)
    return 1


def _to_can_msg(fr: TraceFrame) -> Message:
    return Message(
        timestamp=float(fr.timestamp),
        arbitration_id=int(fr.arbitration_id),
        data=bytes(fr.data),
        is_extended_id=bool(fr.is_extended),
        is_fd=bool(fr.is_fd),
        bitrate_switch=bool(fr.bitrate_switch),
        is_rx=bool(fr.is_rx),
        is_error_frame=bool(fr.is_error),
        channel=_channel_int(fr.channel),
        check=False,
    )


def save_trace(
    path: str | Path,
    frames: list[TraceFrame],
    *,
    fmt: TraceFormat | None = None,
) -> int:
    """Write frames to ``path``. Returns number of frames written."""
    path = Path(path)
    fmt = fmt or sniff_save_format(path)
    if fmt is TraceFormat.ASC:
        return _save_asc(path, frames)
    if fmt is TraceFormat.BLF:
        return _save_blf(path, frames)
    if fmt is TraceFormat.CSV:
        return _save_csv(path, frames)
    raise ValueError(f"unsupported format: {fmt}")


def _save_asc(path: Path, frames: list[TraceFrame]) -> int:
    n = 0
    with ASCWriter(str(path)) as writer:
        for fr in frames:
            writer.on_message_received(_to_can_msg(fr))
            n += 1
    return n


def _save_blf(path: Path, frames: list[TraceFrame]) -> int:
    n = 0
    with BLFWriter(str(path)) as writer:
        for fr in frames:
            writer.on_message_received(_to_can_msg(fr))
            n += 1
    return n


def _save_csv(path: Path, frames: list[TraceFrame]) -> int:
    """CANoe / TSMaster-style columns (compatible with ``readers._iter_csv``)."""
    fieldnames = [
        "Abs Time(Sec)",
        "Arb ID",
        "Tx",
        "Er",
        "Xtd",
        "Network",
        "Description",
        "B1",
        "B2",
        "B3",
        "B4",
        "B5",
        "B6",
        "B7",
        "B8",
        "MoreBytes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for fr in frames:
            data = bytes(fr.data)
            row = {
                "Abs Time(Sec)": f"{fr.timestamp:.6f}",
                "Arb ID": f"0x{fr.arbitration_id:X}",
                "Tx": "T" if not fr.is_rx else "F",
                "Er": "T" if fr.is_error else "F",
                "Xtd": "T" if fr.is_extended else "F",
                "Network": fr.channel,
                "Description": fr.name,
                "B1": "",
                "B2": "",
                "B3": "",
                "B4": "",
                "B5": "",
                "B6": "",
                "B7": "",
                "B8": "",
                "MoreBytes": "",
            }
            if len(data) <= 8:
                for i, b in enumerate(data):
                    row[f"B{i + 1}"] = f"{b:02X}"
            else:
                # Pack all bytes into B1 so FD payloads round-trip via reader.
                row["B1"] = " ".join(f"{b:02X}" for b in data)
            writer.writerow(row)
    return len(frames)
