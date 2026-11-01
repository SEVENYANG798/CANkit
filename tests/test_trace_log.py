"""Offline ASC / BLF / CSV trace readers."""

from pathlib import Path

import can
from can.io import ASCWriter, BLFWriter

from xcp_pcan.trace_log.readers import load_trace, sniff_format
from xcp_pcan.trace_log.types import TraceFormat

FIX = Path(__file__).parent / "fixtures"


def _write_sample_asc(path: Path) -> None:
    with ASCWriter(path) as writer:
        writer.on_message_received(
            can.Message(arbitration_id=0x6A6, data=bytes([0xFF, 0x00]), is_extended_id=False, timestamp=0.0)
        )
        writer.on_message_received(
            can.Message(
                arbitration_id=0x400006A7,
                data=bytes([0x01, 0x02, 0x03, 0x04]),
                is_extended_id=True,
                timestamp=0.001,
                is_fd=True,
            )
        )


def _write_sample_blf(path: Path) -> None:
    with BLFWriter(path) as writer:
        writer.on_message_received(
            can.Message(arbitration_id=0x123, data=b"\x11\x22", timestamp=10.0)
        )
        writer.on_message_received(
            can.Message(arbitration_id=0x123, data=b"\x33\x44", timestamp=10.5)
        )


def _write_sample_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "Line,Abs Time(Sec),Rel Time (Sec),Timestamp,Status,Er,Tx,Description,Network,Node,Arb ID,Remote,Xtd,B1,B2,B3,B4,B5,B6,B7,B8,MoreBytes,RTC Time",
                "1,0,0,1ms,0,F,F,FCP_127,CAN1,,127,1-1-0,F,AA BB CC DD,,,,,,,,,",
                "2,0.001,0,1ms,0,F,T,XCP_TX,CAN1,,6A6,1-1-0,F,FF,00,11,22,33,44,55,66,,",
                "3,0.002,0,1ms,0,T,F,ERR,CAN1,,1B1,1-1-0,F,DE AD BE EF,,,,,,,,,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_sniff_format():
    assert sniff_format("a.asc") is TraceFormat.ASC
    assert sniff_format("b.BLF") is TraceFormat.BLF
    assert sniff_format("c.csv") is TraceFormat.CSV


def test_load_asc_blf_csv(tmp_path: Path):
    asc = tmp_path / "sample.asc"
    blf = tmp_path / "sample.blf"
    csv_path = tmp_path / "sample.csv"
    _write_sample_asc(asc)
    _write_sample_blf(blf)
    _write_sample_csv(csv_path)

    asc_frames = load_trace(asc)
    assert len(asc_frames) == 2
    assert asc_frames[0].arbitration_id == 0x6A6
    assert asc_frames[0].data == b"\xff\x00"
    assert asc_frames[1].is_extended

    blf_frames = load_trace(blf)
    assert len(blf_frames) == 2
    assert blf_frames[0].arbitration_id == 0x123
    assert abs(blf_frames[1].timestamp - 0.5) < 1e-6

    csv_frames = load_trace(csv_path)
    assert len(csv_frames) == 3
    assert csv_frames[0].arbitration_id == 127
    assert csv_frames[0].data == bytes.fromhex("AABBCCDD")
    assert csv_frames[1].arbitration_id == 0x6A6
    assert not csv_frames[1].is_rx  # Tx=T
    assert csv_frames[2].arbitration_id == 0x1B1
    assert csv_frames[2].is_error
