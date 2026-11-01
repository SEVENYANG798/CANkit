"""Vector hardware enumeration helpers."""

from types import SimpleNamespace
from unittest.mock import patch

from xcp_pcan.can.transport import (
    VECTOR_CHANNELS,
    discover_vector_devices,
    list_vector_channels,
    list_vector_channels_for_serial,
)


def _fake_cfg(
    *,
    name: str,
    serial: int,
    hw_channel: int,
    channel_index: int,
    can: bool = True,
    fd: bool = True,
    xcvr: str = "CAN Highspeed",
    hw_type_name: str = "XL_HWTYPE_VN1630",
):
    hw_type = SimpleNamespace(name=hw_type_name)
    # Match XL_BUS_ACTIVE_CAP_CAN / CANFD_ISO bits used in transport helpers
    bus_caps = (1 << 16) if can else 0
    ch_caps = (1 << 30) if fd else 0  # XL_CHANNEL_FLAG_CANFD_ISO_SUPPORT often bit 30
    return SimpleNamespace(
        name=name,
        serial_number=serial,
        hw_channel=hw_channel,
        channel_index=channel_index,
        hw_type=hw_type,
        channel_bus_capabilities=bus_caps,
        channel_capabilities=ch_caps,
        transceiver_name=xcvr,
    )


def test_discover_groups_by_serial():
    cfgs = [
        _fake_cfg(name="VN1630 Channel 1", serial=1001, hw_channel=0, channel_index=2),
        _fake_cfg(name="VN1630 Channel 2", serial=1001, hw_channel=1, channel_index=3),
        _fake_cfg(
            name="VN1640 Channel 1",
            serial=2002,
            hw_channel=0,
            channel_index=4,
            hw_type_name="XL_HWTYPE_VN1640",
        ),
        _fake_cfg(
            name="LIN only",
            serial=1001,
            hw_channel=2,
            channel_index=5,
            can=False,
        ),
    ]
    with patch("can.interfaces.vector.get_channel_configs", return_value=cfgs):
        # Bypass capability flags that depend on xldefine — force CAN filter via our bits
        with patch(
            "xcp_pcan.can.transport._is_can_channel",
            side_effect=lambda c: bool(c.channel_bus_capabilities),
        ):
            with patch(
                "xcp_pcan.can.transport._channel_supports_fd",
                side_effect=lambda c: bool(c.channel_capabilities),
            ):
                devices = discover_vector_devices()

    assert len(devices) == 2
    vn1630 = next(d for d in devices if d.serial == 1001)
    assert vn1630.device_name == "VN1630"
    assert "VN1630" in vn1630.label
    assert len(vn1630.channels) == 2
    assert vn1630.channels[0].hw_channel == 0
    assert "CAN1" in vn1630.channels[0].label
    assert "FD" in vn1630.channels[0].label

    chans = list_vector_channels_for_serial(1001, devices=devices)
    assert [v for _, v in chans] == [0, 1]
    assert chans[0][0].startswith("CAN1")
    assert "VN1630" not in chans[0][0]  # device already chosen


def test_discover_fallback_when_empty():
    with patch("can.interfaces.vector.get_channel_configs", return_value=[]):
        assert discover_vector_devices() == []
    with patch("xcp_pcan.can.transport.discover_vector_devices", return_value=[]):
        assert list_vector_channels() == list(VECTOR_CHANNELS)
    assert list_vector_channels_for_serial(None, devices=[]) == list(VECTOR_CHANNELS)


def test_device_name_from_hw_type_when_name_missing():
    cfg = _fake_cfg(name="", serial=9, hw_channel=0, channel_index=0)
    cfg.name = ""
    with patch("can.interfaces.vector.get_channel_configs", return_value=[cfg]):
        with patch("xcp_pcan.can.transport._is_can_channel", return_value=True):
            with patch("xcp_pcan.can.transport._channel_supports_fd", return_value=False):
                devices = discover_vector_devices()
    assert devices[0].device_name == "VN1630"
