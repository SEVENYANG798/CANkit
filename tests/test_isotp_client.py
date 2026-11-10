"""ISO-TP client smoke tests with a mock transport."""

from __future__ import annotations

import pytest

from xcp_pcan.can.frame_util import CanFrame
from xcp_pcan.diag.isotp import IsoTpClient, IsoTpConfig, IsoTpTimeout


class _MockBus:
    def __init__(self, *, fd: bool = False) -> None:
        self.is_open = True
        self.fd = fd
        self._listeners: list = []
        self.sent: list[tuple[int, bytes]] = []

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def send(self, arbitration_id, data, **_kwargs) -> None:
        payload = bytes(data)
        self.sent.append((arbitration_id, payload))
        # Echo TX (ignored by client via is_rx=False)
        for cb in list(self._listeners):
            cb(
                CanFrame(
                    arbitration_id=arbitration_id,
                    data=payload,
                    is_rx=False,
                )
            )


def test_isotp_single_frame_roundtrip() -> None:
    bus = _MockBus(fd=True)

    def ecu_reply(frame: CanFrame) -> None:
        if not frame.is_rx and frame.arbitration_id == 0x73D:
            # Request SF: 03 22 F1 87 CC CC CC CC
            if frame.data[0] == 0x03 and frame.data[1:4] == bytes([0x22, 0xF1, 0x87]):
                resp = bytes([0x05, 0x62, 0xF1, 0x87, 0xAA, 0xBB, 0xCC, 0xCC])
                for cb in list(bus._listeners):
                    cb(CanFrame(arbitration_id=0x7BD, data=resp, is_rx=True))

    bus.add_listener(ecu_reply)
    client = IsoTpClient(
        bus,  # type: ignore[arg-type]
        IsoTpConfig(tx_id=0x73D, rx_id=0x7BD, fd=True, pad_byte=0xCC, timeout_s=1.0),
    )
    try:
        resp = client.request(bytes([0x22, 0xF1, 0x87]))
    finally:
        client.close()
    assert resp == bytes([0x62, 0xF1, 0x87, 0xAA, 0xBB])
    assert bus.sent[0][0] == 0x73D
    assert bus.sent[0][1] == bytes([0x03, 0x22, 0xF1, 0x87, 0xCC, 0xCC, 0xCC, 0xCC])


def test_isotp_multiframe_response() -> None:
    bus = _MockBus()

    def ecu_reply(frame: CanFrame) -> None:
        if frame.is_rx or frame.arbitration_id != 0x7E0:
            return
        # TesterPresent SF
        if frame.data[0] == 0x02 and frame.data[1] == 0x3E:
            # FF: length 20, data starts with 62 F1 90 …
            body = bytes([0x62, 0xF1, 0x90]) + bytes(range(17))
            assert len(body) == 20
            ff = bytes([0x10, 0x14]) + body[:6]
            for cb in list(bus._listeners):
                cb(CanFrame(arbitration_id=0x7E8, data=ff + bytes(0), is_rx=True))
            return
        # FC from tester → send CFs
        if frame.data[0] == 0x30:
            body = bytes([0x62, 0xF1, 0x90]) + bytes(range(17))
            rest = body[6:]
            sn = 1
            while rest:
                chunk = rest[:7]
                rest = rest[7:]
                cf = bytes([0x20 | sn]) + chunk
                cf = cf + bytes(8 - len(cf))
                for cb in list(bus._listeners):
                    cb(CanFrame(arbitration_id=0x7E8, data=cf, is_rx=True))
                sn = (sn + 1) & 0x0F

    bus.add_listener(ecu_reply)
    client = IsoTpClient(
        bus,  # type: ignore[arg-type]
        IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, timeout_s=1.0),
    )
    try:
        resp = client.request(bytes([0x3E, 0x00]))
    finally:
        client.close()
    assert resp[0:3] == bytes([0x62, 0xF1, 0x90])
    assert len(resp) == 20


def test_isotp_timeout() -> None:
    bus = _MockBus()
    client = IsoTpClient(
        bus,  # type: ignore[arg-type]
        IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, timeout_s=0.15),
    )
    try:
        with pytest.raises(IsoTpTimeout):
            client.request(bytes([0x22, 0xF1, 0x90]))
    finally:
        client.close()


def test_uds_decode_positive_and_nrc() -> None:
    from xcp_pcan.diag.uds_decode import describe_uds, format_uds_message

    ev = describe_uds(bytes([0x62, 0xF1, 0x90, 0x01]), is_request=False)
    assert ev.kind == "Positive Response"
    assert "0xF190" in ev.parameters
    neg = describe_uds(bytes([0x7F, 0x22, 0x31]), is_request=False)
    assert neg.kind == "Negative Response"
    assert "requestOutOfRange" in neg.parameters
    # legacy one-liner still works
    assert "Positive" in format_uds_message(bytes([0x62, 0xF1, 0x90]), direction="RX")


def test_extract_uds_from_trace_sf() -> None:
    from xcp_pcan.ui.diag_console_dialog import _extract_uds_payload

    assert _extract_uds_payload(bytes.fromhex("10 03")) == bytes.fromhex("10 03")
    assert _extract_uds_payload(bytes.fromhex("02 10 02 CC CC CC CC CC")) == bytes.fromhex(
        "10 02"
    )
    assert _extract_uds_payload(bytes.fromhex("02 10 03 CC CC CC CC CC")) == bytes.fromhex(
        "10 03"
    )
    assert _extract_uds_payload(bytes.fromhex("03 22 F1 87 CC CC CC CC")) == bytes.fromhex(
        "22 F1 87"
    )


def test_build_sf_wire_frame_matches_trace() -> None:
    from xcp_pcan.diag.isotp import build_sf_wire_frame

    assert build_sf_wire_frame(
        bytes([0x22, 0xF1, 0x87]), pad_byte=0xCC, fd=True
    ) == bytes([0x03, 0x22, 0xF1, 0x87, 0xCC, 0xCC, 0xCC, 0xCC])
    assert build_sf_wire_frame(
        bytes([0x10, 0x02]), pad_byte=0xCC, fd=False
    ) == bytes([0x02, 0x10, 0x02, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC])
