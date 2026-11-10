"""Decode PEAK PCAN / SJA1000-style CAN error frames for Trace Viewer."""

from __future__ import annotations

# ECC ERRC (bits 6..7) values as reported in PCAN error-frame ID.
_ERRC: dict[int, str] = {
    0: "counters",
    1: "bit",
    2: "form",
    4: "stuff",
    8: "other",
}

# ECC SEG (bits 0..4) — bit-stream processor position (PCAN-Basic Error Frames).
_SEG: dict[int, str] = {
    2: "ID28-21",
    3: "SOF",
    4: "SRTR",
    5: "IDE",
    6: "ID20-18",
    7: "ID17-13",
    8: "CRC",
    9: "r0",
    10: "data",
    11: "DLC",
    12: "RTR",
    13: "r1",
    14: "ID4-0",
    15: "ID12-5",
    17: "err-flag",
    18: "intermission",
    19: "dom-bits",
    23: "err-delim",
    24: "CRC-delim",
    25: "ACK",
    26: "EOF",
    27: "ACK-delim",
    28: "overload",
}


def decode_pcan_error_frame(arbitration_id: int, data: bytes) -> str | None:
    """Return a short human label for a PCAN error frame, or None if not decodable.

    PCAN lays out SJA1000 ECC into ID / DATA[0..3] when ``PCAN_MESSAGE_ERRFRAME``
    is set (see PEAK PCAN-Basic "Error Frames").
    """
    errc = int(arbitration_id) & 0x1FFFFFFF
    if errc not in _ERRC:
        return None
    kind = _ERRC[errc]
    if len(data) < 4:
        return f"CAN err ({kind})"
    direction = "TX" if data[0] == 0 else "RX" if data[0] == 1 else f"dir={data[0]}"
    seg = int(data[1])
    where = _SEG.get(seg, f"seg={seg:#x}")
    rec = int(data[2])
    tec = int(data[3])
    # ACK-slot "other" while transmitting is the common no-peer / wrong-bitrate case.
    if errc == 8 and seg == 25 and data[0] == 0:
        hint = "no ACK?"
    elif errc in (1, 4) and data[0] == 1:
        # Stuff/bit while receiving often means Classic session on a CAN FD bus.
        hint = "enable CAN FD?"
    elif errc == 1 and data[0] == 0:
        hint = "bitrate/wiring?"
    else:
        hint = ""
    base = f"{kind} {direction} @{where} REC={rec} TEC={tec}"
    return f"{base} ({hint})" if hint else base


def error_frame_flags_suffix(arbitration_id: int, data: bytes) -> str:
    """Compact Flags-column text after ``ERR`` (may be empty)."""
    label = decode_pcan_error_frame(arbitration_id, data)
    if not label:
        return ""
    # Keep Flags short: "bit TX @ACK TEC=16"
    parts = label.split()
    if len(parts) >= 3:
        kind, direction = parts[0], parts[1]
        where = parts[2].lstrip("@") if parts[2].startswith("@") else parts[2]
        tec = ""
        for p in parts:
            if p.startswith("TEC="):
                tec = p
                break
        short = f"{kind} {direction} {where}"
        if tec:
            short = f"{short} {tec}"
        if "no ACK" in label:
            short = f"{short} noACK"
        return short
    return label
