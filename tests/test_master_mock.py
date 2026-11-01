"""XcpMaster tests with a fake CAN transport."""

from __future__ import annotations

import threading
from collections.abc import Callable

from xcp_pcan.can.pcan_bus import CanFrame
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.master import XcpMaster, XcpSessionConfig


class FakeTransport:
    def __init__(self) -> None:
        self.is_open = True
        self.fd = False
        self._listeners: list[Callable[[CanFrame], None]] = []
        self.sent: list[tuple[int, bytes]] = []
        self._auto_reply: Callable[[bytes], bytes] | None = None

    def add_listener(self, cb: Callable[[CanFrame], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[CanFrame], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def set_auto_reply(self, fn: Callable[[bytes], bytes]) -> None:
        self._auto_reply = fn

    def send(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        self.sent.append((arbitration_id, data))
        if self._auto_reply is None:
            return
        reply = self._auto_reply(data)

        def _emit() -> None:
            frame = CanFrame(arbitration_id=0x7E1, data=reply)
            for cb in list(self._listeners):
                cb(frame)

        # Emit asynchronously to mimic bus latency
        threading.Timer(0.01, _emit).start()


def test_connect_and_short_upload():
    transport = FakeTransport()

    def reply(req: bytes) -> bytes:
        cmd = req[0]
        if cmd == proto.Command.CONNECT:
            return bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        if cmd == proto.Command.SHORT_UPLOAD:
            # echo size bytes of 0x11
            size = req[1]
            return bytes([0xFF]) + bytes([0x11] * size)
        if cmd == proto.Command.DISCONNECT:
            return bytes([0xFF])
        return bytes([0xFE, 0x20])

    transport.set_auto_reply(reply)
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(master_can_id=0x667, slave_can_id=0x7E1, timeout_s=1.0),
    )
    info = master.connect()
    assert info.max_cto == 8
    assert master.is_connected
    data = master.short_upload(2, 0x20000000)
    assert data[:2] == b"\x11\x11"
    master.disconnect()
    assert not master.is_connected


def test_read_memory_chunks():
    transport = FakeTransport()

    def reply(req: bytes) -> bytes:
        cmd = req[0]
        if cmd == proto.Command.CONNECT:
            return bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        if cmd == proto.Command.SHORT_UPLOAD:
            size = req[1]
            return bytes([0xFF]) + bytes(range(size))
        return bytes([0xFE, 0x20])

    transport.set_auto_reply(reply)
    master = XcpMaster(transport, XcpSessionConfig(timeout_s=1.0))  # type: ignore[arg-type]
    master.connect()
    data = master.read_memory(0x1000, 5)
    assert len(data) == 5


def test_write_memory_set_mta_download():
    transport = FakeTransport()
    seen: list[int] = []

    def reply(req: bytes) -> bytes:
        cmd = req[0]
        seen.append(cmd)
        if cmd == proto.Command.CONNECT:
            # max_cto=8 → SHORT_DOWNLOAD (8+2) won't fit → SET_MTA+DOWNLOAD
            return bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        if cmd in (
            proto.Command.SET_MTA,
            proto.Command.DOWNLOAD,
            proto.Command.DOWNLOAD_NEXT,
            proto.Command.SHORT_DOWNLOAD,
        ):
            return bytes([0xFF])
        return bytes([0xFE, 0x20])

    transport.set_auto_reply(reply)
    master = XcpMaster(transport, XcpSessionConfig(timeout_s=1.0))  # type: ignore[arg-type]
    master.connect()
    master.write_memory(0x20001000, b"\x11\x22")
    assert proto.Command.SET_MTA in seen
    assert proto.Command.DOWNLOAD in seen

