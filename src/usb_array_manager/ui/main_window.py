from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QDateTime, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
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
from usb_array_manager.services.raid_backend import WindowsStorageSpacesBackend
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
from usb_array_manager.ui.device_tabs import (
    BackendDeploymentTab,
    BenchmarksTab,
    LogsTab,
    OverviewTab,
)
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
        self._raid10_workspace: Raid10PlannerDialog | None = None
        self._last_inventory_signature: tuple | None = None
        self._last_readiness_text: str | None = None
        self._raid_backend = WindowsStorageSpacesBackend()

        self.setWindowTitle("USB Array Manager 0.6 — Read-only deployment planning")
        self.setMinimumSize(1_050, 650)
        self.resize(1_350, 780)

        self._overview_tab = OverviewTab()
        self._benchmarks_tab = BenchmarksTab()
        self._backend_tab = BackendDeploymentTab()
        self._logs_tab = LogsTab()
        self._model = self._overview_tab.model
        self._table = self._overview_tab.table
        self._benchmark_model = self._benchmarks_tab.model
        self._benchmark_table = self._benchmarks_tab.table
        self._status = self._overview_tab.status
        self._progress = self._benchmarks_tab.progress
        self._cancel_benchmark_button = self._benchmarks_tab.cancel_button
        self._refresh_button = self._overview_tab.refresh_button
        self._assign_slot_button = self._overview_tab.assign_button

        self._cancel_benchmark_button.clicked.connect(self._cancel_benchmark)
        self._refresh_button.clicked.connect(self.refresh_devices)
        self._assign_slot_button.clicked.connect(self._assign_selected_slot)
        self._table.selectionModel().selectionChanged.connect(
            self._update_slot_button
        )

        try:
            self._benchmark_results = self._benchmark_store.load_all()
        except BenchmarkStoreError as error:
            self._status.setStyleSheet("color: #b00020;")
            self._status.setText("Saved benchmark results could not be loaded.")
            self._status.setToolTip(str(error))
            self._log("Benchmark results", str(error))
        self._model.set_benchmark_results(self._benchmark_results)
        self._benchmark_model.set_benchmark_results(self._benchmark_results)

        try:
            self._saved_raid10_plan = self._raid10_plan_store.load()
        except Raid10PlanStoreError as error:
            self._status.setStyleSheet("color: #b00020;")
            self._status.setText("Saved RAID10 plan could not be loaded.")
            self._status.setToolTip(str(error))
            self._log("Saved plan", str(error))

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(self._overview_tab, "Overview")
        self._tabs.addTab(self._benchmarks_tab, "Benchmarks")
        self._tabs.addTab(self._backend_tab, "Backend / Deployment")
        self._tabs.addTab(self._logs_tab, "Logs / Details")
        self.setCentralWidget(self._tabs)
        self._rebuild_raid_workspace(preserve_state=False)
        if self._saved_raid10_plan is not None:
            self._log(
                "Saved plan",
                f"Loaded {len(self._saved_raid10_plan.pairs)} mirror pair(s) from "
                f"{self._raid10_plan_store.path}.",
            )

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
            self._benchmark_model.set_devices(devices)
            self._status.setStyleSheet("color: #b00020;")
            self._status.setText("Slot configuration could not be loaded.")
            self._status.setToolTip(str(error))
            self._log("Slot reconciliation", str(error))
            return

        self._model.set_devices(displayed_devices)
        self._benchmark_model.set_devices(displayed_devices)
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
        signature = _inventory_signature(displayed_devices)
        if signature != self._last_inventory_signature:
            self._log(
                "Inventory",
                f"Reconciled {len(devices)} connected device(s); "
                f"{disconnected_count} reserved slot(s) disconnected.",
            )
            self._last_inventory_signature = signature
            self._rebuild_raid_workspace()

    def _show_error(self, message: str) -> None:
        self._status.setStyleSheet("color: #b00020;")
        self._status.setText("Device scan failed. Hover here for details.")
        self._status.setToolTip(message)
        self._log("Device scan error", message)

    def _device_change_detected(self) -> None:
        if self._closing:
            return

        change_already_pending = self._device_change_timer.isActive()
        self._rescan_requested = True
        self._status.setStyleSheet("")
        self._status.setText("USB hardware change detected…")
        if not change_already_pending:
            self._log(
                "USB hardware",
                "Insertion, removal, or device-tree change detected.",
            )
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
            self._log("Slot assignment", str(error))
            return
        except SlotStoreError as error:
            QMessageBox.warning(self, "Slot configuration error", str(error))
            self._log("Slot assignment", str(error))
            return

        changed_slots = tuple(
            slot for slot in (device.slot, selected_slot) if slot is not None
        )
        try:
            self._benchmark_store.remove_slots(*changed_slots)
            self._benchmark_results = self._benchmark_store.load_all()
        except BenchmarkStoreError as error:
            QMessageBox.warning(self, "Benchmark results error", str(error))
        self._set_benchmark_results_on_models()

        self._model.set_devices(displayed_devices)
        self._benchmark_model.set_devices(displayed_devices)
        self._install_benchmark_buttons()
        assignment = (
            f"Slot {selected_slot}" if selected_slot is not None else "Unassigned"
        )
        self._status.setStyleSheet("")
        self._status.setText(
            f"Saved as {assignment} — {self._slot_store.path}"
        )
        self._log(
            "Slot assignment",
            f"{device.model or 'USB device'} changed from "
            f"{f'Slot {device.slot}' if device.slot is not None else 'Unassigned'} "
            f"to {assignment}; affected benchmark results were invalidated.",
        )
        self._last_inventory_signature = _inventory_signature(displayed_devices)
        self._rebuild_raid_workspace()

    def _install_benchmark_buttons(self) -> None:
        self._benchmark_buttons.clear()
        action_column = self._benchmark_model.benchmark_action_column()
        benchmark_running = self._benchmark_thread is not None
        for row in range(self._benchmark_model.rowCount()):
            device = self._benchmark_model.device_at(row)
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
            self._benchmark_table.setIndexWidget(
                self._benchmark_model.index(row, action_column), button
            )
            self._benchmark_buttons.append(button)

    def _open_raid10_planner(self) -> None:
        if self._raid10_workspace is not None:
            self._tabs.setCurrentWidget(self._raid10_workspace.planner_page)

    def _rebuild_raid_workspace(self, *, preserve_state: bool = True) -> None:
        current_widget = self._tabs.currentWidget()
        old_workspace = self._raid10_workspace
        pairs = (
            old_workspace.current_pairs()
            if preserve_state and old_workspace is not None
            else None
        )
        failed_slots = (
            old_workspace.simulated_failed_slots()
            if preserve_state and old_workspace is not None
            else frozenset()
        )
        was_planner = (
            old_workspace is not None
            and current_widget is old_workspace.planner_page
        )
        was_failure = (
            old_workspace is not None
            and current_widget is old_workspace.failure_page
        )
        if old_workspace is not None:
            for page in (old_workspace.planner_page, old_workspace.failure_page):
                index = self._tabs.indexOf(page)
                if index >= 0:
                    self._tabs.removeTab(index)
            old_workspace.deleteLater()

        try:
            ambiguous_slots = self._slot_store.ambiguous_slots(
                self._connected_devices
            )
        except SlotStoreError as error:
            ambiguous_slots = set()
            self._log("Slot reconciliation", str(error))

        workspace = Raid10PlannerDialog(
            self._model.devices(),
            self._benchmark_results,
            self,
            plan_store=self._raid10_plan_store,
            saved_plan=self._saved_raid10_plan,
            ambiguous_slots=ambiguous_slots,
            embedded=True,
            initial_pairs=pairs,
            initial_failed_slots=failed_slots,
        )
        workspace.plan_saved.connect(self._raid_plan_saved)
        workspace.diagnostic.connect(self._log)
        self._raid10_workspace = workspace
        self._tabs.insertTab(2, workspace.planner_page, "RAID10 Planner")
        self._tabs.insertTab(3, workspace.failure_page, "Failure Simulator")
        self._backend_tab.set_assessment(
            self._raid_backend.assess(
                self._saved_raid10_plan,
                self._model.devices(),
                ambiguous_slots=ambiguous_slots,
            )
        )
        if was_planner:
            self._tabs.setCurrentWidget(workspace.planner_page)
        elif was_failure:
            self._tabs.setCurrentWidget(workspace.failure_page)

        readiness_text = workspace._readiness_status.text()
        inventory_is_available = self._last_inventory_signature is not None
        if (
            readiness_text != self._last_readiness_text
            and (inventory_is_available or self._saved_raid10_plan is None)
        ):
            self._log("Plan validation", readiness_text)
            self._last_readiness_text = readiness_text

    def _raid_plan_saved(self, plan: SavedRaid10Plan) -> None:
        self._saved_raid10_plan = plan
        self._last_readiness_text = None
        try:
            ambiguous_slots = self._slot_store.ambiguous_slots(
                self._connected_devices
            )
        except SlotStoreError as error:
            ambiguous_slots = set()
            self._log("Slot reconciliation", str(error))
        assessment = self._raid_backend.assess(
            plan,
            self._model.devices(),
            ambiguous_slots=ambiguous_slots,
        )
        self._backend_tab.set_assessment(assessment)
        self._log(
            "Backend capability",
            f"{assessment.backend_name}: {assessment.status}. {assessment.explanation}",
        )

    def _set_benchmark_results_on_models(self) -> None:
        self._model.set_benchmark_results(self._benchmark_results)
        self._benchmark_model.set_benchmark_results(self._benchmark_results)

    def _log(self, category: str, message: str) -> None:
        self._logs_tab.add_entry(category, message)

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
            self._log(
                "Benchmark",
                f"Slot {device.slot} benchmark was not started; confirmation was cancelled.",
            )
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
        self._benchmarks_tab.status.setStyleSheet("")
        self._benchmarks_tab.status.setText(f"Starting benchmark for Slot {device.slot}…")
        self._log(
            "Benchmark",
            f"Started Slot {device.slot} on {drive_letter}; temporary-file safety checks passed.",
        )
        self._benchmark_thread.start()

    def _show_benchmark_progress(self, percent: int, message: str) -> None:
        self._progress.setValue(percent)
        self._benchmarks_tab.status.setStyleSheet("")
        self._benchmarks_tab.status.setText(message)

    def _cancel_benchmark(self) -> None:
        if self._benchmark_thread is None:
            return
        self._benchmark_thread.cancel()
        self._cancel_benchmark_button.setEnabled(False)
        self._benchmarks_tab.status.setText(
            "Cancelling safely and deleting the temporary file…"
        )
        self._log("Benchmark", "Cancellation requested; cleanup is in progress.")

    def _benchmark_succeeded(self, result: BenchmarkResult) -> None:
        self._benchmark_results[result.slot] = result
        try:
            self._benchmark_store.save(result)
        except BenchmarkStoreError as error:
            QMessageBox.warning(self, "Could not save benchmark result", str(error))
        self._set_benchmark_results_on_models()
        self._benchmarks_tab.status.setText(
            f"Slot {result.slot}: {result.qualification} — benchmark complete"
        )
        self._log(
            "Benchmark",
            f"Slot {result.slot} completed with {result.qualification}; "
            f"read {result.sequential_read_mbps:.1f} MB/s, sustained write "
            f"{result.sustained_write_mbps:.1f} MB/s.",
        )
        self._rebuild_raid_workspace()

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
        self._benchmarks_tab.status.setStyleSheet("color: #b00020;")
        self._benchmarks_tab.status.setText(
            "Benchmark failed; temporary-file cleanup was attempted."
        )
        self._log("Benchmark error", message)
        QMessageBox.warning(self, "Benchmark failed", message)

    def _benchmark_cancelled(self) -> None:
        self._benchmarks_tab.status.setStyleSheet("")
        self._benchmarks_tab.status.setText(
            "Benchmark cancelled; temporary file deleted."
        )
        self._log("Benchmark", "Benchmark cancelled; temporary file deleted.")

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
            self._benchmarks_tab.status.setText(
                "Finishing safely and deleting temporary files…"
            )
            event.ignore()
            return
        self._remove_native_event_filter()
        event.accept()


def _inventory_signature(devices: list[StorageDevice]) -> tuple:
    return tuple(
        (
            device.slot,
            device.model,
            device.serial_number,
            device.capacity_bytes,
            device.drive_letters,
            device.device_path,
            device.pnp_device_id,
            device.storage_unique_id,
            device.container_id,
            device.health_status,
            device.is_connected,
        )
        for device in devices
    )
