"""Backward-compatible import check for DaqEngine. """

from xcp_pcan.xcp.daq import DaqEngine


def test_daq_engine_importable():
    assert DaqEngine is not None
