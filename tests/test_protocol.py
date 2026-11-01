"""XCP protocol encode/decode unit tests."""

from xcp_pcan.xcp import protocol as proto


def test_encode_connect():
    frame = proto.encode_connect()
    assert len(frame) == 8
    assert frame[0] == proto.Command.CONNECT
    assert frame[1] == 0x00


def test_encode_short_upload_little():
    frame = proto.encode_short_upload(2, 0x20000004, little_endian=True)
    assert frame[0] == proto.Command.SHORT_UPLOAD
    assert frame[1] == 2
    assert frame[3] == 0  # addr_ext
    assert frame[4:8] == (0x20000004).to_bytes(4, "little")


def test_encode_short_upload_big():
    frame = proto.encode_short_upload(4, 0x11223344, little_endian=False)
    assert frame[4:8] == (0x11223344).to_bytes(4, "big")


def test_encode_set_mta():
    frame = proto.encode_set_mta(0xAABBCCDD, addr_ext=1, little_endian=True)
    assert frame[0] == proto.Command.SET_MTA
    assert frame[3] == 1
    assert frame[4:8] == (0xAABBCCDD).to_bytes(4, "little")


def test_parse_connect_response():
    # RES, resource, comm_mode_basic, max_cto, max_dto_lo, max_dto_hi, proto_ver, transport_ver
    data = bytes([0xFF, 0x05, 0x00, 0x08, 0x08, 0x00, 0x01, 0x01])
    resp = proto.parse_response(data)
    info = proto.parse_connect_response(resp)
    assert info.max_cto == 8
    assert info.max_dto == 8
    assert info.byte_order_intel is True
    assert info.address_granularity == 1


def test_parse_connect_motorola():
    # bit0 of comm_mode_basic = 1 → Motorola
    data = bytes([0xFF, 0x01, 0x01, 0x08, 0x08, 0x00, 0x01, 0x01])
    info = proto.parse_connect_response(proto.parse_response(data))
    assert info.byte_order_intel is False


def test_parse_error():
    resp = proto.parse_response(bytes([0xFE, 0x24]))
    assert resp.is_error
    assert resp.error_code == proto.ErrCode.ACCESS_DENIED


def test_daq_stubs_exist():
    # Reserved helpers for future DAQ — must remain importable.
    frame = proto.encode_alloc_daq(2)
    assert frame[0] == proto.Command.ALLOC_DAQ
    frame2 = proto.encode_start_stop_synch(0)
    assert frame2[0] == proto.Command.START_STOP_SYNCH
