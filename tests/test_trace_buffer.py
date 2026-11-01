"""Trace buffer unit tests."""

from xcp_pcan.ui.trace_buffer import TraceBuffer


def test_trace_append_and_relative():
    buf = TraceBuffer(maxlen=100)
    buf.append("A", 1000.0, 1.0, unit="rpm")
    buf.append("A", 1000.5, 2.0)
    buf.append("B", 1000.2, 9.0)
    assert buf.names() == ["A", "B"]
    xs, ys = buf.relative_xy("A")
    assert xs == [0.0, 0.5]
    assert ys == [1.0, 2.0]
    assert buf.unit("A") == "rpm"


def test_trace_clear():
    buf = TraceBuffer()
    buf.append("A", 1.0, 1.0)
    buf.clear()
    assert buf.names() == []


def test_trace_export_csv(tmp_path):
    buf = TraceBuffer()
    buf.append("A", 1000.0, 1.5, unit="V")
    buf.append("B", 1000.1, 2.0)
    out = tmp_path / "samples.csv"
    rows = buf.export_csv(out)
    assert rows == 2
    text = out.read_text(encoding="utf-8")
    assert "time_s,timestamp,name,value,unit" in text
    assert "A" in text and "1.5" in text and "V" in text
