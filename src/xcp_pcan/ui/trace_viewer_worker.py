"""Background loader for offline trace files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from xcp_pcan.trace_log.readers import load_trace, sniff_format
from xcp_pcan.trace_log.types import TraceFrame
from xcp_pcan.ui.symbol_load_worker import SymbolLoadCancelled


class TraceLoadWorker(QThread):
    progress = Signal(int, str)  # count, note
    finished_ok = Signal(object, str)  # frames, summary
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:  # noqa: N802
        try:
            path = Path(self._path)
            fmt = sniff_format(path)

            def on_progress(count: int, note: str) -> None:
                if self.isInterruptionRequested():
                    raise SymbolLoadCancelled()
                self.progress.emit(count, note or fmt.value.upper())

            frames: list[TraceFrame] = load_trace(path, fmt=fmt, progress=on_progress)
            if self.isInterruptionRequested():
                raise SymbolLoadCancelled()
            ids = {fr.arbitration_id for fr in frames}
            t0 = frames[0].timestamp if frames else 0.0
            t1 = frames[-1].timestamp if frames else 0.0
            summary = (
                f"{path.name}: {len(frames)} frames, {len(ids)} IDs, "
                f"{fmt.value.upper()}, span {t1 - t0:.3f}s"
            )
            self.finished_ok.emit(frames, summary)
        except SymbolLoadCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
