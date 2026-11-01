"""MainWindow mixin: symbol filter, watch lists, poll/DAQ/write, charts."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from xcp_pcan.symbols.symbol_lists import (
    build_norm_index,
    load_symbol_list_dir,
    resolve_names,
)
from xcp_pcan.symbols.types import (
    decode_raw,
    encode_raw,
    physical_to_raw,
    physical_value,
)
from xcp_pcan.ui.app_log import LogCategory
from xcp_pcan.ui.chart_window import CurveChartDialog
from xcp_pcan.ui.models import WatchTableModel
from xcp_pcan.ui.trace_buffer import TraceBuffer
from xcp_pcan.ui.window_util import raise_window
from xcp_pcan.ui.write_dialog import WriteValueDialog
from xcp_pcan.xcp.poller import PollSample
from xcp_pcan.xcp.write_diag import classify_verify_mismatch, classify_xcp_error

logger = logging.getLogger(__name__)


class MeasureMixin:
    # --- Symbols / watch -------------------------------------------------------

    def _filter_symbols(self) -> None:
        q = self.symbols.filter_text()
        total = len(self._store.variables)
        # Dumping tens of thousands of rows into QTableView freezes the UI.
        max_rows = 4000
        if not q.strip() and total > max_rows:
            self._symbol_model.set_variables([])
            self.symbols.set_filter_status(
                f"{total} symbol(s) loaded — type a filter (or Load lists) to show rows"
            )
            return
        hits = self._store.search(q)
        missing = self._store.missing_names(q)
        shown = hits[:max_rows]
        self._symbol_model.set_variables(shown)
        if missing:
            preview = ", ".join(missing[:8])
            more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
            self.symbols.set_filter_status(
                f"Matched {len(hits)}; missing {len(missing)}: {preview}{more}",
                error=True,
            )
        elif len(hits) > max_rows:
            self.symbols.set_filter_status(
                f"Showing {max_rows} of {len(hits)} matches — refine the filter"
            )
        elif not q.strip():
            self.symbols.set_filter_status(f"{len(hits)} symbol(s)")
        else:
            self.symbols.set_filter_status(f"Matched {len(hits)} symbol(s)")

    def _set_list_status(self, matched: int, missing: list[str]) -> None:
        lists = len(self._symbol_groups)
        if not self._store.variables:
            self.symbols.set_filter_status(f"{lists} list(s); load an A2L or ELF to resolve addresses")
        else:
            self.symbols.set_filter_status(f"{lists} list(s); matched {matched} symbol(s)")
        if missing:
            self._append_log(
                f"Unmatched symbol names ({len(missing)}):",
                category=LogCategory.ERROR,
                level=logging.ERROR,
            )
            for name in missing:
                self._append_log(name, category=LogCategory.ERROR, level=logging.ERROR)

    @Slot()
    def _load_symbol_lists(self) -> None:
        start = str(self._settings.value("ui_symbol_list_dir", "") or "")
        folder = QFileDialog.getExistingDirectory(self, "Symbol list folder", start)
        if not folder:
            return
        groups = load_symbol_list_dir(Path(folder))
        if not groups:
            QMessageBox.information(
                self, "No lists", "That folder has no .txt files with symbol names."
            )
            return
        self._symbol_groups = groups
        self._settings.setValue("ui_symbol_list_dir", folder)
        names = sum(len(g.names) for g in groups)
        self._append_log(f"Loaded {len(groups)} symbol list(s), {names} name(s) from {folder}")
        if not self._store.variables:
            self._set_list_status(0, [n for g in groups for n in g.names])
            return
        matched, missing = self._add_symbol_groups_to_watch()
        self._set_list_status(matched, missing)

    def _symbol_tag_map(self) -> dict[str, str]:
        if not self._symbol_groups or not self._store.variables:
            return {}
        by_lower = {v.name.lower(): v for v in self._store.variables}
        by_norm = build_norm_index(by_lower)
        tags: dict[str, str] = {}
        for group in self._symbol_groups:
            found, _absent = resolve_names(group.names, by_lower, by_norm=by_norm)
            for _requested, var in found:
                tags.setdefault(var.name, group.name)
        return tags

    def _list_watch_items(self) -> tuple[list[tuple], list[str]]:
        """Resolve all txt-list symbols to (variable, tag) pairs."""
        by_lower = {v.name.lower(): v for v in self._store.variables}
        by_norm = build_norm_index(by_lower)
        items: list[tuple] = []
        missing: list[str] = []
        seen: set[str] = set()
        for group in self._symbol_groups:
            found, absent = resolve_names(group.names, by_lower, by_norm=by_norm)
            missing.extend(absent)
            for _requested, var in found:
                if var.name in seen:
                    continue
                seen.add(var.name)
                items.append((var, group.name))
        return items, missing

    def _watch_items_from_lists(
        self,
        *,
        poll_names: list[str] | None = None,
        daq_names: list[str] | None = None,
    ) -> tuple[list[tuple], list[tuple], list[str]]:
        """Build tagged poll/daq watches from lists.

        If the project already saved poll/daq names, keep only those that still
        appear in a list (so tags stay). Otherwise put every matched list
        symbol onto the Polling watch.
        """
        items, missing = self._list_watch_items()
        by_name = {var.name: (var, tag) for var, tag in items}
        poll_set = set(poll_names or ())
        daq_set = set(daq_names or ())
        if poll_set or daq_set:
            poll_items = [by_name[n] for n in (poll_names or ()) if n in by_name]
            daq_items = [by_name[n] for n in (daq_names or ()) if n in by_name]
            # Project watch was a flat dump with almost no list overlap —
            # fall back to the full tagged list on Polling.
            if not poll_items and not daq_items:
                poll_items = items
            return poll_items, daq_items, missing
        return items, [], missing

    def _add_symbol_groups_to_watch(self) -> tuple[int, list[str]]:
        """Replace the active watch with txt-list symbols, grouped by file name."""
        items, missing = self._list_watch_items()
        model = self._active_watch_model()
        # Replace — keeping old untagged rows would bury the group headers.
        model.clear()
        model.add_variables(items)
        self._sync_poller_vars()
        tab = "DAQ" if model is self._daq_watch_model else "Polling"
        self._append_log(f"Watch {tab}: {len(items)} symbol(s) in {len(self._symbol_groups)} tag(s)")
        return len(items), missing

    def _active_watch_model(self) -> WatchTableModel:
        return self._daq_watch_model if self.watch.active_is_daq() else self._poll_watch_model

    def _add_variables_to_watch(self, variables: list) -> None:
        model = self._active_watch_model()
        items: list[tuple] = []
        for entry in variables:
            if isinstance(entry, tuple):
                var, tag = entry
            else:
                var, tag = entry, ""
            if var.address == 0:
                self._append_log(
                    f"Warning: {var.name} has address 0 — check A2L ECU_ADDRESS / ELF"
                )
            items.append((var, tag))
        added = model.add_variables(items)
        self._sync_poller_vars()
        tab = "DAQ" if model is self._daq_watch_model else "Polling"
        self._append_log(f"Added {added} variable(s) to {tab} watch list")

    @Slot()
    def _add_selected_to_watch(self) -> None:
        vars_ = self.symbols.selected_variables()
        if not vars_:
            QMessageBox.information(self, "No selection", "Select rows in the symbol list.")
            return
        self._add_variables_to_watch(vars_)

    @Slot()
    def _add_all_filtered_to_watch(self) -> None:
        vars_ = self.symbols.visible_variables()
        if not vars_:
            QMessageBox.information(self, "Empty filter", "No symbols in the filtered list.")
            return
        self._add_variables_to_watch(vars_)

    def _remove_watch(self, model: WatchTableModel, table) -> None:
        picked = []
        seen: set[str] = set()
        for index in table.selectionModel().selectedRows():
            for var in model.variables_under(index):
                if var.name not in seen:
                    seen.add(var.name)
                    picked.append(var)
        model.remove_variables(picked)
        if model is self._poll_watch_model:
            self._sync_poller_vars()

    def _clear_poll_watch(self) -> None:
        if self._poller.is_running:
            self._stop_poll()
        self._poll_watch_model.clear()
        self._poll_traces.clear()
        self._sync_poller_vars()

    def _clear_daq_watch(self) -> None:
        if self._daq.is_running:
            self._stop_daq()
        self._daq_watch_model.clear()
        self._daq_traces.clear()

    def _sync_poller_vars(self) -> None:
        self._poller.set_variables(self._poll_watch_model.variables())

    def _record_sample(self, buffer: TraceBuffer, sample: PollSample) -> None:
        if sample.error or sample.physical is None:
            return
        if isinstance(sample.physical, (int, float)):
            buffer.append(
                sample.name, sample.timestamp, float(sample.physical), unit=sample.unit or ""
            )

    def _open_poll_chart(self) -> None:
        if self._poll_chart is None:
            self._poll_chart = CurveChartDialog(
                self._poll_traces, title="Polling curves", parent=self
            )
        self._poll_chart.set_monitoring(self._poller.is_running)
        self._poll_chart.sync_series_list()
        raise_window(self._poll_chart)

    def _open_daq_chart(self) -> None:
        if self._daq_chart is None:
            self._daq_chart = CurveChartDialog(
                self._daq_traces, title="DAQ curves", parent=self
            )
        self._daq_chart.set_monitoring(self._daq.is_running)
        self._daq_chart.sync_series_list()
        raise_window(self._daq_chart)

    def _export_csv(self, buffer: TraceBuffer, label: str) -> None:
        if buffer.point_count() == 0:
            QMessageBox.information(
                self,
                "No samples",
                f"No {label} samples buffered yet. Start poll/DAQ and wait for values.",
            )
            return
        start = str(self._settings.value("ui_export_dir", "") or "")
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export {label} CSV",
            start,
            "CSV (*.csv);;All (*.*)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            rows = buffer.export_csv(Path(path))
            self._settings.setValue("ui_export_dir", str(Path(path).parent))
            self._append_log(f"Exported {rows} {label} sample(s) → {path}")
            self.statusBar().showMessage(f"Exported {rows} rows", 4000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        self.statusBar().showMessage(f"Active tab: {'DAQ' if index == 1 else 'Polling'}", 2000)

    # --- Poll / DAQ / write ----------------------------------------------------

    @Slot()
    def _start_poll(self) -> None:
        if self._daq.is_running:
            QMessageBox.information(
                self, "DAQ running", "Stop DAQ before starting polling (same XCP session)."
            )
            return
        if not self._poll_watch_model.variables():
            QMessageBox.information(self, "Empty watch", "Add variables on the Polling tab.")
            return
        self._ensure_xcp_then(self._start_poll_impl)

    def _start_poll_impl(self) -> None:
        self._poller.set_interval_ms(self.watch.spn_poll.value())
        self._sync_poller_vars()
        try:
            self._poller.start()
            self._refresh_link_ui()
            if self._poll_chart is not None:
                self._poll_chart.set_monitoring(True)
            self._append_log(f"Poll started @ {self.watch.spn_poll.value()} ms")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Poll failed", str(exc))

    @Slot()
    def _stop_poll(self) -> None:
        was = self._poller.is_running
        self._poller.stop()
        self._refresh_link_ui()
        if self._poll_chart is not None:
            self._poll_chart.set_monitoring(False)
        if was:
            self._append_log("Poll stopped")

    @Slot()
    def _start_daq(self) -> None:
        if self._poller.is_running:
            QMessageBox.information(
                self, "Polling running", "Stop polling before starting DAQ (same XCP session)."
            )
            return
        if not self._daq_watch_model.variables():
            QMessageBox.information(self, "Empty watch", "Add variables on the DAQ tab.")
            return
        self._ensure_xcp_then(self._start_daq_impl)

    def _start_daq_impl(self) -> None:
        vars_ = self._daq_watch_model.variables()
        if not vars_:
            return
        self._apply_daq_timeout_from_ui()
        try:
            layout = self._daq.configure_and_start(
                vars_,
                event_channel=self.watch.spn_event.value(),
                prescaler=self.watch.spn_prescaler.value(),
                timestamp=self.watch.chk_daq_timestamp.isChecked(),
            )
            self._refresh_link_ui()
            if self._daq_chart is not None:
                self._daq_chart.set_monitoring(True)
            ts_note = (
                f", ts={layout.timestamp_size}B"
                if layout.timestamp_size
                else (", ts=off" if self.watch.chk_daq_timestamp.isChecked() else "")
            )
            res = self.connection.collect_resilience()
            self._append_log(
                f"DAQ started — {len(layout.odts)} ODT(s), "
                f"event={self.watch.spn_event.value()}, "
                f"id_type={layout.identification_field_type}{ts_note}, "
                f"dto_silence={res.daq_dto_timeout_ms}ms"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("DAQ start failed")
            QMessageBox.critical(self, "DAQ failed", str(exc))
            self._append_log(f"DAQ ERROR: {exc}")
            try:
                self._daq.stop()
            except Exception:  # noqa: BLE001
                logger.debug("DAQ stop after start failure failed", exc_info=True)
            self._refresh_link_ui()

    @Slot()
    def _stop_daq(self) -> None:
        was = self._daq.is_running
        self._daq.stop()
        self._refresh_link_ui()
        if self._daq_chart is not None:
            self._daq_chart.set_monitoring(False)
        if was:
            self._append_log("DAQ stopped")

    def _write_selected(self, model: WatchTableModel, table) -> None:
        if self._daq.is_running:
            QMessageBox.information(
                self,
                "DAQ running",
                "Stop DAQ before writing. Downloads share the same CTO path as DAQ setup.",
            )
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "No selection", "Select one variable in the watch list.")
            return
        var = model.variable_at(rows[0])
        if var is None:
            QMessageBox.information(self, "No selection", "Select one variable, not the group label.")
            return
        if not var.writable:
            kind = var.characteristic_type or var.kind.value
            QMessageBox.warning(
                self,
                "Not writable",
                f"«{var.name}» is not a scalar VALUE characteristic ({kind}).\n"
                "CURVE / MAP / VAL_BLK and MEASUREMENT rows are read-only here "
                "to avoid corrupting calibration blocks.",
            )
            return
        if var.address == 0:
            QMessageBox.warning(self, "No address", f"«{var.name}» has address 0.")
            return

        dlg = WriteValueDialog(var, parent=self)
        if dlg.exec() != WriteValueDialog.DialogCode.Accepted:
            return
        phys = dlg.physical_value()
        self._ensure_xcp_then(lambda m=model, v=var, p=phys: self._write_selected_do(m, v, p))

    def _write_selected_do(self, model: WatchTableModel, var, phys: float) -> None:
        poll_paused = False
        if self._poller.is_running and not self._poller.is_paused:
            self._poller.pause()
            poll_paused = True
            self._append_log("Poll paused for write")
        try:
            raw_num = physical_to_raw(phys, var.conversion)
            if var.datatype.value in {
                "UBYTE", "SBYTE", "UWORD", "SWORD", "ULONG", "SLONG",
                "A_UINT64", "A_INT64", "UNKNOWN",
            }:
                raw_num = int(round(raw_num))
            payload = encode_raw(raw_num, var.datatype, var.size, self._master.little_endian)
            with self._master.exclusive():
                self._master.write_memory(var.address, payload, addr_ext=var.addr_ext)
                back = self._master.read_memory(var.address, var.size, addr_ext=var.addr_ext)
            back_raw = decode_raw(back, var.datatype, self._master.little_endian)
            back_phys = physical_value(back_raw, var.conversion)
            sample = PollSample(
                name=var.name,
                address=var.address,
                raw=back,
                raw_int=back_raw if isinstance(back_raw, (int, float)) else None,
                physical=back_phys,
                unit=var.unit,
                timestamp=time.time(),
            )
            model.update_sample(sample)
            if back != payload:
                info = classify_verify_mismatch(
                    name=var.name,
                    written=payload,
                    readback=back,
                    written_phys=phys,
                    readback_phys=back_phys,
                )
                self._append_log(
                    f"WRITE VERIFY {var.name}: {info.kind.value} — {info.detail}",
                    category=LogCategory.ERROR,
                    level=logging.ERROR,
                )
                QMessageBox.warning(self, info.title, info.message())
            else:
                self._append_log(
                    f"WRITE {var.name} phys={phys} → raw={raw_num} "
                    f"bytes={payload.hex()} readback={back_phys}"
                )
                QMessageBox.information(
                    self,
                    "Write OK",
                    f"Wrote {var.name}\nPhysical: {phys}\nReadback: {back_phys}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Write failed")
            info = classify_xcp_error(exc)
            self._append_log(
                f"WRITE ERROR {var.name} [{info.kind.value}]: {exc}",
                category=LogCategory.ERROR,
                level=logging.ERROR,
            )
            QMessageBox.critical(self, info.title, info.message())
        finally:
            if poll_paused:
                self._poller.resume()
                self._append_log("Poll resumed after write")

    # --- Samples / log ---------------------------------------------------------

    @Slot(object)
    def _on_poll_sample(self, sample: object) -> None:
        if isinstance(sample, PollSample):
            self._poll_watch_model.update_sample(sample)
            self._record_sample(self._poll_traces, sample)

    @Slot(object)
    def _on_daq_sample(self, sample: object) -> None:
        if isinstance(sample, PollSample):
            self._daq_watch_model.update_sample(sample)
            self._record_sample(self._daq_traces, sample)

    @Slot(str)
    def _on_error(self, message: str) -> None:
        lower = message.lower()
        if "reconnect" in lower or "link lost" in lower or "cto" in lower or "daq rebuild" in lower:
            cat = LogCategory.XCP
            level = logging.WARNING
        elif "error" in lower or "fail" in lower:
            cat = LogCategory.ERROR
            level = logging.ERROR
        else:
            cat = LogCategory.XCP
            level = logging.WARNING
        self._append_log(message, category=cat, level=level)
        if "reconnect" in lower or "link lost" in lower or "daq rebuild" in lower:
            self.statusBar().showMessage(message, 5000)
