"""Lightweight UDS decode for the diagnostic console / Trace."""

from __future__ import annotations

from dataclasses import dataclass

_NRC: dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

_SID: dict[int, str] = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}


def _hex_bytes(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


@dataclass(frozen=True, slots=True)
class UdsEvent:
    """One Diagnostic Trace row (Vector-style)."""

    kind: str  # Request | Positive Response | Negative Response | Error | Info
    service: str
    parameters: str
    data_hex: str
    param_rows: tuple[tuple[str, str], ...] = ()  # (Name, Value) detail pane


def describe_uds(payload: bytes, *, is_request: bool) -> UdsEvent:
    """Decode a UDS PDU into Trace columns + detail parameter rows."""
    if not payload:
        kind = "Request" if is_request else "Positive Response"
        return UdsEvent(kind, "(empty)", "", "", ())

    data_hex = _hex_bytes(payload)
    sid = payload[0]

    # Negative response
    if sid == 0x7F and len(payload) >= 3:
        req_sid = payload[1]
        nrc = payload[2]
        svc = _SID.get(req_sid, f"0x{req_sid:02X}")
        nrc_name = _NRC.get(nrc, f"NRC_0x{nrc:02X}")
        pending = " (ResponsePending)" if nrc == 0x78 else ""
        rows = (
            ("Service", svc),
            ("NRC", f"0x{nrc:02X} ({nrc_name})"),
        )
        return UdsEvent(
            "Negative Response",
            svc,
            f"NRC=0x{nrc:02X} {nrc_name}{pending}",
            data_hex,
            rows,
        )

    # Positive response (SID | 0x40)
    if not is_request and (sid & 0x40) and sid != 0x7F:
        req_sid = sid & ~0x40
        svc = _SID.get(req_sid, f"0x{req_sid:02X}")
        rest = payload[1:]
        params, rows = _params_for(req_sid, rest, response=True)
        return UdsEvent("Positive Response", svc, params, data_hex, rows)

    # Request (or treat as request-shaped)
    svc = _SID.get(sid, f"0x{sid:02X}")
    rest = payload[1:]
    params, rows = _params_for(sid, rest, response=False)
    kind = "Request" if is_request else "Positive Response"
    return UdsEvent(kind, svc, params, data_hex, rows)


def _params_for(
    sid: int, rest: bytes, *, response: bool
) -> tuple[str, tuple[tuple[str, str], ...]]:
    rows: list[tuple[str, str]] = [("Service", _SID.get(sid, f"0x{sid:02X}"))]
    if sid == 0x10 and rest:
        rows.append(("Session", f"0x{rest[0]:02X}"))
        return f"session=0x{rest[0]:02X}", tuple(rows)
    if sid == 0x22 and len(rest) >= 2:
        did = (rest[0] << 8) | rest[1]
        rows.append(("DID", f"0x{did:04X}"))
        data = rest[2:]
        if response and data:
            rows.append(("Data", _hex_bytes(data)))
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            if any(32 <= b < 127 for b in data):
                rows.append(("ASCII", ascii_part))
            return f"DID=0x{did:04X} data={_hex_bytes(data)}", tuple(rows)
        return f"DID=0x{did:04X}", tuple(rows)
    if sid == 0x2E and len(rest) >= 2:
        did = (rest[0] << 8) | rest[1]
        data = rest[2:]
        rows.append(("DID", f"0x{did:04X}"))
        if data:
            rows.append(("Data", _hex_bytes(data)))
            return f"DID=0x{did:04X} data={_hex_bytes(data)}", tuple(rows)
        return f"DID=0x{did:04X}", tuple(rows)
    if sid == 0x27 and rest:
        rows.append(("Level", f"0x{rest[0]:02X}"))
        if len(rest) > 1:
            rows.append(("Key/Seed", _hex_bytes(rest[1:])))
            return f"level=0x{rest[0]:02X} data={_hex_bytes(rest[1:])}", tuple(rows)
        return f"level=0x{rest[0]:02X}", tuple(rows)
    if sid == 0x11 and rest:
        rows.append(("ResetType", f"0x{rest[0]:02X}"))
        return f"reset=0x{rest[0]:02X}", tuple(rows)
    if sid == 0x3E and rest:
        rows.append(("SubFunction", f"0x{rest[0]:02X}"))
        return f"sub=0x{rest[0]:02X}", tuple(rows)
    if sid == 0x14 and rest:
        rows.append(("Group", _hex_bytes(rest[:3])))
        return f"group={_hex_bytes(rest[:3])}", tuple(rows)
    if sid == 0x19 and rest:
        rows.append(("SubFunction", f"0x{rest[0]:02X}"))
        if len(rest) > 1:
            rows.append(("Mask/Data", _hex_bytes(rest[1:])))
        return f"sub=0x{rest[0]:02X}", tuple(rows)
    if rest:
        rows.append(("Data", _hex_bytes(rest)))
        return _hex_bytes(rest), tuple(rows)
    return "", tuple(rows)


def format_uds_message(payload: bytes, *, direction: str) -> str:
    """Legacy one-line summary (tests / simple logs)."""
    is_req = direction.upper().startswith("T")
    ev = describe_uds(payload, is_request=is_req)
    if ev.kind == "Negative Response":
        return f"{ev.kind}: {ev.service} → {ev.parameters}  [{ev.data_hex}]"
    if ev.kind == "Positive Response":
        return f"{ev.kind}: {ev.service} {ev.parameters}  [{ev.data_hex}]".strip()
    return f"{ev.service} {ev.parameters}  [{ev.data_hex}]".strip()
