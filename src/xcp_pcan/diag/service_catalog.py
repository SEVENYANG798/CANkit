"""Built-in UDS services extracted from FlashFlow ASC (Req 0x73D / Resp 0x7BD).

Source: FlashFlow_N82KS_R1001_2026-11-03 101458.asc
Pad byte on the wire is 0xCC; short frames are ISO-TP SF + pad to 8.
TransferData (0x36) blocks are omitted — use Raw / programming templates instead.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiagService:
    title: str
    uds_hex: str  # UDS only; UI wraps ISO-TP + pad for Raw
    params: tuple[tuple[str, str, str], ...] = ()  # (name, value, unit)


# Groups shown in the Diagnostic Console tree (Vector-like).
FLASHFLOW_SERVICES: tuple[tuple[str, tuple[DiagService, ...]], ...] = (
    (
        "Sessions",
        (
            DiagService("10 01 — DefaultSession", "10 01", (("Session", "01", "hex"),)),
            DiagService(
                "10 02 — ProgrammingSession",
                "10 02",
                (("Session", "02", "hex"),),
            ),
            DiagService(
                "10 03 — ExtendedSession",
                "10 03",
                (("Session", "03", "hex"),),
            ),
        ),
    ),
    (
        "Identification (0x22)",
        (
            DiagService("22 F183 — Read DID", "22 F1 83", (("DID", "F183", "hex"),)),
            DiagService("22 F184 — Read DID", "22 F1 84", (("DID", "F184", "hex"),)),
            DiagService("22 F187 — Read DID", "22 F1 87", (("DID", "F187", "hex"),)),
            DiagService("22 F188 — Read DID", "22 F1 88", (("DID", "F188", "hex"),)),
            DiagService("22 F18A — Read DID", "22 F1 8A", (("DID", "F18A", "hex"),)),
            DiagService("22 F190 — Read DID", "22 F1 90", (("DID", "F190", "hex"),)),
            DiagService("22 F191 — Read DID", "22 F1 91", (("DID", "F191", "hex"),)),
            DiagService("22 F195 — Read DID", "22 F1 95", (("DID", "F195", "hex"),)),
            DiagService("22 F199 — Read DID", "22 F1 99", (("DID", "F199", "hex"),)),
            DiagService("22 F0FF — Read DID", "22 F0 FF", (("DID", "F0FF", "hex"),)),
        ),
    ),
    (
        "WriteDataByIdentifier (0x2E)",
        (
            DiagService(
                "2E F184 — Write DID",
                "2E F1 84 26 11 03 31 31 31 31 31 31",
                (
                    ("DID", "F184", "hex"),
                    ("Data", "26 11 03 31 31 31 31 31 31", "hex"),
                ),
            ),
        ),
    ),
    (
        "Security Access (0x27)",
        (
            DiagService("27 11 — RequestSeed", "27 11", (("Level", "11", "hex"),)),
            DiagService(
                "27 12 — SendKey",
                "27 12 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
                (
                    ("Level", "12", "hex"),
                    (
                        "Key",
                        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
                        "hex",
                    ),
                ),
            ),
        ),
    ),
    (
        "RoutineControl (0x31)",
        (
            DiagService(
                "31 01 0203 — StartRoutine",
                "31 01 02 03",
                (
                    ("SubFn", "01", "hex"),
                    ("Routine", "0203", "hex"),
                ),
            ),
            DiagService(
                "31 01 0202 — StartRoutine",
                "31 01 02 02",
                (
                    ("SubFn", "01", "hex"),
                    ("Routine", "0202", "hex"),
                    ("Data", "", "hex"),
                ),
            ),
            DiagService(
                "31 01 FF00 — StartRoutine",
                "31 01 FF 00 44 80 06 20 00 00 16 B8 00",
                (
                    ("SubFn", "01", "hex"),
                    ("Routine", "FF00", "hex"),
                    ("Data", "44 80 06 20 00 00 16 B8 00", "hex"),
                ),
            ),
            DiagService(
                "31 01 FF01 — StartRoutine",
                "31 01 FF 01",
                (
                    ("SubFn", "01", "hex"),
                    ("Routine", "FF01", "hex"),
                ),
            ),
        ),
    ),
    (
        "Programming (0x34/36/37)",
        (
            DiagService(
                "34 — RequestDownload",
                "34 00 44 70 10 00 10 00 00 10 31",
                (
                    ("DataFormat", "00", "hex"),
                    ("AddrLenFormat", "44", "hex"),
                    ("Address/Size", "70 10 00 10 00 00 10 31", "hex"),
                ),
            ),
            DiagService(
                "36 — TransferData",
                "36 01 00",
                (
                    ("BlockCounter", "01", "hex"),
                    ("Data", "00", "hex"),
                ),
            ),
            DiagService("37 — RequestTransferExit", "37", ()),
        ),
    ),
    (
        "ECU Reset",
        (DiagService("11 01 — HardReset", "11 01", (("ResetType", "01", "hex"),)),),
    ),
    (
        "Tester Present",
        (DiagService("3E 00 — TesterPresent", "3E 00", (("SubFn", "00", "hex"),)),),
    ),
    (
        "Raw Telegrams",
        (DiagService("[Raw] Custom telegram", "", (("Request", "", "hex"),)),),
    ),
)

# Addressing / framing defaults — UDS FL preset (see can_id_presets.UDS_PRESETS).
DEFAULT_REQ_ID = "0x725"
DEFAULT_RESP_ID = "0x7A5"
DEFAULT_PAD = 0xCC
DEFAULT_FD = True
