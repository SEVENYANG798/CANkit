"""A2L IF_DATA XCP transport hint extraction."""

from pathlib import Path

from xcp_pcan.symbols.a2l_loader import load_a2l

FIX = Path(__file__).parent / "fixtures" / "sample.a2l"


def test_if_data_xcp_hints():
    store = load_a2l(FIX)
    assert store.transport is not None
    assert store.transport.master_can_id == 0x667
    assert store.transport.slave_can_id == 0x7E1
    assert store.transport.bitrate == 500_000
