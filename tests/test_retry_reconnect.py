"""CTO retry and session recover tests."""

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
        self._auto_reply: Callable[[bytes], bytes] | None = None
        self.send_count = 0

    def add_listener(self, cb: Callable[[CanFrame], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[CanFrame], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def set_auto_reply(self, fn: Callable[[bytes], bytes]) -> None:
        self._auto_reply = fn

    def send(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        self.send_count += 1
        if self._auto_reply is None:
            return
        reply = self._auto_reply(data)
        if reply is None:
            return

        def _emit() -> None:
            frame = CanFrame(arbitration_id=0x7E1, data=reply)
            for cb in list(self._listeners):
                cb(frame)

        threading.Timer(0.005, _emit).start()


def test_cto_retries_then_success():
    transport = FakeTransport()
    state = {"n": 0}

    def reply(req: bytes) -> bytes | None:
        cmd = req[0]
        if cmd == proto.Command.CONNECT:
            return bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        if cmd == proto.Command.SHORT_UPLOAD:
            state["n"] += 1
            if state["n"] < 3:
                return None  # timeout
            return bytes([0xFF, 0xAA, 0xBB])
        return bytes([0xFE, 0x20])

    transport.set_auto_reply(reply)
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(
            timeout_s=0.05,
            cto_retries=3,
            cto_retry_delay_s=0.01,
            reconnect_enabled=False,
        ),
    )
    master.connect()
    data = master.short_upload(2, 0x1000)
    assert data[:2] == b"\xaa\xbb"
    assert state["n"] == 3


def test_recover_session():
    transport = FakeTransport()

    def reply(req: bytes) -> bytes:
        cmd = req[0]
        if cmd in (proto.Command.CONNECT, proto.Command.SYNCH):
            if cmd == proto.Command.SYNCH:
                return bytes([0xFE, 0x00])  # ERR SYNCH is fine
            return bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        return bytes([0xFF])

    transport.set_auto_reply(reply)
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(timeout_s=0.2, reconnect_max_attempts=2, reconnect_backoff_s=0.01),
    )
    master.connect()
    master._connected = False  # simulate link drop
    info = master.recover_session()
    assert master.is_connected
    assert info.max_cto == 8
