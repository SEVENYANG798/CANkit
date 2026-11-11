"""Prompt when a newer CANkit.exe is available on GitHub Releases."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan import __version__
from xcp_pcan.update_check import (
    UpdateInfo,
    check_for_update,
    current_exe_path,
    download_update,
    is_frozen,
    write_apply_script,
)

logger = logging.getLogger(__name__)


class _CheckWorker(QObject):
    finished = Signal(object)  # UpdateInfo | None
    failed = Signal(str)

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(check_for_update())
        except Exception as exc:  # noqa: BLE001 — surface to UI log
            self.failed.emit(str(exc))


class _DownloadWorker(QObject):
    finished = Signal(object)  # Path
    failed = Signal(str)

    def __init__(self, info: UpdateInfo, dest: Path) -> None:
        super().__init__()
        self._info = info
        self._dest = dest

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(download_update(self._info, self._dest))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UpdateAvailableDialog(QDialog):
    def __init__(self, info: UpdateInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CANkit update")
        self.resize(480, 320)
        self._info = info
        self._thread: QThread | None = None
        self._worker: QObject | None = None

        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(f"New version {info.version} is available (you have {__version__}).")
        )
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(info.notes or "(no release notes)")
        root.addWidget(notes, stretch=1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#616161;")
        root.addWidget(self._status)

        row = QHBoxLayout()
        row.addStretch(1)
        self._btn_later = QPushButton("Later")
        self._btn_later.clicked.connect(self.reject)
        self._btn_update = QPushButton("Download & restart")
        self._btn_update.setDefault(True)
        self._btn_update.clicked.connect(self._start_download)
        row.addWidget(self._btn_later)
        row.addWidget(self._btn_update)
        root.addLayout(row)

        if not is_frozen():
            self._btn_update.setEnabled(False)
            self._status.setText("Update install only works from the packaged CANkit.exe.")

    def _start_download(self) -> None:
        exe = current_exe_path()
        if exe is None:
            QMessageBox.information(
                self, "CANkit update", "Run the packaged CANkit.exe to apply updates."
            )
            return
        dest = exe.with_suffix(exe.suffix + ".new")
        self._btn_update.setEnabled(False)
        self._btn_later.setEnabled(False)
        self._progress.setVisible(True)
        self._status.setText("Downloading…")

        thread = QThread(self)
        worker = _DownloadWorker(self._info, dest)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_downloaded)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(object)
    def _on_downloaded(self, path: object) -> None:
        assert isinstance(path, Path)
        exe = current_exe_path()
        if exe is None:
            self.reject()
            return
        script = write_apply_script(current_exe=exe, new_exe=path)
        self._status.setText("Restarting to apply update…")
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", str(script)],
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            QMessageBox.warning(self, "CANkit update", f"Could not start updater:\n{exc}")
            self._btn_update.setEnabled(True)
            self._btn_later.setEnabled(True)
            self._progress.setVisible(False)
            return
        # Quit the app so the script can replace the exe.
        self.accept()
        from PySide6.QtWidgets import QApplication

        qapp = QApplication.instance()
        if qapp is not None:
            qapp.quit()

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        self._progress.setVisible(False)
        self._btn_update.setEnabled(True)
        self._btn_later.setEnabled(True)
        self._status.setText("")
        QMessageBox.warning(self, "CANkit update", f"Download failed:\n{message}")


class _UpdateCheckBridge(QObject):
    """Receives worker results on the GUI thread (slots → queued across threads)."""

    def __init__(self, ui: QWidget) -> None:
        super().__init__(ui)
        self._ui = ui

    @Slot(object)
    def on_finished(self, info: object) -> None:
        if not isinstance(info, UpdateInfo):
            return
        # Defer past the signal stack / startup Connect dialog settle.
        QTimer.singleShot(0, self._ui, lambda: self._show(info))

    def _show(self, info: UpdateInfo) -> None:
        ui = self._ui
        if ui is None:
            return
        UpdateAvailableDialog(info, parent=ui).exec()

    @Slot(str)
    def on_failed(self, message: str) -> None:
        logger.info("Update check failed: %s", message)


def schedule_update_check(parent: QWidget) -> None:
    """Non-blocking startup check (only prompts when a newer release exists)."""
    thread = QThread(parent)
    worker = _CheckWorker()
    worker.moveToThread(thread)
    bridge = _UpdateCheckBridge(parent)

    thread.started.connect(worker.run)
    # Bridge lives on the GUI thread — never open QDialog from the worker thread
    # (0.1.5 did that via a plain lambda and the process flash-exited).
    worker.finished.connect(bridge.on_finished)
    worker.failed.connect(bridge.on_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    parent._update_check_thread = thread  # type: ignore[attr-defined]
    parent._update_check_worker = worker  # type: ignore[attr-defined]
    parent._update_check_bridge = bridge  # type: ignore[attr-defined]
    thread.start()


class AboutDialog(QDialog):
    """About box: app name, real version; opens download dialog when an update exists."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About CANkit")
        self.setMinimumWidth(360)
        self._update_info: UpdateInfo | None = None
        self._thread: QThread | None = None
        self._worker: QObject | None = None

        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"app name：CANkit\nversion number:{__version__}"))
        self._status = QLabel("Checking for updates…")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        row = QHBoxLayout()
        row.addStretch(1)
        self._btn_ok = QPushButton("OK")
        self._btn_ok.setDefault(True)
        self._btn_ok.clicked.connect(self.accept)
        row.addWidget(self._btn_ok)
        root.addLayout(row)

        self._start_check()

    def _start_check(self) -> None:
        thread = QThread(self)
        worker = _CheckWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_check_done)
        worker.failed.connect(self._on_check_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_check_thread)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def _clear_check_thread(self) -> None:
        self._thread = None
        self._worker = None

    @Slot(object)
    def _on_check_done(self, info: object) -> None:
        if isinstance(info, UpdateInfo):
            self._update_info = info
            self._status.setText(f"Update available: {info.version}.")
            QTimer.singleShot(0, self._open_update)
            return
        self._status.setText(
            "No updates detected. You are already on the latest version."
        )

    @Slot(str)
    def _on_check_failed(self, message: str) -> None:
        self._status.setText(f"Could not check for updates:\n{message}")

    def _open_update(self) -> None:
        info = self._update_info
        if info is None:
            return
        # Prefer main window as parent so nested About→Update stays on GUI owner.
        host = self.parentWidget() or self
        UpdateAvailableDialog(info, parent=host).exec()

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        thread = self._thread
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(3000)
            except RuntimeError:
                pass
            self._thread = None
            self._worker = None
        super().closeEvent(event)
