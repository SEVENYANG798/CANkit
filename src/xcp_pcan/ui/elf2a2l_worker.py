"""Background ELF → A2L generation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from xcp_pcan.elf2a2l.a2l_gen import fill_template
from xcp_pcan.elf2a2l.a2l_update import load_a2l, save_a2l
from xcp_pcan.elf2a2l.elf_extract import extract_variables
from xcp_pcan.ui.symbol_load_worker import SymbolLoadCancelled


class Elf2A2lWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(str)  # summary
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        elf_path: str,
        output_path: str,
        template_path: str,
        can_id_master: str,
        can_id_slave: str,
        *,
        expand_structs: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._elf = elf_path
        self._out = output_path
        self._tpl = template_path
        self._master = can_id_master
        self._slave = can_id_slave
        self._expand = expand_structs

    def run(self) -> None:  # noqa: N802
        try:
            if self.isInterruptionRequested():
                raise SymbolLoadCancelled()
            self.progress.emit("Reading ELF symbols…")
            variables, typed, sections = extract_variables(
                self._elf,
                with_types=True,
                prefer_dwarf=self._expand,
            )
            if self.isInterruptionRequested():
                raise SymbolLoadCancelled()
            self.progress.emit(f"Writing A2L ({len(variables)} variables)…")
            text = fill_template(
                load_a2l(self._tpl),
                variables,
                sections,
                elf_path=self._elf,
                can_id_master=self._master,
                can_id_slave=self._slave,
            )
            if self.isInterruptionRequested():
                raise SymbolLoadCancelled()
            save_a2l(self._out, text)
            self.finished_ok.emit(
                f"Wrote {Path(self._out).name}: {len(variables)} variables, "
                f"DWARF types {typed}, segments {len(sections)}, "
                f"TX {self._master}, RX {self._slave}"
            )
        except SymbolLoadCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
