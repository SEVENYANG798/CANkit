"""CAN FD payload sizing helpers."""

from xcp_pcan.can.frame_util import fit_can_payload, next_can_fd_length


def test_next_can_fd_length():
    assert next_can_fd_length(0) == 0
    assert next_can_fd_length(8) == 8
    assert next_can_fd_length(9) == 12
    assert next_can_fd_length(13) == 16
    assert next_can_fd_length(33) == 48
    assert next_can_fd_length(64) == 64


def test_fit_classic_always_8():
    assert len(fit_can_payload(b"\xff\x00", fd=False)) == 8
    assert fit_can_payload(bytes(10), fd=False) == bytes(8)


def test_fit_fd_pads_to_dlc():
    assert len(fit_can_payload(b"\xff\x00", fd=True)) == 2
    assert len(fit_can_payload(b"\xff\x00", fd=True, max_len=8)) == 8
    assert len(fit_can_payload(bytes(9), fd=True, max_len=9)) == 12
    assert len(fit_can_payload(bytes(40), fd=True, max_len=64)) == 64


def test_fit_fd_max_cto_64():
    payload = fit_can_payload(bytes([0xFF, 0x00]), fd=True, max_len=64)
    assert len(payload) == 64
    assert payload[0] == 0xFF
