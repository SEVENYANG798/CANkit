"""Project / session persistence (connection + symbol file + watch lists)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROJECT_VERSION = 1
DEFAULT_PROJECT_NAME = "default.xcpp"


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


@dataclass
class ConnectionSettings:
    hardware: str = "pcan"
    channel: str | int = "PCAN_USBBUS1"
    vector_app_name: str = "XcpReader"
    vector_serial: int | None = None  # XL serial; None → app-channel / fallback
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: int = 2_000_000
    f_clock_mhz: int = 80
    master_can_id: str = "0x667"
    slave_can_id: str = "0x7E1"
    timeout_ms: int = 1000


@dataclass
class SecuritySettings:
    """Seed&Key unlock + calibration page (persisted in .xcpp)."""

    seedkey_path: str = ""
    dll_symbol: str = "XCP_ComputeKeyFromSeed"
    auto_unlock: bool = False
    unlock_cal: bool = True
    unlock_daq: bool = True
    unlock_stim: bool = False
    unlock_pgm: bool = False
    auto_set_cal_page: bool = False
    cal_mode_ecu: bool = True
    cal_mode_xcp: bool = True
    cal_segment: int = 0
    cal_page: int = 0


@dataclass
class ResilienceSettings:
    """CTO retry / reconnect / DAQ silence timeout (persisted in .xcpp)."""

    cto_retries: int = 2
    cto_retry_delay_ms: int = 50
    reconnect_enabled: bool = True
    reconnect_max_attempts: int = 5
    reconnect_backoff_ms: int = 400
    daq_dto_timeout_ms: int = 2000


@dataclass
class PollingSettings:
    interval_ms: int = 100
    watch: list[str] = field(default_factory=list)


@dataclass
class DaqSettings:
    event_channel: int = 0
    prescaler: int = 1
    timestamp: bool = False
    watch: list[str] = field(default_factory=list)


@dataclass
class ProjectFile:
    version: int = PROJECT_VERSION
    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    resilience: ResilienceSettings = field(default_factory=ResilienceSettings)
    symbol_path: str = ""
    polling: PollingSettings = field(default_factory=PollingSettings)
    daq: DaqSettings = field(default_factory=DaqSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "connection": asdict(self.connection),
            "security": asdict(self.security),
            "resilience": asdict(self.resilience),
            "symbol_path": self.symbol_path,
            "polling": asdict(self.polling),
            "daq": asdict(self.daq),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectFile:
        conn = data.get("connection") or {}
        sec = data.get("security") or {}
        res = data.get("resilience") or {}
        poll = data.get("polling") or {}
        daq = data.get("daq") or {}
        return cls(
            version=int(data.get("version", PROJECT_VERSION)),
            connection=ConnectionSettings(
                hardware=str(conn.get("hardware", "pcan")),
                channel=conn.get("channel", "PCAN_USBBUS1"),
                vector_app_name=str(conn.get("vector_app_name", "XcpReader")),
                vector_serial=_optional_int(conn.get("vector_serial")),
                bitrate=int(conn.get("bitrate", 500_000)),
                fd=bool(conn.get("fd", False)),
                data_bitrate=int(conn.get("data_bitrate", 2_000_000)),
                f_clock_mhz=int(conn.get("f_clock_mhz", 80)),
                master_can_id=str(conn.get("master_can_id", "0x667")),
                slave_can_id=str(conn.get("slave_can_id", "0x7E1")),
                timeout_ms=int(conn.get("timeout_ms", 1000)),
            ),
            security=SecuritySettings(
                seedkey_path=str(sec.get("seedkey_path", "")),
                dll_symbol=str(sec.get("dll_symbol", "XCP_ComputeKeyFromSeed")),
                auto_unlock=bool(sec.get("auto_unlock", False)),
                unlock_cal=bool(sec.get("unlock_cal", True)),
                unlock_daq=bool(sec.get("unlock_daq", True)),
                unlock_stim=bool(sec.get("unlock_stim", False)),
                unlock_pgm=bool(sec.get("unlock_pgm", False)),
                auto_set_cal_page=bool(sec.get("auto_set_cal_page", False)),
                cal_mode_ecu=bool(sec.get("cal_mode_ecu", True)),
                cal_mode_xcp=bool(sec.get("cal_mode_xcp", True)),
                cal_segment=int(sec.get("cal_segment", 0)),
                cal_page=int(sec.get("cal_page", 0)),
            ),
            resilience=ResilienceSettings(
                cto_retries=int(res.get("cto_retries", 2)),
                cto_retry_delay_ms=int(res.get("cto_retry_delay_ms", 50)),
                reconnect_enabled=bool(res.get("reconnect_enabled", True)),
                reconnect_max_attempts=int(res.get("reconnect_max_attempts", 5)),
                reconnect_backoff_ms=int(res.get("reconnect_backoff_ms", 400)),
                daq_dto_timeout_ms=int(res.get("daq_dto_timeout_ms", 2000)),
            ),
            symbol_path=str(data.get("symbol_path", "")),
            polling=PollingSettings(
                interval_ms=int(poll.get("interval_ms", 100)),
                watch=list(poll.get("watch") or []),
            ),
            daq=DaqSettings(
                event_channel=int(daq.get("event_channel", 0)),
                prescaler=int(daq.get("prescaler", 1)),
                timestamp=bool(daq.get("timestamp", False)),
                watch=list(daq.get("watch") or []),
            ),
        )


def default_project_dir() -> Path:
    base = Path.home() / ".xcp_pcan"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_project_path() -> Path:
    return default_project_dir() / DEFAULT_PROJECT_NAME


def save_project(path: str | Path, project: ProjectFile) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(project.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_project(path: str | Path) -> ProjectFile:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid project file")
    return ProjectFile.from_dict(data)
