"""Entry point: ``python -m xcp_pcan``."""

from __future__ import annotations

import sys


def main() -> int:
    import logging

    from PySide6.QtWidgets import QApplication

    from xcp_pcan.ui.main_window import MainWindow

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("XCP on PCAN")
    app.setOrganizationName("XcpOnPcan")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
