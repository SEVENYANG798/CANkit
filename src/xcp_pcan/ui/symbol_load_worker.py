"""Background A2L/ELF loading so the Qt UI stays responsive."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from xcp_pcan.symbols.a2l_loader import load_a2l, overlay_elf_addresses, unresolved_address_count
from xcp_pcan.symbols.elf_loader import load_elf
from xcp_pcan.symbols.types import SymbolStore
from xcp_pcan.util.cancel import OperationCancelled

# Back-compat alias for other workers/UI.
SymbolLoadCancelled = OperationCancelled


class SymbolLoadWorker(QThread):
    """Load symbols off the GUI thread.

    Modes:
    - ``replace``: load A2L or ELF into a new store
    - ``overlay_elf``: load ELF and fill zero addresses on ``base_store``

    Progress signals are throttled (~5 Hz) to avoid flooding the UI event loop.
    Call ``requestInterruption()`` to abort; the loader checks at progress points.
    """

    progress = Signal(str, float)  # message, 0..1
    finished_ok = Signal(object, str)  # SymbolStore, summary
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        path: str | Path,
        *,
        mode: str = "replace",
        base_store: SymbolStore | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._mode = mode
        self._base_store = base_store

    def run(self) -> None:  # noqa: N802
        try:
            path = self._path
            suffix = path.suffix.lower()
            last_emit = 0.0
            pending: tuple[str, float] | None = None

            def on_progress(msg: str, frac: float) -> None:
                nonlocal last_emit, pending
                if self.isInterruptionRequested():
                    raise SymbolLoadCancelled()
                pending = (msg, frac)
                now = time.monotonic()
                if frac >= 1.0 or (now - last_emit) >= 0.2:
                    self.progress.emit(msg, frac)
                    last_emit = now
                    pending = None
                    # Let the GUI thread run while we hold the interpreter briefly.
                    time.sleep(0.002)
                    if self.isInterruptionRequested():
                        raise SymbolLoadCancelled()

            if self._mode == "overlay_elf":
                if self._base_store is None:
                    raise RuntimeError("overlay_elf requires base_store")
                elf = load_elf(path, on_progress=on_progress)
                if self.isInterruptionRequested():
                    raise SymbolLoadCancelled()
                # Copy so the GUI thread can keep reading self._store safely.
                merged = SymbolStore(
                    variables=[replace(v) for v in self._base_store.variables],
                    source_path=self._base_store.source_path,
                    transport=self._base_store.transport,
                )
                n = overlay_elf_addresses(merged, elf)
                missing = unresolved_address_count(merged)
                summary = (
                    f"ELF overlay: resolved {n} address(es) from {path.name}; "
                    f"{missing} still at 0"
                )
                if pending is not None:
                    self.progress.emit(pending[0], pending[1])
                self.finished_ok.emit(merged, summary)
                return

            if suffix in {".a2l"} or self._mode == "a2l":
                store = load_a2l(path, on_progress=on_progress)
            elif suffix in {".elf", ".out", ".axf"} or self._mode == "elf":
                store = load_elf(path, on_progress=on_progress)
            else:
                try:
                    store = load_a2l(path, on_progress=on_progress)
                except SymbolLoadCancelled:
                    raise
                except Exception:
                    if self.isInterruptionRequested():
                        raise SymbolLoadCancelled()
                    store = load_elf(path, on_progress=on_progress)

            if self.isInterruptionRequested():
                raise SymbolLoadCancelled()

            missing = unresolved_address_count(store)
            summary = f"Loaded {len(store.variables)} symbols from {path.name}"
            if missing:
                summary += f" ({missing} without address — load ELF to resolve SYMBOL_LINK)"
            if pending is not None:
                self.progress.emit(pending[0], pending[1])
            self.finished_ok.emit(store, summary)
        except SymbolLoadCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
