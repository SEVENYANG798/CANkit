"""Cancel must propagate through progress reporters."""

import pytest

from xcp_pcan.symbols.a2l_loader import _report as a2l_report
from xcp_pcan.util.cancel import OperationCancelled, report_progress


def test_report_progress_reraises_cancel():
    def boom(_msg: str, _frac: float) -> None:
        raise OperationCancelled()

    with pytest.raises(OperationCancelled):
        report_progress(boom, "x", 0.5)


def test_report_progress_swallows_other_errors():
    def boom(_msg: str, _frac: float) -> None:
        raise RuntimeError("ui glitch")

    report_progress(boom, "x", 0.5)  # must not raise


def test_a2l_report_reraises_cancel():
    def boom(_msg: str, _frac: float) -> None:
        raise OperationCancelled()

    with pytest.raises(OperationCancelled):
        a2l_report(boom, "tok", 0.2)
