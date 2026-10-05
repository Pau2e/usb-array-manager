from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from usb_array_manager.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("USB Array Manager")

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

