"""MainWindow mixin: .xcpp project open/save and tool windows."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from xcp_pcan.project import (
    ProjectFile,
    default_project_path,
    load_project,
    save_project,
)
from xcp_pcan.symbols.symbol_lists import load_symbol_list_dir
from xcp_pcan.ui.diag_console_dialog import DiagConsoleDialog
from xcp_pcan.ui.help_dialog import HelpDialog
from xcp_pcan.ui.window_util import raise_window
from xcp_pcan.ui.xcp_viewer_dialog import XcpViewerDialog


class ProjectMixin:
    # --- Project ---------------------------------------------------------------

    def _collect_project(self) -> ProjectFile:
        return ProjectFile(
            connection=self.connection.collect_connection(),
            security=self.security.collect(),
            resilience=self.connection.collect_resilience(),
            symbol_path=self._store.source_path or "",
            polling=self.watch.collect_polling(
                [v.name for v in self._poll_watch_model.variables()]
            ),
            daq=self.watch.collect_daq([v.name for v in self._daq_watch_model.variables()]),
        )

    def _load_saved_symbol_lists(self) -> None:
        """Reuse the last Load lists folder so a restored watch keeps its tags."""
        folder = str(self._settings.value("ui_symbol_list_dir", "") or "")
        if not folder:
            return
        path = Path(folder)
        if not path.is_dir():
            return
        groups = load_symbol_list_dir(path)
        if not groups:
            return
        self._symbol_groups = groups
        self._append_log(f"Restored {len(groups)} symbol list(s) from {folder}")

    def _apply_project(self, project: ProjectFile, *, reload_symbols: bool = True) -> None:
        # Drop any in-flight load so its on_done cannot overwrite this project.
        if self._symbol_load_busy():
            self._cancel_and_wait_symbol_load()
        self._load_saved_symbol_lists()
        self.connection.apply_connection(project.connection)
        self.connection.apply_resilience(project.resilience)
        self.security.apply(project.security)
        self.watch.apply_polling(project.polling)
        self.watch.apply_daq(project.daq)
        self._apply_daq_timeout_from_ui()

        def restore_watches() -> None:
            # When txt lists are available, rebuild a tagged watch from them.
            # Restoring a flat .xcpp watch dump would hide groups under untagged rows.
            if self._symbol_groups and self._store.variables:
                poll_items, daq_items, missing = self._watch_items_from_lists(
                    poll_names=project.polling.watch,
                    daq_names=project.daq.watch,
                )
                self._poll_watch_model.clear()
                self._daq_watch_model.clear()
                if poll_items:
                    self._poll_watch_model.add_variables(poll_items)
                if daq_items:
                    self._daq_watch_model.add_variables(daq_items)
                self._sync_poller_vars()
                self._set_list_status(len(poll_items) + len(daq_items), missing)
                return
            by_name = self._store.by_name()
            poll_items = []
            daq_items = []
            for name in project.polling.watch:
                var = by_name.get(name)
                if var is not None:
                    poll_items.append((var, ""))
                else:
                    self._append_log(f"Poll watch missing symbol: {name}")
            for name in project.daq.watch:
                var = by_name.get(name)
                if var is not None:
                    daq_items.append((var, ""))
                else:
                    self._append_log(f"DAQ watch missing symbol: {name}")
            self._poll_watch_model.clear()
            self._daq_watch_model.clear()
            if poll_items:
                self._poll_watch_model.add_variables(poll_items)
            if daq_items:
                self._daq_watch_model.add_variables(daq_items)
            self._sync_poller_vars()

        if reload_symbols and project.symbol_path:
            p = Path(project.symbol_path)
            if p.is_file():
                self._start_symbol_load(
                    p, apply_transport=False, mode="replace", on_done=restore_watches
                )
                return
            self._append_log(f"Symbol file missing: {p}")
        restore_watches()

    def _remember_project_path(self, path: Path) -> None:
        self._project_path = path
        self._settings.setValue("last_project", str(path))
        self.setWindowTitle(f"CANkit — {path.name}")

    def _restore_last_project(self) -> None:
        last = self._settings.value("last_project", "")
        candidates: list[Path] = []
        if last:
            candidates.append(Path(str(last)))
        candidates.append(default_project_path())
        for path in candidates:
            if path.is_file():
                try:
                    self._apply_project(load_project(path))
                    self._remember_project_path(path)
                    self._append_log(f"Restored project: {path}")
                    return
                except Exception as exc:  # noqa: BLE001
                    self._append_log(f"Failed to restore {path}: {exc}")

    @Slot()
    def _open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", "XCP project (*.xcpp);;All (*.*)"
        )
        if not path:
            return
        try:
            self._apply_project(load_project(path))
            self._remember_project_path(Path(path))
            self._append_log(f"Opened project: {path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(exc))

    @Slot()
    def _save_project(self) -> None:
        path = self._project_path or default_project_path()
        try:
            save_project(path, self._collect_project())
            self._remember_project_path(path)
            self._append_log(f"Saved project: {path}")
            self.statusBar().showMessage(f"Saved {path.name}", 3000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))

    @Slot()
    def _save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project as", "", "XCP project (*.xcpp);;All (*.*)"
        )
        if not path:
            return
        if not path.lower().endswith(".xcpp"):
            path += ".xcpp"
        try:
            save_project(path, self._collect_project())
            self._remember_project_path(Path(path))
            self._append_log(f"Saved project: {path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))

    def _open_xcp_viewer(self) -> None:
        if self._xcp_viewer is None:
            self._xcp_viewer = XcpViewerDialog(self._mid, self._settings, self)
        raise_window(self._xcp_viewer)

    def _open_diag_console(self) -> None:
        if self._diag_console is None:
            self._diag_console = DiagConsoleDialog(
                self._settings,
                self,
                transport=self._transport,
                on_connect=lambda: self._connect(keep_front=self._diag_console),
            )
        else:
            self._diag_console.set_transport(self._transport)
            self._diag_console.set_on_connect(
                lambda: self._connect(keep_front=self._diag_console)
            )
        raise_window(self._diag_console)

    def _show_help(self) -> None:
        if self._help_dialog is None:
            self._help_dialog = HelpDialog(self)
        raise_window(self._help_dialog)

    def _show_about(self) -> None:
        from xcp_pcan.ui.update_dialog import AboutDialog

        AboutDialog(self).exec()

