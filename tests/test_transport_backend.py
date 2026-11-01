"""Hardware backend helpers."""

from unittest.mock import patch

from xcp_pcan.can.transport import (
    HardwareBackend,
    list_pcan_channels,
    list_vector_channels,
)


def test_backends():
    assert HardwareBackend.PCAN.value == "pcan"
    assert HardwareBackend.VECTOR.value == "vector"


def test_channel_lists():
    assert any(v == "PCAN_USBBUS1" for _, v in list_pcan_channels())
    with patch("xcp_pcan.can.transport.discover_vector_devices", return_value=[]):
        assert any(v == 0 for _, v in list_vector_channels())
