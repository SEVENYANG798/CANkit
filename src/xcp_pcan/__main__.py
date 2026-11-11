"""Entry point: ``python -m xcp_pcan``."""

from __future__ import annotations

import sys


def main() -> int:
    import logging

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from xcp_pcan.resources import app_icon_path
    from xcp_pcan.ui.main_window import MainWindow

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from PySide6.QtCore import QTimer

    from xcp_pcan.ui.update_dialog import schedule_update_check

    app = QApplication(sys.argv)
    app.setApplicationName("CANkit")
    app.setOrganizationName("CANkit")
    icon_file = app_icon_path()
    if icon_file.is_file():
        icon = QIcon(str(icon_file))
        app.setWindowIcon(icon)
    else:
        icon = QIcon()
    window = MainWindow()
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.show()
    # Defer so first paint is not blocked; silent if offline / no newer release.
    QTimer.singleShot(1500, lambda: schedule_update_check(window))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
