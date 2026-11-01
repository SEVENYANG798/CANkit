"""Polling pause and exclusive write/readback must not interleave CTOs."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from xcp_pcan.can.pcan_bus import CanFrame
from xcp_pcan.symbols.types import DataType, Variable
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.master import XcpMaster, XcpSessionConfig
from xcp_pcan.xcp.poller import Poller


class FakeTransport:
    def __init__(self) -> None:
        self.is_open = True
        self.fd = False
        self._listeners: list[Callable[[CanFrame], None]] = []
        self._auto_reply: Callable[[bytes], bytes | None] | None = None
        self.sent: list[bytes] = []
        self._send_lock = threading.Lock()

    def add_listener(self, cb: Callable[[CanFrame], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[CanFrame], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def set_auto_reply(self, fn: Callable[[bytes], bytes | None]) -> None:
        self._auto_reply = fn

    def send(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        with self._send_lock:
            self.sent.append(bytes(data))
        if self._auto_reply is None:
            return
        reply = self._auto_reply(data)
        if reply is None:
            return

        def _emit() -> None:
            frame = CanFrame(arbitration_id=0x7E1, data=reply)
            for cb in list(self._listeners):
                cb(frame)

        threading.Timer(0.002, _emit).start()


def _ok_connect_reply(req: bytes) -> bytes | None:
    cmd = req[0]
    if cmd == proto.Command.CONNECT:
        return bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
    if cmd == proto.Command.SHORT_UPLOAD:
        return bytes([0xFF, 0x11])
    if cmd in (
        proto.Command.SHORT_DOWNLOAD,
        proto.Command.SET_MTA,
        proto.Command.DOWNLOAD,
        proto.Command.DOWNLOAD_NEXT,
    ):
        return bytes([0xFF])
    if cmd == proto.Command.SYNCH:
        return bytes([0xFE, 0x00])
    return bytes([0xFF])


def test_poller_pause_stops_cto_traffic():
    transport = FakeTransport()
    transport.set_auto_reply(_ok_connect_reply)
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(timeout_s=0.2, cto_retries=0, reconnect_enabled=False),
    )
    master.connect()
    var = Variable(
        name="v",
        address=0x1000,
        size=1,
        datatype=DataType.UBYTE,
        addr_ext=0,
    )
    samples: list[object] = []
    poller = Poller(master, on_sample=lambda s: samples.append(s))
    poller.set_variables([var])
    poller.set_interval_ms(20)
    poller.start()
    time.sleep(0.08)
    assert len(samples) >= 1
    before = len(transport.sent)
    poller.pause()
    assert poller.is_paused
    time.sleep(0.1)
    during = len(transport.sent)
    assert during == before  # no new CTOs while paused
    poller.resume()
    time.sleep(0.08)
    assert len(transport.sent) > during
    poller.stop()


def test_exclusive_write_blocks_concurrent_upload():
    """While exclusive()+write runs, a parallel short_upload waits."""
    transport = FakeTransport()
    gate = threading.Event()
    in_download = threading.Event()

    def reply(req: bytes) -> bytes | None:
        cmd = req[0]
        if cmd == proto.Command.CONNECT:
            return bytes([0xFF, 0x05, 0x00, 0x40, 0x40, 0x00, 0x01, 0x01])
        if cmd == proto.Command.SET_MTA:
            return bytes([0xFF])
        if cmd == proto.Command.DOWNLOAD:
            in_download.set()
            gate.wait(timeout=1.0)
            return bytes([0xFF])
        if cmd == proto.Command.SHORT_UPLOAD:
            return bytes([0xFF, 0x22])
        if cmd == proto.Command.SHORT_DOWNLOAD:
            return bytes([0xFF])
        return bytes([0xFF])

    transport.set_auto_reply(reply)
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(timeout_s=1.0, cto_retries=0, reconnect_enabled=False, fd=True),
    )
    # Force multi-CTO write path: data longer than SHORT_DOWNLOAD budget
    master.connect()
    # max_cto=0x40 → short_download fits up to 56; use 60 bytes via SET_MTA+DOWNLOAD
    payload = bytes(range(60))

    upload_done = threading.Event()
    saw_block = {"ok": False}

    def writer() -> None:
        with master.exclusive():
            master.write_memory(0x2000, payload)

    def reader() -> None:
        assert in_download.wait(timeout=1.0)

        def do_upload() -> None:
            master.short_upload(1, 0x1000)
            upload_done.set()

        tu = threading.Thread(target=do_upload)
        tu.start()
        # Writer still holds exclusive (waiting on gate) — upload must not finish yet
        time.sleep(0.08)
        saw_block["ok"] = not upload_done.is_set()
        gate.set()
        tu.join(timeout=2.0)

    tw = threading.Thread(target=writer)
    tr = threading.Thread(target=reader)
    tw.start()
    tr.start()
    tw.join(timeout=3.0)
    tr.join(timeout=3.0)
    assert upload_done.is_set()
    assert saw_block["ok"]
