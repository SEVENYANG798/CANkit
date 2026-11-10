"""Unified python-can transport for PEAK PCAN and Vector XL hardware."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import can
from can import BitTimingFd

from xcp_pcan.can.frame_util import (
    CAN_FD_DLC_LENGTHS,
    CanFrame,
    fit_can_payload,
)

logger = logging.getLogger(__name__)


class HardwareBackend(str, Enum):
    PCAN = "pcan"
    VECTOR = "vector"


PCAN_CHANNELS: list[tuple[str, str]] = [
    ("PCAN_USBBUS1", "PCAN_USBBUS1"),
    ("PCAN_USBBUS2", "PCAN_USBBUS2"),
    ("PCAN_USBBUS3", "PCAN_USBBUS3"),
    ("PCAN_USBBUS4", "PCAN_USBBUS4"),
    ("PCAN_PCIBUS1", "PCAN_PCIBUS1"),
    ("PCAN_PCIBUS2", "PCAN_PCIBUS2"),
]

# Fallback when XL Driver is missing / no hardware enumerated.
VECTOR_CHANNELS: list[tuple[str, int]] = [
    ("Channel 0", 0),
    ("Channel 1", 1),
    ("Channel 2", 2),
    ("Channel 3", 3),
]

_CHANNEL_NAME_RE = re.compile(r"\s+[Cc]hannel\s+\d+\s*$")


@dataclass(frozen=True)
class VectorChannelInfo:
    """One CAN-capable Vector XL channel."""

    label: str
    hw_channel: int
    channel_index: int
    serial: int
    device_name: str
    supports_fd: bool = False
    transceiver: str = ""


@dataclass(frozen=True)
class VectorDeviceInfo:
    """Physical Vector interface (e.g. VN1630) with its CAN channels."""

    label: str
    serial: int
    device_name: str
    channels: tuple[VectorChannelInfo, ...] = field(default_factory=tuple)


def list_pcan_channels() -> list[tuple[str, str]]:
    return list(PCAN_CHANNELS)


def list_vector_channels() -> list[tuple[str, int]]:
    """UI channel list: discovered CAN channels, or app-index fallback."""
    devices = discover_vector_devices()
    if not devices:
        return list(VECTOR_CHANNELS)
    out: list[tuple[str, int]] = []
    for dev in devices:
        for ch in dev.channels:
            out.append((ch.label, ch.hw_channel))
    return out


def _vector_device_name(cfg) -> str:
    """Best-effort type name: ``VN1630`` from channel name or hw_type enum."""
    raw_name = (getattr(cfg, "name", None) or "").strip()
    if raw_name:
        stripped = _CHANNEL_NAME_RE.sub("", raw_name).strip()
        if stripped:
            return stripped
    hw = getattr(cfg, "hw_type", None)
    enum_name = getattr(hw, "name", None)
    if isinstance(enum_name, str) and enum_name.startswith("XL_HWTYPE_"):
        return enum_name[len("XL_HWTYPE_") :]
    serial = getattr(cfg, "serial_number", 0)
    return f"Vector#{serial}"


def _channel_supports_fd(cfg) -> bool:
    try:
        from can.interfaces.vector import xldefine

        caps = getattr(cfg, "channel_capabilities", 0)
        flag = xldefine.XL_ChannelCapabilities.XL_CHANNEL_FLAG_CANFD_ISO_SUPPORT
        return bool(caps & flag)
    except Exception:  # noqa: BLE001
        return False


def _is_can_channel(cfg) -> bool:
    try:
        from can.interfaces.vector import xldefine

        bus_caps = getattr(cfg, "channel_bus_capabilities", 0)
        return bool(bus_caps & xldefine.XL_BusCapabilities.XL_BUS_ACTIVE_CAP_CAN)
    except Exception:  # noqa: BLE001
        return True


def discover_vector_devices() -> list[VectorDeviceInfo]:
    """Enumerate connected Vector XL interfaces via the driver.

    Returns an empty list when the XL API is unavailable or no CAN channels
    are present. Channel labels look like ``VN1630 · CAN1 · FD · SN 12345``.
    """
    try:
        from can.interfaces.vector import get_channel_configs
    except Exception as exc:  # noqa: BLE001
        logger.debug("Vector API import failed: %s", exc)
        return []

    try:
        configs = get_channel_configs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vector channel enumeration failed: %s", exc)
        return []

    by_serial: dict[int, list] = {}
    device_names: dict[int, str] = {}
    for cfg in configs or []:
        if not _is_can_channel(cfg):
            continue
        serial = int(getattr(cfg, "serial_number", 0) or 0)
        device_names.setdefault(serial, _vector_device_name(cfg))
        by_serial.setdefault(serial, []).append(cfg)

    devices: list[VectorDeviceInfo] = []
    for serial, cfgs in by_serial.items():
        cfgs_sorted = sorted(cfgs, key=lambda c: int(getattr(c, "hw_channel", 0)))
        device_name = device_names.get(serial, f"Vector#{serial}")
        channels: list[VectorChannelInfo] = []
        for cfg in cfgs_sorted:
            hw_ch = int(getattr(cfg, "hw_channel", 0))
            ch_idx = int(getattr(cfg, "channel_index", hw_ch))
            fd = _channel_supports_fd(cfg)
            xcvr = (getattr(cfg, "transceiver_name", None) or "").strip()
            parts = [device_name, f"CAN{hw_ch + 1}"]
            if fd:
                parts.append("FD")
            if xcvr:
                parts.append(xcvr)
            if serial:
                parts.append(f"SN {serial}")
            label = " · ".join(parts)
            channels.append(
                VectorChannelInfo(
                    label=label,
                    hw_channel=hw_ch,
                    channel_index=ch_idx,
                    serial=serial,
                    device_name=device_name,
                    supports_fd=fd,
                    transceiver=xcvr,
                )
            )
        if not channels:
            continue
        sn_part = f" (SN {serial})" if serial else ""
        devices.append(
            VectorDeviceInfo(
                label=f"{device_name}{sn_part}",
                serial=serial,
                device_name=device_name,
                channels=tuple(channels),
            )
        )

    devices.sort(key=lambda d: (d.device_name, d.serial))
    logger.info(
        "Vector discovery: %d device(s), %d CAN channel(s)",
        len(devices),
        sum(len(d.channels) for d in devices),
    )
    return devices


def _short_channel_label(ch: VectorChannelInfo) -> str:
    """Label for Channel combo once Device is already selected."""
    parts = [f"CAN{ch.hw_channel + 1}"]
    if ch.supports_fd:
        parts.append("FD")
    if ch.transceiver:
        parts.append(ch.transceiver)
    return " · ".join(parts)


def list_vector_channels_for_serial(
    serial: int | None,
    *,
    devices: list[VectorDeviceInfo] | None = None,
) -> list[tuple[str, int]]:
    """Channel labels/values for one device (hw_channel as value)."""
    devs = devices if devices is not None else discover_vector_devices()
    if not devs:
        return list(VECTOR_CHANNELS)
    if serial is None:
        return (
            list_vector_channels()
            if devices is None
            else [(ch.label, ch.hw_channel) for d in devs for ch in d.channels]
        )
    for dev in devs:
        if dev.serial == serial:
            return [(_short_channel_label(ch), ch.hw_channel) for ch in dev.channels]
    return list(VECTOR_CHANNELS)


class CanBusTransport:
    """Thin wrapper around ``can.Bus`` for PCAN or Vector."""

    def __init__(self) -> None:
        self._bus: can.BusABC | None = None
        self._rx_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listeners: list[Callable[[CanFrame], None]] = []
        self._lock = threading.Lock()
        self._fd = False
        self._backend = HardwareBackend.PCAN
        self._last_open: dict | None = None

    @property
    def is_open(self) -> bool:
        return self._bus is not None

    @property
    def fd(self) -> bool:
        return self._fd

    @property
    def backend(self) -> HardwareBackend:
        return self._backend

    def open(
        self,
        *,
        backend: HardwareBackend | str = HardwareBackend.PCAN,
        channel: str | int = "PCAN_USBBUS1",
        bitrate: int = 500_000,
        fd: bool = False,
        data_bitrate: int = 2_000_000,
        f_clock_mhz: int = 80,
        nom_sample_point: float = 80.0,
        data_sample_point: float = 80.0,
        app_name: str = "CANkit",
        serial: int | None = None,
    ) -> None:
        if self._bus is not None:
            self.close()

        backend = HardwareBackend(backend)
        self._backend = backend
        self._fd = bool(fd)
        self._last_open = {
            "backend": backend,
            "channel": channel,
            "bitrate": bitrate,
            "fd": fd,
            "data_bitrate": data_bitrate,
            "f_clock_mhz": f_clock_mhz,
            "nom_sample_point": nom_sample_point,
            "data_sample_point": data_sample_point,
            "app_name": app_name,
            "serial": serial,
        }

        if backend == HardwareBackend.PCAN:
            kwargs = self._pcan_kwargs(
                channel=str(channel),
                bitrate=bitrate,
                fd=fd,
                data_bitrate=data_bitrate,
                f_clock_mhz=f_clock_mhz,
                nom_sample_point=nom_sample_point,
                data_sample_point=data_sample_point,
            )
            err_hint = (
                "Ensure PEAK drivers are installed, the interface supports the selected "
                "mode, and PCANBasic.dll is available."
            )
        else:
            kwargs = self._vector_kwargs(
                channel=int(channel) if not isinstance(channel, int) else channel,
                bitrate=bitrate,
                fd=fd,
                data_bitrate=data_bitrate,
                app_name=app_name,
                serial=serial,
            )
            err_hint = (
                "Ensure Vector XL Driver is installed (vxlapi64.dll), the channel is "
                "assigned to this application in Vector Hardware Config, and no other "
                "tool exclusively owns the same channel."
            )

        try:
            self._bus = can.Bus(**kwargs)
        except Exception as exc:
            mode = (
                f"CAN FD nom={bitrate} data={data_bitrate}"
                if fd
                else f"Classic CAN @ {bitrate}"
            )
            raise RuntimeError(
                f"Failed to open {backend.value} channel {channel!r} ({mode}): {exc}\n"
                f"{err_hint}"
            ) from exc

        self._enable_error_frames()

        self._stop.clear()
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name=f"{backend.value}-rx", daemon=True
        )
        self._rx_thread.start()
        logger.info(
            "Bus opened: backend=%s channel=%s fd=%s bitrate=%s",
            backend.value,
            channel,
            fd,
            bitrate,
        )

    def reopen(self) -> None:
        """Re-open the bus using the last successful ``open()`` parameters."""
        if self._last_open is None:
            raise RuntimeError("Cannot reopen CAN bus: never opened")
        self.open(**dict(self._last_open))

    def _pcan_kwargs(
        self,
        *,
        channel: str,
        bitrate: int,
        fd: bool,
        data_bitrate: int,
        f_clock_mhz: int,
        nom_sample_point: float,
        data_sample_point: float,
    ) -> dict:
        kwargs: dict = {"interface": "pcan", "channel": channel}
        if fd:
            try:
                timing = BitTimingFd.from_sample_point(
                    f_clock=int(f_clock_mhz) * 1_000_000,
                    nom_bitrate=int(bitrate),
                    nom_sample_point=float(nom_sample_point),
                    data_bitrate=int(data_bitrate),
                    data_sample_point=float(data_sample_point),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Invalid CAN FD timing "
                    f"(nom={bitrate}, data={data_bitrate}, f_clock={f_clock_mhz} MHz): {exc}"
                ) from exc
            kwargs["timing"] = timing
            kwargs["fd"] = True
        else:
            kwargs["bitrate"] = bitrate
        return kwargs

    def _vector_kwargs(
        self,
        *,
        channel: int,
        bitrate: int,
        fd: bool,
        data_bitrate: int,
        app_name: str,
        serial: int | None,
    ) -> dict:
        kwargs: dict = {
            "interface": "vector",
            "channel": channel,
            "app_name": app_name or "CANkit",
            "bitrate": bitrate,
            "fd": fd,
        }
        if serial is not None:
            kwargs["serial"] = serial
        if fd:
            kwargs["data_bitrate"] = data_bitrate
        return kwargs

    def close(self) -> None:
        self._stop.set()
        if self._rx_thread is not None:
            self._rx_thread.join(timeout=2.0)
            self._rx_thread = None
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("Error shutting down CAN bus")
            self._bus = None
        self._fd = False
        logger.info("Bus closed (%s)", self._backend.value)

    def add_listener(self, callback: Callable[[CanFrame], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[CanFrame], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def send(
        self,
        arbitration_id: int,
        data: bytes,
        *,
        is_extended_id: bool = False,
        fd: bool | None = None,
        bitrate_switch: bool | None = None,
    ) -> None:
        if self._bus is None:
            raise RuntimeError("CAN bus is not open")
        use_fd = self._fd if fd is None else fd
        brs = use_fd if bitrate_switch is None else bool(bitrate_switch)
        payload = bytes(data)
        if use_fd:
            if len(payload) not in CAN_FD_DLC_LENGTHS:
                payload = fit_can_payload(payload, fd=True)
        elif len(payload) > 8:
            # Preserve caller DLC (<8); only truncate illegal classic lengths.
            payload = payload[:8]

        msg = can.Message(
            arbitration_id=arbitration_id,
            data=payload,
            is_extended_id=is_extended_id,
            is_fd=use_fd,
            bitrate_switch=brs if use_fd else False,
        )
        # Bound wait — bus-off / no-ACK must not freeze the GUI thread.
        try:
            self._bus.send(msg, timeout=0.5)
        except TypeError:
            self._bus.send(msg)
        logger.debug(
            "TX id=0x%X%s fd=%s brs=%s data=%s",
            arbitration_id,
            "x" if is_extended_id else "",
            use_fd,
            brs,
            payload.hex(),
        )
        # Echo TX to Monitor listeners (bus RX only sees remote traffic).
        # Always wall-clock so live relative time stays consistent with RX.
        tx_frame = CanFrame(
            arbitration_id=arbitration_id,
            data=payload,
            is_extended_id=is_extended_id,
            is_fd=use_fd,
            bitrate_switch=brs if use_fd else False,
            timestamp=time.time(),
            is_rx=False,
            is_error=False,
        )
        self._notify_listeners(tx_frame)

    def _enable_error_frames(self) -> None:
        """Best-effort: surface CAN error frames to Monitor instead of dropping them.

        Only PEAK PCAN exposes a reliable toggle via ``PCAN_ALLOW_ERROR_FRAMES``.
        Vector XL delivers error frames according to the driver/channel config;
        there is no portable python-can switch here.
        """
        bus = self._bus
        if bus is None or self._backend != HardwareBackend.PCAN:
            return
        try:
            from can.interfaces.pcan.basic import (  # type: ignore[attr-defined]
                PCAN_ALLOW_ERROR_FRAMES,
                PCAN_PARAMETER_ON,
            )

            basic = getattr(bus, "m_objPCANBasic", None)
            handle = getattr(bus, "m_PcanHandle", None)
            if basic is not None and handle is not None:
                basic.SetValue(handle, PCAN_ALLOW_ERROR_FRAMES, PCAN_PARAMETER_ON)
                logger.debug("PCAN error frames enabled")
        except Exception:  # noqa: BLE001
            logger.debug("Could not enable PCAN error frames", exc_info=True)

    @staticmethod
    def _message_to_frame(msg: can.Message) -> CanFrame | None:
        """Convert python-can Message; never raise (error frames can be quirky)."""
        try:
            is_err = bool(getattr(msg, "is_error_frame", False))
            raw = getattr(msg, "data", b"") or b""
            try:
                data = bytes(raw)
            except Exception:  # noqa: BLE001
                data = b""
            arb = getattr(msg, "arbitration_id", 0)
            try:
                arb_i = int(arb) & 0x1FFFFFFF
            except (TypeError, ValueError):
                arb_i = 0
            return CanFrame(
                arbitration_id=arb_i,
                data=data,
                is_extended_id=bool(getattr(msg, "is_extended_id", False)),
                is_fd=bool(getattr(msg, "is_fd", False)),
                bitrate_switch=bool(getattr(msg, "bitrate_switch", False)),
                # Wall clock: HW timestamps mix poorly with TX echo time.time().
                timestamp=time.time(),
                is_rx=True,
                is_error=is_err,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Skipping undecodable CAN message")
            return None

    def _notify_listeners(self, frame: CanFrame) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(frame)
            except Exception:  # noqa: BLE001
                logger.exception("Listener error")

    def _rx_loop(self) -> None:
        assert self._bus is not None
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.05)
            except Exception:  # noqa: BLE001
                if self._stop.is_set():
                    break
                logger.exception("CAN recv error")
                time.sleep(0.05)
                continue
            if msg is None:
                continue
            frame = self._message_to_frame(msg)
            if frame is None:
                continue
            logger.debug(
                "RX id=0x%X%s err=%s fd=%s data=%s",
                frame.arbitration_id,
                "x" if frame.is_extended_id else "",
                frame.is_error,
                frame.is_fd,
                frame.data.hex(),
            )
            self._notify_listeners(frame)


# Backward-compatible alias
PcanTransport = CanBusTransport
