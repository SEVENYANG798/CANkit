"""Background A2L address update so the Qt UI stays responsive."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from xcp_pcan.symbols.a2l_updater import update_a2l_from_elf
from xcp_pcan.ui.symbol_load_worker import SymbolLoadCancelled


class A2lUpdateWorker(QThread):
    """Run ``update_a2l_from_elf`` off the GUI thread.

    Progress signals are throttled (~5 Hz) to avoid flooding the UI event loop.
    Call ``requestInterruption()`` to abort at the next progress checkpoint.
    """

    progress = Signal(str, float)  # message, 0..1
    finished_ok = Signal(object)  # A2lUpdateReport
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        a2l_path: str | Path,
        elf_path: str | Path,
        output_path: str | Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._a2l_path = Path(a2l_path)
        self._elf_path = Path(elf_path)
        self._output_path = Path(output_path)

    def run(self) -> None:  # noqa: N802
        try:
            last_emit = 0.0
            pending: tuple[str, float] | None = None

            def on_progress(msg: str, frac: float) -> None:
                nonlocal last_emit, pending
                if self.isInterruptionRequested():
                    raise SymbolLoadCancelled()
                pending = (msg, frac)
                now = time.monotonic()
                # Always emit completion; otherwise at most ~5 Hz
                if frac >= 1.0 or (now - last_emit) >= 0.2:
                    self.progress.emit(msg, frac)
                    last_emit = now
                    pending = None
                    time.sleep(0.002)
                    if self.isInterruptionRequested():
                        raise SymbolLoadCancelled()

            report = update_a2l_from_elf(
                self._a2l_path,
                self._elf_path,
                self._output_path,
                on_progress=on_progress,
            )
            if self.isInterruptionRequested():
                raise SymbolLoadCancelled()
            if pending is not None:
                self.progress.emit(pending[0], pending[1])
            self.finished_ok.emit(report)
        except SymbolLoadCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
