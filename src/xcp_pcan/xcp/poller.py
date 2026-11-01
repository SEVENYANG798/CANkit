"""Periodic short-upload poller for watched variables."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from xcp_pcan.symbols.types import Variable, decode_raw, physical_value
from xcp_pcan.xcp.master import XcpMaster

logger = logging.getLogger(__name__)


@dataclass
class PollSample:
    name: str
    address: int
    raw: bytes
    raw_int: int | float | None
    physical: float | int | str | None
    unit: str
    timestamp: float
    error: str | None = None
    ecu_timestamp: int | None = None  # raw DAQ DTO timestamp ticks (if enabled)


class Poller:
    """Background thread that polls a list of variables via XCP uploads.

    On repeated CTO timeouts / link errors, attempts ``master.recover_session()``
    and continues polling instead of stopping the thread.
    """

    def __init__(
        self,
        master: XcpMaster,
        *,
        on_sample: Callable[[PollSample], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        fail_before_reconnect: int = 2,
    ) -> None:
        self._master = master
        self._on_sample = on_sample
        self._on_error = on_error
        self._fail_before_reconnect = max(1, fail_before_reconnect)
        self._vars: list[Variable] = []
        self._interval_s = 0.1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()  # set ⇒ skip CTO traffic
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def set_variables(self, variables: list[Variable]) -> None:
        with self._lock:
            self._vars = list(variables)

    def set_interval_ms(self, interval_ms: int) -> None:
        self._interval_s = max(0.01, interval_ms / 1000.0)

    def start(self) -> None:
        if self.is_running:
            return
        if not self._master.is_connected:
            raise RuntimeError("XCP is not connected")
        self._stop.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._loop, name="xcp-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._pause.clear()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def pause(self) -> None:
        """Temporarily stop issuing CTOs (thread keeps running)."""
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def _wait_if_paused(self) -> bool:
        """Block while paused. Returns False if stop was requested."""
        while self._pause.is_set():
            if self._stop.is_set():
                return False
            self._stop.wait(0.05)
        return not self._stop.is_set()

    def _emit_error_sample(self, var: Variable, message: str) -> None:
        if self._on_sample:
            self._on_sample(
                PollSample(
                    name=var.name,
                    address=var.address,
                    raw=b"",
                    raw_int=None,
                    physical=None,
                    unit=var.unit,
                    timestamp=time.time(),
                    error=message,
                )
            )
        if self._on_error:
            self._on_error(f"{var.name}: {message}")

    def _try_recover(self) -> bool:
        if not self._master.config.reconnect_enabled:
            return False
        if self._on_error:
            self._on_error("Link lost — attempting XCP reconnect…")
        try:
            self._master.recover_session()
            if self._on_error:
                self._on_error("XCP reconnect OK — resume polling")
            return True
        except Exception as exc:  # noqa: BLE001
            msg = f"XCP reconnect failed: {exc}"
            logger.warning(msg)
            if self._on_error:
                self._on_error(msg)
            return False

    def _loop(self) -> None:
        fail_streak = 0
        while not self._stop.is_set():
            if not self._wait_if_paused():
                break
            cycle_start = time.monotonic()
            with self._lock:
                variables = list(self._vars)
            for var in variables:
                if self._stop.is_set():
                    break
                if not self._wait_if_paused():
                    break
                try:
                    raw = self._master.read_memory(
                        var.address, var.size, addr_ext=var.addr_ext
                    )
                    raw_val = decode_raw(raw, var.datatype, self._master.little_endian)
                    phys = physical_value(raw_val, var.conversion)
                    sample = PollSample(
                        name=var.name,
                        address=var.address,
                        raw=raw,
                        raw_int=raw_val if isinstance(raw_val, (int, float)) else None,
                        physical=phys,
                        unit=var.unit,
                        timestamp=time.time(),
                    )
                    if self._on_sample:
                        self._on_sample(sample)
                    fail_streak = 0
                except (TimeoutError, RuntimeError, OSError) as exc:
                    fail_streak += 1
                    msg = str(exc)
                    logger.warning("Poll failed (%s): %s", fail_streak, msg)
                    self._emit_error_sample(var, msg)
                    if fail_streak >= self._fail_before_reconnect:
                        if self._try_recover():
                            fail_streak = 0
                        else:
                            # Back off before next cycle to avoid hammering the bus
                            self._stop.wait(self._master.config.reconnect_backoff_s)
                    break  # restart cycle after link error
                except Exception as exc:  # noqa: BLE001
                    msg = f"{var.name}: {exc}"
                    logger.warning("Poll failed: %s", msg)
                    self._emit_error_sample(var, str(exc))
            elapsed = time.monotonic() - cycle_start
            wait = self._interval_s - elapsed
            if wait > 0:
                self._stop.wait(wait)
