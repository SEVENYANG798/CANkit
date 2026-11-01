"""MainWindow mixin: A2L/ELF load, ELF overlay, A2L update, ELF→A2L."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from xcp_pcan.symbols.a2l_loader import unresolved_address_count
from xcp_pcan.symbols.types import SymbolStore
from xcp_pcan.ui.a2l_update_worker import A2lUpdateWorker
from xcp_pcan.ui.app_log import LogCategory
from xcp_pcan.ui.elf2a2l_dialog import Elf2A2lDialog
from xcp_pcan.ui.elf2a2l_worker import Elf2A2lWorker
from xcp_pcan.ui.symbol_load_worker import SymbolLoadWorker
from xcp_pcan.ui.worker_util import (
    abort_qthread,
    close_progress,
    make_progress_dialog,
    mark_progress_cancelling,
)

logger = logging.getLogger(__name__)


class SymbolToolsMixin:
    # --- Symbol load -----------------------------------------------------------

    def _symbol_load_busy(self) -> bool:
        return self._load_worker is not None and self._load_worker.isRunning()

    def _cancel_and_wait_symbol_load(self, *, timeout_ms: int = 8000) -> None:
        """Abort in-flight symbol load so a new project/load can start cleanly."""
        if not self._symbol_load_busy():
            return
        self._load_cancelled = True
        self._load_on_done = None
        worker = self._load_worker
        if worker is not None:
            abort_qthread(worker, timeout_ms=timeout_ms)
        close_progress(self._load_progress)
        self._load_progress = None
        self._load_worker = None
        self._act_load_symbols.setEnabled(True)
        self._act_resolve_elf.setEnabled(True)

    def _start_symbol_load(
        self,
        path: str | Path,
        *,
        apply_transport: bool = True,
        mode: str = "replace",
        on_done=None,
    ) -> None:
        if self._symbol_load_busy():
            QMessageBox.information(self, "Busy", "A symbol file is already loading — please wait.")
            return
        self._load_cancelled = False
        self._load_apply_transport = apply_transport
        self._load_on_done = on_done
        # Non-modal: allow editing connection / IDs while a large A2L loads.
        progress = make_progress_dialog(
            self,
            title="Symbol load",
            label="Loading symbols…",
            cancel_text="Cancel",
            maximum=100,
            tool_window=True,
        )
        progress.canceled.connect(self._cancel_symbol_load)
        self._load_progress = progress
        self._act_load_symbols.setEnabled(False)
        self._act_resolve_elf.setEnabled(False)
        worker = SymbolLoadWorker(
            path,
            mode=mode,
            base_store=self._store if mode == "overlay_elf" else None,
            parent=self,
        )
        worker.progress.connect(self._on_symbol_load_progress)
        worker.finished_ok.connect(self._on_symbol_load_ok)
        worker.failed.connect(self._on_symbol_load_failed)
        worker.cancelled.connect(self._on_symbol_load_cancelled)
        worker.finished.connect(self._on_symbol_load_thread_finished)
        self._load_worker = worker
        self.statusBar().showMessage(f"Loading {Path(path).name}…")
        worker.start()

    @Slot()
    def _cancel_symbol_load(self) -> None:
        if self._load_cancelled:
            return
        self._load_cancelled = True
        self._load_on_done = None
        if self._load_worker is not None:
            self._load_worker.requestInterruption()
        # Do not call setValue after cancel — Qt clears wasCanceled that way.
        mark_progress_cancelling(self._load_progress)
        self._append_log("Symbol load cancel requested")
        self.statusBar().showMessage("Cancelling symbol load…")

    @Slot(str, float)
    def _on_symbol_load_progress(self, message: str, fraction: float) -> None:
        # Qt: setValue() after Cancel clears the canceled state — skip updates.
        if self._load_cancelled or self._load_progress is None:
            return
        self._load_progress.setLabelText(message)
        self._load_progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    @Slot()
    def _on_symbol_load_cancelled(self) -> None:
        if self._closing:
            return
        self._load_cancelled = True
        self._load_on_done = None
        self._append_log("Symbol load cancelled")
        self.statusBar().showMessage("Symbol load cancelled", 4000)

    @Slot(object, str)
    def _on_symbol_load_ok(self, store: object, summary: str) -> None:
        if self._closing:
            return
        if self._load_cancelled:
            self._load_on_done = None
            self._append_log("Symbol load cancelled — result discarded")
            return
        if not isinstance(store, SymbolStore):
            return
        # Close the progress dialog before heavy UI work so Windows does not
        # mark the app as hung while we still show "Done".
        close_progress(self._load_progress)
        self._load_progress = None
        self._store = store
        load_mode = self._load_worker._mode if self._load_worker is not None else "replace"
        restoring = callable(self._load_on_done)
        on_done = self._load_on_done
        self._load_on_done = None
        apply_transport = self._load_apply_transport
        # Defer so the event loop can paint a responsive frame first.
        QTimer.singleShot(
            0,
            lambda: self._finish_symbol_load(
                summary,
                load_mode=load_mode,
                restoring=restoring,
                on_done=on_done,
                apply_transport=apply_transport,
            ),
        )

    def _finish_symbol_load(
        self,
        summary: str,
        *,
        load_mode: str,
        restoring: bool,
        on_done,
        apply_transport: bool,
    ) -> None:
        self._filter_symbols()
        # Project restore fills the watch itself and only needs the tags.
        if self._symbol_groups and load_mode == "replace" and not restoring:
            matched, missing = self._add_symbol_groups_to_watch()
            self._set_list_status(matched, missing)
        self._append_log(summary)
        self.statusBar().showMessage(
            f"Symbols: {Path(self._store.source_path).name} ({len(self._store.variables)})",
            5000,
        )
        if apply_transport and self._store.transport is not None:
            self._apply_transport_hints(self._store.transport)
        if callable(on_done):
            on_done()
            return
        missing = unresolved_address_count(self._store)
        if (
            missing > 0
            and load_mode == "replace"
            and Path(self._store.source_path).suffix.lower() == ".a2l"
        ):
            ans = QMessageBox.question(
                self,
                "Addresses missing",
                f"{missing} symbol(s) have address 0.\n\nResolve from an ELF now?",
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._resolve_from_elf()

    @Slot(str)
    def _on_symbol_load_failed(self, message: str) -> None:
        if self._closing or self._load_cancelled:
            return
        logger.error("Symbol load failed: %s", message)
        self._append_log(f"ERROR load symbols: {message}")
        QMessageBox.critical(self, "Load failed", message)

    @Slot()
    def _on_symbol_load_thread_finished(self) -> None:
        close_progress(self._load_progress)
        self._load_progress = None
        self._load_worker = None
        self._act_load_symbols.setEnabled(True)
        self._act_resolve_elf.setEnabled(True)

    @Slot()
    def _load_symbols(self) -> None:
        start = str(Path(self._store.source_path).parent) if self._store.source_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load symbols",
            start,
            "Symbol files (*.a2l *.A2L *.elf *.ELF *.out);;A2L (*.a2l);;ELF (*.elf *.out);;All (*.*)",
        )
        if path:
            self._start_symbol_load(path, apply_transport=True, mode="replace")

    @Slot()
    def _resolve_from_elf(self) -> None:
        if not self._store.variables:
            QMessageBox.information(self, "No A2L symbols", "Load an A2L first.")
            return
        start = str(Path(self._store.source_path).parent) if self._store.source_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "ELF for address resolution", start, "ELF (*.elf *.out *.axf);;All (*.*)"
        )
        if not path:
            return
        self._start_symbol_load(
            path,
            apply_transport=False,
            mode="overlay_elf",
            on_done=lambda: (self._filter_symbols(), self._sync_poller_vars()),
        )

    @Slot()
    def _elf_to_a2l(self) -> None:
        if self._elf2a2l_worker is not None and self._elf2a2l_worker.isRunning():
            QMessageBox.information(self, "Busy", "Create A2L from ELF is already running.")
            return
        dlg = Elf2A2lDialog(
            self._settings,
            master_id=self.connection.edt_tx.text().strip(),
            slave_id=self.connection.edt_rx.text().strip(),
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        vals = dlg.values()
        progress = make_progress_dialog(
            self,
            title="Create A2L from ELF",
            label="Reading ELF…",
            cancel_text=None,
            maximum=0,
        )
        self._elf2a2l_progress = progress
        self._act_elf2a2l.setEnabled(False)
        worker = Elf2A2lWorker(
            vals["elf"],
            vals["output"],
            vals["template"],
            vals["master"],
            vals["slave"],
            expand_structs=vals["expand_structs"],
            parent=self,
        )
        worker.progress.connect(self._on_elf2a2l_progress)
        worker.finished_ok.connect(self._on_elf2a2l_ok)
        worker.failed.connect(self._on_elf2a2l_failed)
        worker.finished.connect(self._on_elf2a2l_finished)
        self._elf2a2l_worker = worker
        self._elf2a2l_output = vals["output"]
        worker.start()

    @Slot(str)
    def _on_elf2a2l_progress(self, message: str) -> None:
        if self._elf2a2l_progress is not None:
            self._elf2a2l_progress.setLabelText(message)

    @Slot(str)
    def _on_elf2a2l_ok(self, summary: str) -> None:
        if self._closing:
            return
        self._append_log(summary, category=LogCategory.APP)
        out = getattr(self, "_elf2a2l_output", "")
        ans = QMessageBox.question(
            self,
            "Create A2L from ELF",
            f"{summary}\n\nLoad this A2L now?",
        )
        if ans == QMessageBox.StandardButton.Yes and out:
            self._start_symbol_load(out, apply_transport=True, mode="replace")

    @Slot(str)
    def _on_elf2a2l_failed(self, message: str) -> None:
        if self._closing:
            return
        logger.error("Create A2L from ELF failed: %s", message)
        self._append_log(f"ERROR Create A2L from ELF: {message}")
        QMessageBox.critical(self, "Create A2L from ELF failed", message)

    @Slot()
    def _on_elf2a2l_finished(self) -> None:
        close_progress(self._elf2a2l_progress)
        self._elf2a2l_progress = None
        self._elf2a2l_worker = None
        self._act_elf2a2l.setEnabled(True)

    def _a2l_update_busy(self) -> bool:
        return self._a2l_update_worker is not None and self._a2l_update_worker.isRunning()

    @Slot()
    def _update_a2l_from_elf(self) -> None:
        """Rewrite A2L ECU_ADDRESS from ELF (file output), ASAP2 Updater style."""
        if self._a2l_update_busy():
            QMessageBox.information(
                self, "Busy", "An A2L update is already running — check the progress window."
            )
            return

        start = ""
        if self._store.source_path and Path(self._store.source_path).suffix.lower() == ".a2l":
            start = self._store.source_path
        a2l_path, _ = QFileDialog.getOpenFileName(
            self,
            "A2L to update",
            start,
            "A2L (*.a2l *.A2L);;All (*.*)",
        )
        if not a2l_path:
            return
        elf_start = str(Path(a2l_path).parent)
        elf_path, _ = QFileDialog.getOpenFileName(
            self,
            "ELF with symbol addresses",
            elf_start,
            "ELF (*.elf *.out *.axf);;All (*.*)",
        )
        if not elf_path:
            return
        default_out = str(Path(a2l_path).with_name(Path(a2l_path).stem + "_updated.a2l"))
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save updated A2L",
            default_out,
            "A2L (*.a2l);;All (*.*)",
        )
        if not out_path:
            return

        self._a2l_update_cancelled = False
        # Non-modal: main window stays fully interactive while update runs.
        progress = make_progress_dialog(
            self,
            title="Update A2L from ELF",
            label="Updating A2L…",
            cancel_text="Cancel",
            maximum=100,
            tool_window=True,
        )
        progress.canceled.connect(self._cancel_a2l_update)
        self._a2l_update_progress = progress
        self._act_update_a2l.setEnabled(False)

        worker = A2lUpdateWorker(a2l_path, elf_path, out_path, parent=self)
        worker.progress.connect(self._on_a2l_update_progress)
        worker.finished_ok.connect(self._on_a2l_update_ok)
        worker.failed.connect(self._on_a2l_update_failed)
        worker.cancelled.connect(self._on_a2l_update_cancelled)
        worker.finished.connect(self._on_a2l_update_thread_finished)
        self._a2l_update_worker = worker
        self.statusBar().showMessage(f"Updating A2L from {Path(elf_path).name}…")
        worker.start()

    @Slot()
    def _cancel_a2l_update(self) -> None:
        if self._a2l_update_cancelled:
            return
        self._a2l_update_cancelled = True
        if self._a2l_update_worker is not None:
            self._a2l_update_worker.requestInterruption()
        mark_progress_cancelling(self._a2l_update_progress)
        self._append_log("A2L update cancel requested")
        self.statusBar().showMessage("Cancelling A2L update…")

    @Slot(str, float)
    def _on_a2l_update_progress(self, message: str, fraction: float) -> None:
        if self._a2l_update_cancelled or self._a2l_update_progress is None:
            return
        self._a2l_update_progress.setLabelText(message)
        self._a2l_update_progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    @Slot()
    def _on_a2l_update_cancelled(self) -> None:
        if self._closing:
            return
        self._a2l_update_cancelled = True
        self._append_log("A2L update cancelled")
        self.statusBar().showMessage("A2L update cancelled", 4000)

    @Slot(object)
    def _on_a2l_update_ok(self, report: object) -> None:
        from xcp_pcan.symbols.a2l_updater import A2lUpdateReport

        if self._closing:
            return
        if self._a2l_update_cancelled:
            self._append_log("A2L update cancelled — result discarded")
            return
        if not isinstance(report, A2lUpdateReport):
            return
        out_path = report.output_path
        self._append_log(
            f"A2L updated → {out_path}: {report.summary()}",
            category=LogCategory.APP,
        )
        missing_preview = ""
        if report.missing:
            sample = ", ".join(report.missing[:6])
            more = f" (+{len(report.missing) - 6} more)" if len(report.missing) > 6 else ""
            missing_preview = f"\n\nNo ELF match:\n{sample}{more}"
        ans = QMessageBox.question(
            self,
            "A2L updated",
            f"{report.summary()}\n\nSaved to:\n{out_path}{missing_preview}"
            f"\n\nLoad the updated A2L now?",
        )
        if ans == QMessageBox.StandardButton.Yes and out_path:
            self._start_symbol_load(out_path, apply_transport=True, mode="replace")

    @Slot(str)
    def _on_a2l_update_failed(self, message: str) -> None:
        if self._closing or self._a2l_update_cancelled:
            return
        logger.error("A2L update failed: %s", message)
        self._append_log(f"ERROR update A2L: {message}")
        QMessageBox.critical(self, "Update A2L failed", message)

    @Slot()
    def _on_a2l_update_thread_finished(self) -> None:
        close_progress(self._a2l_update_progress)
        self._a2l_update_progress = None
        self._a2l_update_worker = None
        self._act_update_a2l.setEnabled(True)

