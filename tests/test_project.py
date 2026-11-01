"""Project file round-trip."""

from pathlib import Path

from xcp_pcan.project import (
    ConnectionSettings,
    ProjectFile,
    ResilienceSettings,
    SecuritySettings,
    load_project,
    save_project,
)


def test_project_roundtrip(tmp_path: Path):
    path = tmp_path / "demo.xcpp"
    project = ProjectFile(
        connection=ConnectionSettings(
            hardware="vector",
            channel=1,
            vector_serial=1001,
            bitrate=250_000,
            master_can_id="0x100",
            slave_can_id="0x200",
        ),
        security=SecuritySettings(
            seedkey_path=r"D:\keys\seedkey.py",
            auto_unlock=True,
            unlock_cal=True,
            auto_set_cal_page=True,
            cal_page=1,
        ),
        resilience=ResilienceSettings(
            cto_retries=3,
            reconnect_enabled=True,
            daq_dto_timeout_ms=3500,
        ),
        symbol_path=r"D:\ecu\app.a2l",
    )
    project.polling.watch = ["CalGain", "EngineSpeed"]
    save_project(path, project)
    loaded = load_project(path)
    assert loaded.connection.hardware == "vector"
    assert loaded.connection.channel == 1
    assert loaded.connection.vector_serial == 1001
    assert loaded.connection.bitrate == 250_000
    assert loaded.symbol_path.endswith("app.a2l")
    assert loaded.polling.watch == ["CalGain", "EngineSpeed"]
    assert loaded.security.auto_unlock is True
    assert loaded.security.seedkey_path.endswith("seedkey.py")
    assert loaded.security.cal_page == 1
    assert loaded.resilience.cto_retries == 3
    assert loaded.resilience.daq_dto_timeout_ms == 3500
