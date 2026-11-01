"""Seed&Key unlock and calibration page tests."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from xcp_pcan.can.pcan_bus import CanFrame
from xcp_pcan.xcp import protocol as proto
from xcp_pcan.xcp.master import XcpMaster, XcpSessionConfig
from xcp_pcan.xcp.seedkey import (
    CallableSeedKeyProvider,
    EchoSeedKeyProvider,
    load_python_seedkey,
    load_seedkey,
)


class FakeTransport:
    def __init__(self) -> None:
        self.is_open = True
        self.fd = False
        self._listeners: list[Callable[[CanFrame], None]] = []
        self._auto_reply: Callable[[bytes], bytes | None] | None = None
        self.sent: list[bytes] = []

    def add_listener(self, cb: Callable[[CanFrame], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[CanFrame], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def set_auto_reply(self, fn: Callable[[bytes], bytes | None]) -> None:
        self._auto_reply = fn

    def send(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
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


def test_encode_get_seed_unlock_cal_page():
    assert proto.encode_get_seed(0, proto.Resource.CAL_PAG)[0] == proto.Command.GET_SEED
    unlock = proto.encode_unlock(4, b"\x01\x02\x03\x04")
    assert unlock[0] == proto.Command.UNLOCK
    assert unlock[1] == 4
    assert unlock[2:6] == b"\x01\x02\x03\x04"
    cal = proto.encode_set_cal_page(0x03, 1, 2)
    assert cal[0] == proto.Command.SET_CAL_PAGE
    assert cal[1:4] == bytes([0x03, 1, 2])
    page = proto.parse_get_cal_page_response(
        proto.parse_response(bytes([0xFF, 0x00, 0x05]))
    )
    assert page == 5


def test_parse_get_seed_already_unlocked():
    length, chunk = proto.parse_get_seed_response(proto.parse_response(bytes([0xFF, 0x00])))
    assert length == 0
    assert chunk == b""


def test_echo_and_python_plugin(tmp_path: Path):
    echo = EchoSeedKeyProvider()
    assert echo.compute_key(1, b"\xaa\xbb") == b"\xaa\xbb"

    plugin = tmp_path / "sk.py"
    plugin.write_text(
        "def compute_key(resource, seed):\n    return bytes([resource & 0xFF]) + seed[::-1]\n",
        encoding="utf-8",
    )
    prov = load_python_seedkey(plugin)
    assert prov.compute_key(0x01, b"\x11\x22") == b"\x01\x22\x11"
    assert load_seedkey(plugin).compute_key(2, b"\x01") == b"\x02\x01"


def test_unlock_flow_and_auto_cal_page():
    transport = FakeTransport()
    state = {"seeded": False, "unlocked": False, "cal": None}

    def reply(req: bytes) -> bytes | None:
        cmd = req[0]
        if cmd == proto.Command.CONNECT:
            # resource = CAL|DAQ
            return bytes([0xFF, 0x03, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        if cmd == proto.Command.GET_SEED:
            resource = req[2]
            if resource == proto.Resource.CAL_PAG and not state["unlocked"]:
                state["seeded"] = True
                return bytes([0xFF, 0x02, 0xDE, 0xAD])  # length=2, seed
            return bytes([0xFF, 0x00])  # already unlocked / DAQ open
        if cmd == proto.Command.UNLOCK:
            assert req[1] == 2
            assert req[2:4] == b"\xde\xad"  # echo key
            state["unlocked"] = True
            return bytes([0xFF, 0x00])  # nothing protected
        if cmd == proto.Command.SET_CAL_PAGE:
            state["cal"] = (req[1], req[2], req[3])
            return bytes([0xFF])
        if cmd == proto.Command.GET_CAL_PAGE:
            return bytes([0xFF, 0x00, 0x07])
        return bytes([0xFF])

    transport.set_auto_reply(reply)
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(
            timeout_s=0.3,
            cto_retries=0,
            reconnect_enabled=False,
            auto_unlock=True,
            unlock_resources=proto.Resource.CAL_PAG | proto.Resource.DAQ,
            seed_key=EchoSeedKeyProvider(),
            auto_set_cal_page=True,
            cal_page_mode=0x03,
            cal_segment=0,
            cal_page=1,
        ),
    )
    info = master.connect()
    assert info.available_resources == 0x03
    assert state["seeded"]
    assert state["unlocked"]
    assert (master.unlocked_mask & proto.Resource.CAL_PAG) != 0
    assert state["cal"] == (0x03, 0, 1)
    assert master.get_cal_page(0x03, 0) == 7


def test_callable_provider_unlock_resource():
    transport = FakeTransport()

    def reply(req: bytes) -> bytes | None:
        cmd = req[0]
        if cmd == proto.Command.CONNECT:
            return bytes([0xFF, 0x01, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
        if cmd == proto.Command.GET_SEED:
            return bytes([0xFF, 0x03, 0x01, 0x02, 0x03])
        if cmd == proto.Command.UNLOCK:
            # Expect key = seed xor 0xFF
            assert req[2:5] == bytes([0xFE, 0xFD, 0xFC])
            return bytes([0xFF, 0x00])
        return bytes([0xFF])

    transport.set_auto_reply(reply)
    provider = CallableSeedKeyProvider(
        lambda _r, seed: bytes(b ^ 0xFF for b in seed), name="xor"
    )
    master = XcpMaster(
        transport,  # type: ignore[arg-type]
        XcpSessionConfig(timeout_s=0.3, cto_retries=0, reconnect_enabled=False),
    )
    master.connect(apply_post=False)
    assert master.unlock_resource(proto.Resource.CAL_PAG, provider) is True
