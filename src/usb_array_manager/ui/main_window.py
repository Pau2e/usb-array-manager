from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QDateTime, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.services.benchmark import (
    BenchmarkCancelled,
    BenchmarkError,
    BenchmarkSafetyError,
    BenchmarkSettings,
    check_benchmark_target,
    run_benchmark,
)
from usb_array_manager.services.benchmark_store import (
    BenchmarkStore,
    BenchmarkStoreError,
)
from usb_array_manager.services.device_monitor import WindowsDeviceEventFilter
from usb_array_manager.services.raid10_plan_store import (
    Raid10PlanStore,
    Raid10PlanStoreError,
)
from usb_array_manager.services.slot_store import (
    SLOT_COUNT,
    SlotConflictError,
    SlotStore,
    SlotStoreError,
)
from usb_array_manager.services.windows_cim import (
    DeviceDetectionError,
    detect_usb_storage_devices,
)
from usb_array_manager.ui.device_table_model import DeviceTableModel
from usb_array_manager.ui.raid10_planner_dialog import Raid10PlannerDialog


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


class BenchmarkThread(QThread):
    progress_changed = Signal(int, str)
    benchmark_succeeded = Signal(object)
    benchmark_failed = Signal(str)
    benchmark_cancelled = Signal()

    def __init__(
        self,
        root: Path,
        slot: int,
        drive_letter: str,
        settings: BenchmarkSettings,
        include_random_4k: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._root = root
        self._slot = slot
        self._drive_letter = drive_letter
        self._settings = settings
        self._include_random_4k = include_random_4k
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = run_benchmark(
                self._root,
                self._slot,
                self._drive_letter,
                settings=self._settings,
                include_random_4k=self._include_random_4k,
                progress=self.progress_changed.emit,
                cancel_event=self._cancel_event,
            )
        except BenchmarkCancelled:
            self.benchmark_cancelled.emit()
        except (BenchmarkError, OSError) as error:
            self.benchmark_failed.emit(str(error))
        except Exception as error:
            self.benchmark_failed.emit(f"Unexpected benchmark error: {error}")
        else:
            self.benchmark_succeeded.emit(result)


class MainWindow(QMainWindow):
    DEVICE_CHANGE_DEBOUNCE_MS = 500
    RECONCILIATION_INTERVAL_MS = 30_000

    def __init__(self) -> None:
        super().__init__()
        self._scan_thread: DeviceScanThread | None = None
        self._benchmark_thread: BenchmarkThread | None = None
        self._closing = False
        self._rescan_requested = False
        self._native_filter_installed = False
        self._connected_devices: list[StorageDevice] = []
        self._slot_store = SlotStore()
        self._benchmark_store = BenchmarkStore()
        self._raid10_plan_store = Raid10PlanStore()
        self._saved_raid10_plan: SavedRaid10Plan | None = None
        self._benchmark_settings = BenchmarkSettings()
        self._benchmark_results: dict[int, BenchmarkResult] = {}
        self._benchmark_buttons: list[QPushButton] = []

        self.setWindowTitle("USB Array Manager — Inventory, benchmark, and RAID10 planner")
        self.resize(1_560, 500)

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

        safety_note = QLabel(
            "Inventory is read-only. Benchmarks write only a temporary file after confirmation."
        )
        safety_note.setStyleSheet("color: #555;")

        self._status = QLabel("Waiting for the first scan…")
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        self._cancel_benchmark_button = QPushButton("Cancel benchmark")
        self._cancel_benchmark_button.setVisible(False)
        self._cancel_benchmark_button.clicked.connect(self._cancel_benchmark)
        self._refresh_button = QPushButton("Refresh now")
        self._refresh_button.clicked.connect(self.refresh_devices)
        self._assign_slot_button = QPushButton("Assign/change slot…")
        self._assign_slot_button.setEnabled(False)
        self._assign_slot_button.clicked.connect(self._assign_selected_slot)
        self._raid10_button = QPushButton("Plan RAID10…")
        self._raid10_button.clicked.connect(self._open_raid10_planner)
        self._table.selectionModel().selectionChanged.connect(
            self._update_slot_button
        )

        status_row = QHBoxLayout()
        status_row.addWidget(self._status, 1)
        status_row.addWidget(self._progress)
        status_row.addWidget(self._cancel_benchmark_button)
        status_row.addWidget(self._raid10_button)
        status_row.addWidget(self._assign_slot_button)
        status_row.addWidget(self._refresh_button)

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(safety_note)
        layout.addWidget(self._table, 1)
        layout.addLayout(status_row)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        try:
            self._benchmark_results = self._benchmark_store.load_all()
        except BenchmarkStoreError as error:
            self._status.setStyleSheet("color: #b00020;")
            self._status.setText("Saved benchmark results could not be loaded.")
            self._status.setToolTip(str(error))
        self._model.set_benchmark_results(self._benchmark_results)

        try:
            self._saved_raid10_plan = self._raid10_plan_store.load()
        except Raid10PlanStoreError as error:
            self._status.setStyleSheet("color: #b00020;")
            self._status.setText("Saved RAID10 plan could not be loaded.")
            self._status.setToolTip(str(error))

        self._device_change_timer = QTimer(self)
        self._device_change_timer.setSingleShot(True)
        self._device_change_timer.setInterval(self.DEVICE_CHANGE_DEBOUNCE_MS)
        self._device_change_timer.timeout.connect(self.refresh_devices)

        self._reconciliation_timer = QTimer(self)
        self._reconciliation_timer.setInterval(self.RECONCILIATION_INTERVAL_MS)
        self._reconciliation_timer.timeout.connect(self.refresh_devices)
        self._reconciliation_timer.start()

        self._device_event_filter = WindowsDeviceEventFilter(
            self._device_change_detected
        )
        application = QApplication.instance()
        if application is not None:
            application.installNativeEventFilter(self._device_event_filter)
            self._native_filter_installed = True

        QTimer.singleShot(0, self.refresh_devices)

    def refresh_devices(self) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._rescan_requested = True
            return

        self._rescan_requested = False
        self._status.setStyleSheet("")
        self._status.setText("Scanning Windows device inventory…")
        self._refresh_button.setEnabled(False)

        self._scan_thread = DeviceScanThread(self)
        self._scan_thread.scan_succeeded.connect(self._show_devices)
        self._scan_thread.scan_failed.connect(self._show_error)
        self._scan_thread.finished.connect(self._scan_finished)
        self._scan_thread.start()

    def _show_devices(self, devices: list[StorageDevice]) -> None:
        self._connected_devices = devices
        try:
            displayed_devices = self._slot_store.reconcile(devices)
        except SlotStoreError as error:
            self._model.set_devices(devices)
            self._status.setStyleSheet("color: #b00020;")
            self._status.setText("Slot configuration could not be loaded.")
            self._status.setToolTip(str(error))
            return

        self._model.set_devices(displayed_devices)
        self._install_benchmark_buttons()
        updated_at = QDateTime.currentDateTime().toString("HH:mm:ss")
        disconnected_count = sum(
            not device.is_connected for device in displayed_devices
        )
        disconnected_text = (
            f", {disconnected_count} reserved slot(s) disconnected"
            if disconnected_count
            else ""
        )
        self._status.setText(
            f"{len(devices)} connected USB storage device(s){disconnected_text} "
            f"— updated {updated_at}"
        )

    def _show_error(self, message: str) -> None:
        self._status.setStyleSheet("color: #b00020;")
        self._status.setText("Device scan failed. Hover here for details.")
        self._status.setToolTip(message)

    def _device_change_detected(self) -> None:
        if self._closing:
            return

        self._rescan_requested = True
        self._status.setStyleSheet("")
        self._status.setText("USB hardware change detected…")
        self._device_change_timer.start()

    def _update_slot_button(self) -> None:
        self._assign_slot_button.setEnabled(
            self._benchmark_thread is None
            and bool(self._table.selectionModel().selectedRows())
        )

    def _assign_selected_slot(self) -> None:
        selected_rows = self._table.selectionModel().selectedRows()
        if not selected_rows:
            return

        device = self._model.device_at(selected_rows[0].row())
        if device is None:
            return

        try:
            reserved_slots = self._slot_store.reserved_slots()
        except SlotStoreError as error:
            QMessageBox.warning(self, "Slot configuration error", str(error))
            return

        choices = ["Unassigned"]
        choices.extend(
            f"Slot {slot}"
            for slot in range(1, SLOT_COUNT + 1)
            if slot == device.slot or slot not in reserved_slots
        )
        current_choice = (
            f"Slot {device.slot}" if device.slot is not None else "Unassigned"
        )
        selected_choice, accepted = QInputDialog.getItem(
            self,
            "Assign logical slot",
            f"Choose a slot for {device.model or 'this USB device'}:",
            choices,
            choices.index(current_choice),
            False,
        )
        if not accepted:
            return

        selected_slot = (
            None
            if selected_choice == "Unassigned"
            else int(selected_choice.removeprefix("Slot "))
        )
        if selected_slot == device.slot:
            return

        try:
            self._slot_store.assign(device, selected_slot)
            displayed_devices = self._slot_store.reconcile(
                self._connected_devices
            )
        except SlotConflictError as error:
            QMessageBox.warning(self, "Slot already reserved", str(error))
            return
        except SlotStoreError as error:
            QMessageBox.warning(self, "Slot configuration error", str(error))
            return

        changed_slots = tuple(
            slot for slot in (device.slot, selected_slot) if slot is not None
        )
        try:
            self._benchmark_store.remove_slots(*changed_slots)
            self._benchmark_results = self._benchmark_store.load_all()
        except BenchmarkStoreError as error:
            QMessageBox.warning(self, "Benchmark results error", str(error))
        self._model.set_benchmark_results(self._benchmark_results)

        self._model.set_devices(displayed_devices)
        self._install_benchmark_buttons()
        assignment = (
            f"Slot {selected_slot}" if selected_slot is not None else "Unassigned"
        )
        self._status.setStyleSheet("")
        self._status.setText(
            f"Saved as {assignment} — {self._slot_store.path}"
        )

    def _install_benchmark_buttons(self) -> None:
        self._benchmark_buttons.clear()
        action_column = self._model.benchmark_action_column()
        benchmark_running = self._benchmark_thread is not None
        for row in range(self._model.rowCount()):
            device = self._model.device_at(row)
            if (
                device is None
                or not device.is_connected
                or device.slot is None
            ):
                continue
            label = (
                f"Benchmark Slot {device.slot}"
                if device.drive_letters
                else "No drive letter"
            )
            button = QPushButton(label)
            button.setEnabled(bool(device.drive_letters) and not benchmark_running)
            if not device.drive_letters:
                button.setToolTip(
                    "A mounted filesystem and drive letter are required for a safe benchmark."
                )
            button.clicked.connect(
                lambda _checked=False, selected=device: self._start_benchmark(selected)
            )
            self._table.setIndexWidget(self._model.index(row, action_column), button)
            self._benchmark_buttons.append(button)

    def _open_raid10_planner(self) -> None:
        try:
            ambiguous_slots = self._slot_store.ambiguous_slots(
                self._connected_devices
            )
        except SlotStoreError as error:
            QMessageBox.warning(self, "Slot configuration error", str(error))
            ambiguous_slots = set()
        dialog = Raid10PlannerDialog(
            self._model.devices(),
            self._benchmark_results,
            self,
            plan_store=self._raid10_plan_store,
            saved_plan=self._saved_raid10_plan,
            ambiguous_slots=ambiguous_slots,
        )
        dialog.exec()
        try:
            self._saved_raid10_plan = self._raid10_plan_store.load()
        except Raid10PlanStoreError as error:
            QMessageBox.warning(self, "Saved RAID10 plan error", str(error))

    def _start_benchmark(self, device: StorageDevice) -> None:
        if self._benchmark_thread is not None or device.slot is None:
            return
        if not device.is_connected or not device.drive_letters:
            QMessageBox.warning(
                self,
                "Drive unavailable",
                "This slot does not currently have a mounted drive letter.",
            )
            return

        drive_letter = device.drive_letters[0]
        root = Path(f"{drive_letter}\\")
        try:
            free_bytes = check_benchmark_target(root, self._benchmark_settings)
        except BenchmarkSafetyError as error:
            QMessageBox.warning(self, "Benchmark safety check", str(error))
            return

        warning = QMessageBox(self)
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle(f"Benchmark Slot {device.slot}")
        warning.setText("This benchmark will write data to the USB drive.")
        warning.setInformativeText(
            "A 1 GiB temporary file will be created on the mounted filesystem, "
            "read back, and deleted. Do not unplug the drive during the test.\n\n"
            f"Target: {drive_letter} ({free_bytes / 1024**3:.2f} GiB free)"
        )
        warning.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        warning.setDefaultButton(QMessageBox.StandardButton.Cancel)
        random_checkbox = QCheckBox("Include lightweight random 4K test")
        warning.setCheckBox(random_checkbox)
        if warning.exec() != QMessageBox.StandardButton.Yes:
            return

        self._benchmark_thread = BenchmarkThread(
            root,
            device.slot,
            drive_letter,
            self._benchmark_settings,
            random_checkbox.isChecked(),
            self,
        )
        self._benchmark_thread.progress_changed.connect(self._show_benchmark_progress)
        self._benchmark_thread.benchmark_succeeded.connect(
            self._benchmark_succeeded
        )
        self._benchmark_thread.benchmark_failed.connect(self._benchmark_failed)
        self._benchmark_thread.benchmark_cancelled.connect(
            self._benchmark_cancelled
        )
        self._benchmark_thread.finished.connect(self._benchmark_finished)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._cancel_benchmark_button.setVisible(True)
        self._assign_slot_button.setEnabled(False)
        self._install_benchmark_buttons()
        self._benchmark_thread.start()

    def _show_benchmark_progress(self, percent: int, message: str) -> None:
        self._progress.setValue(percent)
        self._status.setStyleSheet("")
        self._status.setText(message)

    def _cancel_benchmark(self) -> None:
        if self._benchmark_thread is None:
            return
        self._benchmark_thread.cancel()
        self._cancel_benchmark_button.setEnabled(False)
        self._status.setText("Cancelling safely and deleting the temporary file…")

    def _benchmark_succeeded(self, result: BenchmarkResult) -> None:
        self._benchmark_results[result.slot] = result
        try:
            self._benchmark_store.save(result)
        except BenchmarkStoreError as error:
            QMessageBox.warning(self, "Could not save benchmark result", str(error))
        self._model.set_benchmark_results(self._benchmark_results)
        self._status.setText(
            f"Slot {result.slot}: {result.qualification} — benchmark complete"
        )

        random_text = ""
        if result.random_4k_read_iops is not None:
            random_text = (
                f"\nRandom 4K read: {result.random_4k_read_iops:.0f} IOPS"
                f"\nRandom 4K write: {result.random_4k_write_iops:.0f} IOPS"
            )
        QMessageBox.information(
            self,
            f"Slot {result.slot} benchmark complete",
            f"Qualification: {result.qualification}\n"
            f"Sequential read: {result.sequential_read_mbps:.1f} MB/s\n"
            f"Sequential write: {result.sequential_write_mbps:.1f} MB/s\n"
            f"Burst write: {result.burst_write_mbps:.1f} MB/s\n"
            f"Sustained write: {result.sustained_write_mbps:.1f} MB/s"
            f"{random_text}",
        )

    def _benchmark_failed(self, message: str) -> None:
        self._status.setStyleSheet("color: #b00020;")
        self._status.setText("Benchmark failed; temporary-file cleanup was attempted.")
        QMessageBox.warning(self, "Benchmark failed", message)

    def _benchmark_cancelled(self) -> None:
        self._status.setStyleSheet("")
        self._status.setText("Benchmark cancelled; temporary file deleted.")

    def _benchmark_finished(self) -> None:
        thread = self._benchmark_thread
        self._benchmark_thread = None
        self._progress.setVisible(False)
        self._cancel_benchmark_button.setVisible(False)
        self._cancel_benchmark_button.setEnabled(True)
        if thread is not None:
            thread.deleteLater()
        self._update_slot_button()
        self._install_benchmark_buttons()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _scan_finished(self) -> None:
        thread = self._scan_thread
        self._scan_thread = None
        self._refresh_button.setEnabled(True)
        if thread is not None:
            thread.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)
        elif self._rescan_requested:
            QTimer.singleShot(0, self.refresh_devices)

    def _remove_native_event_filter(self) -> None:
        if not self._native_filter_installed:
            return

        application = QApplication.instance()
        if application is not None:
            application.removeNativeEventFilter(self._device_event_filter)
        self._native_filter_installed = False

    def closeEvent(self, event: QCloseEvent) -> None:
        scan_running = self._scan_thread is not None and self._scan_thread.isRunning()
        benchmark_running = (
            self._benchmark_thread is not None
            and self._benchmark_thread.isRunning()
        )
        if scan_running or benchmark_running:
            self._closing = True
            self._device_change_timer.stop()
            self._reconciliation_timer.stop()
            self._remove_native_event_filter()
            if self._benchmark_thread is not None:
                self._benchmark_thread.cancel()
            self._status.setText("Finishing safely and deleting temporary files…")
            event.ignore()
            return
        self._remove_native_event_filter()
        event.accept()
