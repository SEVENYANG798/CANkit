"""Project file round-trip."""

from pathlib import Path
from unittest.mock import patch

from xcp_pcan.project import (
    ConnectionSettings,
    ProjectFile,
    ResilienceSettings,
    SecuritySettings,
    ensure_user_default_project,
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


def test_ensure_user_default_project_seeds_once(tmp_path: Path):
    factory = tmp_path / "factory"
    factory.mkdir()
    factory_file = factory / "default.xcpp"
    save_project(
        factory_file,
        ProjectFile(connection=ConnectionSettings(master_can_id="0xABC")),
    )
    user_dir = tmp_path / "user"
    user_file = user_dir / "default.xcpp"
    with (
        patch("xcp_pcan.project.default_project_path", return_value=user_file),
        patch(
            "xcp_pcan.project.bundled_factory_project_path",
            return_value=factory_file,
        ),
    ):
        assert ensure_user_default_project() == user_file
        assert user_file.is_file()
        assert load_project(user_file).connection.master_can_id == "0xABC"
        # Second call must not overwrite user edits.
        save_project(
            user_file,
            ProjectFile(connection=ConnectionSettings(master_can_id="0xDEF")),
        )
        ensure_user_default_project()
        assert load_project(user_file).connection.master_can_id == "0xDEF"
