"""Shared CAN frame helpers (Classic + CAN FD)."""

from __future__ import annotations

from dataclasses import dataclass

# Valid CAN FD data lengths (ISO 11898-1).
CAN_FD_DLC_LENGTHS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


@dataclass
class CanFrame:
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    is_fd: bool = False
    bitrate_switch: bool = False
    timestamp: float | None = None
    is_rx: bool = True  # False = locally transmitted (Monitor / TX echo)
    is_error: bool = False


def next_can_fd_length(nbytes: int) -> int:
    """Smallest valid CAN FD payload length that can hold ``nbytes``."""
    n = max(0, min(64, int(nbytes)))
    for length in CAN_FD_DLC_LENGTHS:
        if length >= n:
            return length
    return 64


def fit_can_payload(data: bytes, *, fd: bool = False, max_len: int | None = None) -> bytes:
    """Pad payload to a legal Classic (8) or CAN FD DLC length."""
    if not fd:
        if len(data) >= 8:
            return data[:8]
        return data + bytes(8 - len(data))

    if max_len is not None:
        target = next_can_fd_length(min(64, max(len(data), max_len)))
    else:
        target = next_can_fd_length(len(data))
    if len(data) > target:
        return data[:target]
    return data + bytes(target - len(data))
