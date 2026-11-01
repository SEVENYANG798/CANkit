"""CAN transport layer (PCAN + Vector)."""

from xcp_pcan.can.frame_util import CanFrame, fit_can_payload, next_can_fd_length
from xcp_pcan.can.transport import (
    CanBusTransport,
    HardwareBackend,
    PcanTransport,
    VectorChannelInfo,
    VectorDeviceInfo,
    discover_vector_devices,
    list_pcan_channels,
    list_vector_channels,
    list_vector_channels_for_serial,
)

__all__ = [
    "CanBusTransport",
    "CanFrame",
    "HardwareBackend",
    "PcanTransport",
    "VectorChannelInfo",
    "VectorDeviceInfo",
    "discover_vector_devices",
    "fit_can_payload",
    "list_pcan_channels",
    "list_vector_channels",
    "list_vector_channels_for_serial",
    "next_can_fd_length",
]
