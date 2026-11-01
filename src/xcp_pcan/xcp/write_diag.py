"""Classify XCP write / verify failures for user-facing diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from xcp_pcan.xcp.protocol import ErrCode, XcpError


class WriteFailureKind(str, Enum):
    LOCKED = "locked"
    PAGE = "page"
    READONLY = "readonly"
    VERIFY = "verify"
    TIMEOUT = "timeout"
    OTHER = "other"


@dataclass(frozen=True)
class WriteFailureInfo:
    kind: WriteFailureKind
    title: str
    detail: str
    hint: str

    def message(self) -> str:
        parts = [self.detail]
        if self.hint:
            parts.append(self.hint)
        return "\n".join(parts)


_KIND_META: dict[WriteFailureKind, tuple[str, str]] = {
    WriteFailureKind.LOCKED: (
        "Access locked",
        "Unlock CAL (Seed&Key) and retry. Check Auto unlock / Unlock now.",
    ),
    WriteFailureKind.PAGE: (
        "Wrong calibration page",
        "Switch to a writable RAM page (SET_CAL_PAGE) then retry.",
    ),
    WriteFailureKind.READONLY: (
        "Write protected",
        "Target is read-only or flash-backed; use a RAM page / CHARACTERISTIC.",
    ),
    WriteFailureKind.VERIFY: (
        "Verify mismatch",
        "Write may have been ignored (lock/page/protect) or value was clipped.",
    ),
    WriteFailureKind.TIMEOUT: (
        "Timeout",
        "Check bus, CAN IDs, and that the ECU still answers CTOs.",
    ),
    WriteFailureKind.OTHER: (
        "Write failed",
        "",
    ),
}


def classify_xcp_error(exc: BaseException) -> WriteFailureInfo:
    if isinstance(exc, TimeoutError) or (
        isinstance(exc, OSError) and "timeout" in str(exc).lower()
    ):
        title, hint = _KIND_META[WriteFailureKind.TIMEOUT]
        return WriteFailureInfo(WriteFailureKind.TIMEOUT, title, str(exc), hint)

    code: int | None = None
    if isinstance(exc, XcpError):
        code = int(exc.code)
    text = str(exc).upper()

    if code in (ErrCode.ACCESS_LOCKED, ErrCode.ACCESS_DENIED) or "ACCESS_LOCKED" in text or "ACCESS_DENIED" in text:
        kind = WriteFailureKind.LOCKED
    elif code in (
        ErrCode.PAGE_NOT_VALID,
        ErrCode.MODE_NOT_VALID,
        ErrCode.SEGMENT_NOT_VALID,
    ) or "PAGE_NOT_VALID" in text or "SEGMENT_NOT_VALID" in text:
        kind = WriteFailureKind.PAGE
    elif code == ErrCode.WRITE_PROTECTED or "WRITE_PROTECTED" in text:
        kind = WriteFailureKind.READONLY
    elif code == ErrCode.VERIFY or "VERIFY" in text:
        kind = WriteFailureKind.VERIFY
    else:
        kind = WriteFailureKind.OTHER

    title, hint = _KIND_META[kind]
    return WriteFailureInfo(kind, title, str(exc), hint)


def classify_verify_mismatch(
    *,
    name: str,
    written: bytes,
    readback: bytes,
    written_phys: object = None,
    readback_phys: object = None,
) -> WriteFailureInfo:
    title, hint = _KIND_META[WriteFailureKind.VERIFY]
    detail = (
        f"«{name}» readback differs from written value.\n"
        f"Wrote: {written.hex()}  Read: {readback.hex()}"
    )
    if written_phys is not None or readback_phys is not None:
        detail += f"\nPhysical wrote={written_phys}  read={readback_phys}"
    # Unchanged readback often means locked/protected page
    if readback == written:
        pass
    return WriteFailureInfo(WriteFailureKind.VERIFY, title, detail, hint)
