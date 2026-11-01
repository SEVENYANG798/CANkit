"""Backward-compatible re-exports (prefer ``xcp_pcan.can.transport``)."""

from xcp_pcan.can.frame_util import (
    CAN_FD_DLC_LENGTHS,
    CanFrame,
    fit_can_payload,
    next_can_fd_length,
)
from xcp_pcan.can.transport import (
    CanBusTransport,
    HardwareBackend,
    PcanTransport,
    discover_vector_devices,
    list_pcan_channels,
    list_vector_channels,
)

__all__ = [
    "CAN_FD_DLC_LENGTHS",
    "CanBusTransport",
    "CanFrame",
    "HardwareBackend",
    "PcanTransport",
    "discover_vector_devices",
    "fit_can_payload",
    "list_pcan_channels",
    "list_vector_channels",
    "next_can_fd_length",
]
