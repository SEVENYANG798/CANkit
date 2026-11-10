"""DBC decode for Trace Log viewer."""

from pathlib import Path

from xcp_pcan.trace_log.dbc_decode import DbcDatabase
from xcp_pcan.trace_log.types import TraceFrame

_MIN_DBC = """\
VERSION ""


NS_ :
	NS_DESC_
	CM_
	BA_DEF_
	BA_
	VAL_
	CAT_DEF_
	CAT_
	FILTER
	BA_DEF_DEF_
	EV_DATA_
	ENVVAR_DATA_
	SGTYPE_
	SGTYPE_VAL_
	BA_DEF_SGTYPE_
	BA_SGTYPE_
	SIG_TYPE_REF_
	VAL_TABLE_
	SIG_GROUP_
	SIG_VALTYPE_
	SIGTYPE_VALTYPE_
	BO_TX_BU_
	BA_DEF_REL_
	BA_REL_
	BA_DEF_DEF_REL_
	BU_SG_REL_
	BU_EV_REL_
	BU_BO_REL_
	SG_MUL_VAL_

BS_:

BU_: ECU

BO_ 1702 TestMsg: 8 ECU
 SG_ Counter : 0|8@1+ (1,0) [0|255] "" ECU
 SG_ Speed : 8|16@1+ (0.1,0) [0|100] "km/h" ECU
"""


def test_dbc_decode_signals(tmp_path: Path):
    path = tmp_path / "sample.dbc"
    path.write_text(_MIN_DBC, encoding="utf-8")
    dbc = DbcDatabase(path)
    assert dbc.message_count == 1
    assert dbc.message_name(0x6A6) == "TestMsg"
    assert dbc.message_names()[0x6A6] == "TestMsg"

    # Counter=0xFF, Speed raw=0x0064 → 10.0 km/h (little-endian)
    frame = TraceFrame(
        index=0,
        timestamp=0.0,
        arbitration_id=0x6A6,
        data=bytes([0xFF, 0x64, 0x00, 0, 0, 0, 0, 0]),
    )
    decoded = dbc.decode(frame)
    assert decoded is not None
    assert decoded.name == "TestMsg"
    assert decoded.error == ""
    values = {s.name: s.value for s in decoded.signals}
    assert values["Counter"] == 255
    assert abs(float(values["Speed"]) - 10.0) < 1e-6
    detail = dbc.format_detail(frame)
    assert detail is not None
    assert "Counter" in detail
    assert "km/h" in detail

    unknown = TraceFrame(index=1, timestamp=0.0, arbitration_id=0x123, data=b"\x00")
    assert dbc.decode(unknown) is None

    msgs = dbc.list_messages()
    assert msgs == [("TestMsg", 0x6A6)]
    infos = dbc.list_signals()
    assert {i.signal for i in infos} == {"Counter", "Speed"}
    assert infos[0].frame_id <= infos[-1].frame_id
    frames = [
        TraceFrame(
            index=0,
            timestamp=1.0,
            arbitration_id=0x6A6,
            data=bytes([0x01, 0x64, 0x00, 0, 0, 0, 0, 0]),
        ),
        TraceFrame(
            index=1,
            timestamp=1.1,
            arbitration_id=0x6A6,
            data=bytes([0x02, 0xC8, 0x00, 0, 0, 0, 0, 0]),
        ),
        TraceFrame(index=2, timestamp=1.2, arbitration_id=0x123, data=b"\x00"),
    ]
    msg, unit, samples = dbc.extract_signal_series(
        frames, frame_id=0x6A6, signal="Speed"
    )
    assert msg == "TestMsg"
    assert unit == "km/h"
    assert len(samples) == 2
    assert abs(float(samples[0].numeric) - 10.0) < 1e-6
    assert abs(float(samples[1].numeric) - 20.0) < 1e-6
