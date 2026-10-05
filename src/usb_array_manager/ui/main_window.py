from __future__ import annotations

from PySide6.QtCore import QDateTime, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.windows_cim import (
    DeviceDetectionError,
    detect_usb_storage_devices,
)
from usb_array_manager.ui.device_table_model import DeviceTableModel


class DeviceScanThread(QThread):
    scan_succeeded = Signal(object)
    scan_failed = Signal(str)

    def run(self) -> None:
        try:
            devices = detect_usb_storage_devices()
        except DeviceDetectionError as error:
            self.scan_failed.emit(str(error))
        except Exception as error:
            self.scan_failed.emit(f"Unexpected detection error: {error}")
        else:
            self.scan_succeeded.emit(devices)


class MainWindow(QMainWindow):
    REFRESH_INTERVAL_MS = 5_000

    def __init__(self) -> None:
        super().__init__()
        self._scan_thread: DeviceScanThread | None = None
        self._closing = False

        self.setWindowTitle("USB Array Manager — Read-only device inventory")
        self.resize(1_180, 430)

        self._model = DeviceTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setStretchLastSection(True)

        title = QLabel("Connected USB storage devices")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")

        safety_note = QLabel("Inventory only — this application does not modify drives.")
        safety_note.setStyleSheet("color: #555;")

        self._status = QLabel("Waiting for the first scan…")
        self._refresh_button = QPushButton("Refresh now")
        self._refresh_button.clicked.connect(self.refresh_devices)

        status_row = QHBoxLayout()
        status_row.addWidget(self._status, 1)
        status_row.addWidget(self._refresh_button)

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(safety_note)
        layout.addWidget(self._table, 1)
        layout.addLayout(status_row)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh_devices)
        self._timer.start()
        QTimer.singleShot(0, self.refresh_devices)

    def refresh_devices(self) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return

        self._status.setStyleSheet("")
        self._status.setText("Scanning Windows device inventory…")
        self._refresh_button.setEnabled(False)

        self._scan_thread = DeviceScanThread(self)
        self._scan_thread.scan_succeeded.connect(self._show_devices)
        self._scan_thread.scan_failed.connect(self._show_error)
        self._scan_thread.finished.connect(self._scan_finished)
        self._scan_thread.start()

    def _show_devices(self, devices: list[StorageDevice]) -> None:
        self._model.set_devices(devices)
        updated_at = QDateTime.currentDateTime().toString("HH:mm:ss")
        self._status.setText(
            f"{len(devices)} connected USB storage device(s) — updated {updated_at}"
        )

    def _show_error(self, message: str) -> None:
        self._status.setStyleSheet("color: #b00020;")
        self._status.setText("Device scan failed. Hover here for details.")
        self._status.setToolTip(message)

    def _scan_finished(self) -> None:
        thread = self._scan_thread
        self._scan_thread = None
        self._refresh_button.setEnabled(True)
        if thread is not None:
            thread.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._closing = True
            self._timer.stop()
            self._status.setText("Waiting for the current read-only scan to finish…")
            event.ignore()
            return
        event.accept()

