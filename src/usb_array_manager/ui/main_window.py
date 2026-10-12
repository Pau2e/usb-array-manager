from __future__ import annotations

import threading
from dataclasses import dataclass, field
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

from usb_array_manager import __version__
from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.raid_backend import (
    NEEDS_ADMINISTRATOR,
    SUPPORTED,
    BackendAssessment,
)
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import (
    VhdxBenchmarkComparison,
    VhdxProbeResult,
    VhdxTogetherResult,
)
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
from usb_array_manager.services.batch_results import (
    average_benchmark_results,
    average_vhdx_comparisons,
)
from usb_array_manager.services.device_monitor import WindowsDeviceEventFilter
from usb_array_manager.services.raid10_plan_store import (
    Raid10PlanStore,
    Raid10PlanStoreError,
)
from usb_array_manager.services.raid_backend import (
    VhdxStorageSpacesBackend,
    WindowsStorageSpacesBackend,
)
from usb_array_manager.services.slot_store import (
    SLOT_COUNT,
    SlotConflictError,
    SlotStore,
    SlotStoreError,
)
from usb_array_manager.services.vhdx_probe import probe_vhdx_backend
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
from usb_array_manager.ui.vhdx_together_worker import VhdxTogetherThread


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


class VhdxProbeThread(QThread):
    probe_finished = Signal(object)
    probe_failed = Signal(str)
    diagnostic = Signal(str)

    def __init__(
        self,
        device: StorageDevice,
        performance: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._device = device
        self._performance = performance
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = probe_vhdx_backend(
                self._device,
                performance=self._performance,
                cancel_event=self._cancel_event,
                diagnostic_callback=self.diagnostic.emit,
            )
        except Exception as error:
            self.probe_failed.emit(f"Unexpected VHDX probe error: {error}")
        else:
            self.probe_finished.emit(result)


@dataclass(slots=True)
class _BenchmarkBatchState:
    devices: tuple[StorageDevice, ...]
    runs: int
    include_random_4k: bool
    device_index: int = 0
    run_index: int = 0
    current_results: list[BenchmarkResult] = field(default_factory=list)
    completed: dict[int, BenchmarkResult] = field(default_factory=dict)
    failures: dict[int, str] = field(default_factory=dict)
    cancelled: bool = False
    stop_reason: str = ""


@dataclass(slots=True)
class _VhdxBatchState:
    devices: tuple[StorageDevice, ...]
    performance: bool
    runs: int
    device_index: int = 0
    run_index: int = 0
    current_results: list[VhdxBenchmarkComparison] = field(default_factory=list)
    completed: dict[int, tuple[int, VhdxBenchmarkComparison]] = field(
        default_factory=dict
    )
    probe_results: dict[int, VhdxProbeResult] = field(default_factory=dict)
    failures: dict[int, str] = field(default_factory=dict)
    cancelled: bool = False
    stop_reason: str = ""


class MainWindow(QMainWindow):
    DEVICE_CHANGE_DEBOUNCE_MS = 500
    RECONCILIATION_INTERVAL_MS = 30_000

    def __init__(self) -> None:
        super().__init__()
        self._scan_thread: DeviceScanThread | None = None
        self._benchmark_thread: BenchmarkThread | None = None
        self._vhdx_thread: VhdxProbeThread | None = None
        self._together_thread: VhdxTogetherThread | None = None
        self._together_cleanup_blocker = ""
        self._benchmark_batch: _BenchmarkBatchState | None = None
        self._vhdx_batch: _VhdxBatchState | None = None
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
        windows_backend = WindowsStorageSpacesBackend()
        self._vhdx_backend = VhdxStorageSpacesBackend()
        self._raid_backends = {
            windows_backend.name: windows_backend,
            self._vhdx_backend.name: self._vhdx_backend,
        }
        self._raid_backend = windows_backend
        self._selected_backend_name = windows_backend.name

        self.setWindowTitle(f"USB Array Manager {__version__} — VHDX backend evaluation")
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

        self._backend_tab.backend_changed.connect(self._backend_selection_changed)
        self._backend_tab.probe_requested.connect(self._start_vhdx_probe)
        self._backend_tab.probe_all_requested.connect(
            lambda: self._start_vhdx_batch(performance=False, runs=1)
        )
        self._backend_tab.together_requested.connect(self._start_vhdx_together)
        self._backend_tab.performance_requested.connect(
            lambda slot: self._start_vhdx_probe(slot, performance=True)
        )
        self._backend_tab.performance_all_requested.connect(
            lambda runs: self._start_vhdx_batch(performance=True, runs=runs)
        )
        self._backend_tab.batch_cancel_button.clicked.connect(self._cancel_vhdx_batch)
        self._benchmarks_tab.benchmark_all_requested.connect(
            self._start_benchmark_batch
        )

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
        self._backend_tab.set_devices(displayed_devices)
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
            not self._storage_job_active()
            and bool(self._table.selectionModel().selectedRows())
        )

    def _assign_selected_slot(self) -> None:
        if self._storage_job_active():
            return
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
        benchmark_running = self._storage_job_active()
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

    def _storage_job_active(self) -> bool:
        return (
            self._benchmark_thread is not None
            or self._vhdx_thread is not None
            or self._together_thread is not None
            or self._benchmark_batch is not None
            or self._vhdx_batch is not None
        )

    def _set_storage_controls_busy(self, busy: bool) -> None:
        self._benchmarks_tab.set_operation_running(busy)
        self._backend_tab.set_probe_running(busy)
        self._refresh_button.setEnabled(not busy and self._scan_thread is None)
        self._assign_slot_button.setEnabled(False)
        if self._raid10_workspace is not None:
            self._raid10_workspace.planner_page.setEnabled(not busy)
        self._install_benchmark_buttons()
        if not busy:
            self._update_slot_button()

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
        workspace.planner_page.setEnabled(not self._storage_job_active())
        planner_index = self._tabs.indexOf(self._backend_tab)
        self._tabs.insertTab(planner_index, workspace.planner_page, "RAID10 Planner")
        self._tabs.insertTab(planner_index + 1, workspace.failure_page, "Failure Simulator")
        self._refresh_backend_assessments(ambiguous_slots)
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
        assessments = self._refresh_backend_assessments(ambiguous_slots)
        assessment = assessments[self._selected_backend_name]
        self._log(
            "Backend capability",
            f"{assessment.backend_name}: {assessment.status}. {assessment.explanation}",
        )

    def _refresh_backend_assessments(
        self,
        ambiguous_slots: set[int] | frozenset[int],
    ) -> dict[str, BackendAssessment]:
        assessments = {
            name: backend.assess(
                self._saved_raid10_plan,
                self._model.devices(),
                ambiguous_slots=ambiguous_slots,
            )
            for name, backend in self._raid_backends.items()
        }
        self._backend_tab.set_assessments(
            assessments,
            self._selected_backend_name,
        )
        self._backend_tab.set_production_plan(self._vhdx_backend.production_plan(
            self._saved_raid10_plan, self._model.devices(), ambiguous_slots=ambiguous_slots,
        ))
        return assessments

    def _backend_selection_changed(self, name: str) -> None:
        if name in self._raid_backends:
            self._selected_backend_name = name

    def _start_vhdx_probe(self, slot: int, performance: bool = False) -> None:
        if self._storage_job_active():
            return
        if self._together_cleanup_blocker:
            QMessageBox.warning(self, "VHDX cleanup required", self._together_cleanup_blocker)
            return
        device = next(
            (item for item in self._model.devices() if item.slot == slot and item.is_connected),
            None,
        )
        if device is None:
            QMessageBox.warning(self, "VHDX probe", f"Slot {slot} is not connected.")
            return
        if performance:
            if not self._vhdx_performance_ready(device):
                message = (
                    "Run a successful VHDX capability probe for this connected USB "
                    "slot before starting the performance test."
                )
                self._log("VHDX performance test", message)
                QMessageBox.warning(self, "VHDX performance test unavailable", message)
                return
        action = "performance test" if performance else "capability probe"
        warning = QMessageBox(self)
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle(f"VHDX {action} — Slot {slot}")
        warning.setText("A temporary 8 GiB expandable VHDX will be created on this USB filesystem.")
        warning.setInformativeText(
            "The file uses a unique name inside .usb-array-manager-vhdx-test. "
            "It will be attached, inspected, detached, and deleted. The physical USB "
            "disk will not be initialized, partitioned, or formatted.\n\n"
            + (
                "The performance test initializes and formats only the disposable virtual "
                "disk inside that temporary VHDX file."
                if performance else
                "No filesystem is created during the capability-only probe."
            )
        )
        warning.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        warning.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if warning.exec() != QMessageBox.StandardButton.Yes:
            self._log("VHDX probe", f"Slot {slot} {action} cancelled before file creation.")
            return
        self._launch_vhdx_thread(device, performance)

    def _launch_vhdx_thread(self, device: StorageDevice, performance: bool) -> None:
        action = "performance test" if performance else "capability probe"
        self._selected_backend_name = self._vhdx_backend.name
        self._backend_tab.backend_selector.setCurrentText(self._selected_backend_name)
        self._vhdx_thread = VhdxProbeThread(device, performance, self)
        self._vhdx_thread.probe_finished.connect(self._vhdx_probe_finished)
        self._vhdx_thread.probe_failed.connect(self._vhdx_probe_failed)
        self._vhdx_thread.diagnostic.connect(
            lambda message: self._log("VHDX DiskPart", message)
        )
        self._vhdx_thread.finished.connect(self._vhdx_thread_finished)
        self._set_storage_controls_busy(True)
        self._log("VHDX probe", f"Started Slot {device.slot} {action}; VHDX size 8 GiB (expandable).")
        self._vhdx_thread.start()

    def _vhdx_probe_finished(self, result: VhdxProbeResult) -> None:
        state = self._vhdx_batch
        if state is not None:
            self._record_vhdx_batch_run(result)
        self._vhdx_backend.record_probe(result)
        self._backend_tab.set_probe_result(result)
        try:
            ambiguous = self._slot_store.ambiguous_slots(self._connected_devices)
        except SlotStoreError:
            ambiguous = set()
        self._refresh_backend_assessments(ambiguous)
        self._log(
            "VHDX probe",
            " ".join(
                item
                for item in (
                    f"Slot {result.slot}: {result.status}.",
                    result.explanation,
                    f"Cleanup: {result.cleanup_status}.",
                    (
                        f"Diagnostic details: {result.diagnostic_details}"
                        if result.diagnostic_details
                        else ""
                    ),
                )
                if item
            ),
        )
        if result.leftover_path:
            QMessageBox.critical(
                self,
                "VHDX cleanup failed",
                f"Manual cleanup is required. The file may remain attached or present at:\n"
                f"{result.leftover_path}\n\n{result.cleanup_status}",
            )
        elif result.status == NEEDS_ADMINISTRATOR:
            QMessageBox.warning(
                self,
                "Administrator privileges required",
                result.explanation,
            )
        if state is None:
            self._backend_tab.set_batch_progress(
                f"Slot {result.slot}: {result.status} — cleanup: {result.cleanup_status}"
            )

    def _vhdx_probe_failed(self, message: str) -> None:
        self._log("VHDX probe error", message)
        state = self._vhdx_batch
        if state is not None:
            slot = state.devices[state.device_index].slot or 0
            state.failures[slot] = message
            state.stop_reason = "Unexpected probe error; cleanup could not be verified."
        QMessageBox.warning(
            self,
            "VHDX probe failed",
            message
            + "\n\nNo production Storage Spaces operation was attempted. "
            "Review Logs / Details before retrying.",
        )

    def _vhdx_thread_finished(self) -> None:
        if self._vhdx_thread is not None:
            self._vhdx_thread.deleteLater()
        self._vhdx_thread = None
        if self._vhdx_batch is not None:
            if self._closing:
                self._vhdx_batch.cancelled = True
            QTimer.singleShot(0, self._start_next_vhdx_batch_step)
            return
        self._set_storage_controls_busy(False)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _batch_device(self, expected: StorageDevice) -> StorageDevice:
        matches = [
            item for item in self._model.devices()
            if item.slot == expected.slot and item.is_connected
        ]
        ambiguous = self._slot_store.ambiguous_slots(self._connected_devices)
        if len(matches) != 1 or expected.slot in ambiguous:
            raise ValueError("The slot is disconnected or ambiguous.")
        current = matches[0]
        duplicates = [
            item for item in self._model.devices()
            if item.is_connected and item.slot is not None
            and (
                (_device_identity(current) and _device_identity(item) == _device_identity(current))
                or (current.device_path and item.device_path == current.device_path)
            )
        ]
        if len(duplicates) != 1:
            raise ValueError("The physical device maps to duplicate assigned slots.")
        if (
            not _device_identity(expected)
            or _device_identity(current) != _device_identity(expected)
            or current.device_path != expected.device_path
            or current.drive_letters != expected.drive_letters
        ):
            raise ValueError("The slot identity or mounted filesystem changed during the batch.")
        return current

    def _vhdx_performance_ready(self, device: StorageDevice) -> bool:
        capability = self._vhdx_backend.probe_for_slot(device.slot or 0)
        return bool(
            capability is not None
            and capability.status == SUPPORTED
            and capability.leftover_path is None
            and capability.source_identity == _device_identity(device)
        )

    def _four_vhdx_devices(self, *, performance: bool = False) -> tuple[StorageDevice, ...]:
        if self._together_cleanup_blocker:
            raise ValueError(self._together_cleanup_blocker)
        for slot in range(1, SLOT_COUNT + 1):
            previous = self._vhdx_backend.probe_for_slot(slot)
            if previous is not None and (
                previous.leftover_path
                or previous.cleanup_status.upper().startswith("FAILED")
            ):
                message = (
                    f"Slot {slot} has an unresolved cleanup failure. Resolve it and "
                    "restart the application before starting another VHDX batch. "
                    + (f"Leftover: {previous.leftover_path}" if previous.leftover_path else previous.cleanup_status)
                )
                raise ValueError(message)
        slots = (
            sorted(self._saved_raid10_plan.slots)
            if self._saved_raid10_plan is not None
            else list(range(1, SLOT_COUNT + 1))
        )
        devices: list[StorageDevice] = []
        for slot in slots:
            matches = [
                item for item in self._model.devices()
                if item.slot == slot and item.is_connected
            ]
            if len(matches) != 1:
                raise ValueError(f"Slot {slot} must map to one connected assigned drive.")
            current = self._batch_device(matches[0])
            if performance and not self._vhdx_performance_ready(current):
                raise ValueError(
                    f"Slot {slot} requires a successful capability probe for its current identity."
                )
            devices.append(current)
        if len(devices) != 4 or len(set(slots)) != 4:
            raise ValueError("Exactly four assigned RAID-member slots are required.")
        return tuple(devices)

    def _start_vhdx_batch(self, *, performance: bool, runs: int) -> None:
        if self._storage_job_active():
            return
        if runs not in (1, 3, 5):
            raise ValueError("Runs must be 1, 3, or 5.")
        try:
            devices = self._four_vhdx_devices(performance=performance)
        except (ValueError, SlotStoreError) as error:
            QMessageBox.warning(self, "VHDX batch unavailable", str(error))
            self._log("VHDX batch", str(error))
            return

        action = "performance test" if performance else "capability probe"
        warning = QMessageBox(self)
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle(f"VHDX batch {action}")
        warning.setText(f"Run a sequential {action} on all four RAID-member slots?")
        warning.setInformativeText(
            f"Each slot will run {runs} time(s). Each run creates one unique temporary "
            "8 GiB expandable VHDX inside .usb-array-manager-vhdx-test. It is attached, "
            "inspected, detached and deleted before the next run starts. Do not unplug "
            "a drive during its active run. Physical USB disks are never initialized, "
            "partitioned or formatted.\n\n"
            + (
                "Performance testing initializes and formats only the verified disposable "
                "virtual disk and benchmarks temporary files on both filesystems."
                if performance else "Capability-only probes do not create a filesystem."
            )
        )
        warning.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        warning.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if warning.exec() != QMessageBox.StandardButton.Yes:
            return
        self._vhdx_batch = _VhdxBatchState(tuple(devices), performance, runs)
        self._backend_tab.set_performance_batch_results({})
        self._backend_tab.set_probe_batch_summary([f"Batch {action} started."])
        self._backend_tab.batch_cancel_button.setVisible(True)
        self._backend_tab.batch_cancel_button.setEnabled(True)
        self._set_storage_controls_busy(True)
        self._log("VHDX batch", f"Started {action}, {runs} run(s) per slot, sequentially.")
        self._start_next_vhdx_batch_step()

    def _start_vhdx_together(self) -> None:
        if self._storage_job_active():
            return
        try:
            devices = self._four_vhdx_devices()
        except (ValueError, SlotStoreError) as error:
            QMessageBox.warning(self, "Four-member validation unavailable", str(error))
            self._log("Four-member validation", str(error))
            return
        warning = QMessageBox(self)
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle("Validate 4 VHDXs Together")
        warning.setText("Create and attach four temporary 8 GiB expandable VHDXs together?")
        warning.setInformativeText(
            "One unique temporary file will be created on each member. All four remain "
            "attached for a shared identity and Storage Spaces eligibility check, then "
            "are detached and deleted. This test does not create a pool, filesystem or "
            "production VHDX, and runs no benchmarks. Do not unplug the drives.\n\n"
            + "\n".join(f"Slot {item.slot}: {item.model} — {', '.join(item.drive_letters)}" for item in devices)
        )
        warning.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        warning.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if warning.exec() != QMessageBox.StandardButton.Yes:
            return
        self._selected_backend_name = self._vhdx_backend.name
        self._backend_tab.backend_selector.setCurrentText(self._selected_backend_name)
        self._together_thread = VhdxTogetherThread(devices, self)
        self._together_thread.saved_plan = self._saved_raid10_plan
        self._together_thread.validation_finished.connect(self._vhdx_together_result)
        self._together_thread.validation_failed.connect(self._vhdx_together_failed)
        self._together_thread.progress.connect(self._backend_tab.set_batch_progress)
        self._together_thread.diagnostic.connect(lambda message: self._log("Four-member validation", message))
        self._together_thread.finished.connect(self._vhdx_together_finished)
        self._backend_tab.show_together_started()
        self._backend_tab.batch_cancel_button.setVisible(False)
        self._set_storage_controls_busy(True)
        self._together_thread.start()

    def _vhdx_together_result(self, result: VhdxTogetherResult) -> None:
        devices = self._together_thread.devices if self._together_thread is not None else ()
        plan = getattr(self._together_thread, "saved_plan", None)
        self._vhdx_backend.record_together(result, devices, plan)
        try:
            ambiguous = self._slot_store.ambiguous_slots(self._connected_devices)
        except SlotStoreError:
            ambiguous = set()
        self._refresh_backend_assessments(ambiguous)
        self._backend_tab.set_together_result(result)
        self._backend_tab.set_batch_progress("Four-member validation finished; review cleanup results.")
        self._log("Four-member validation result", f"{result.status}: {result.explanation}")
        problems = [
            f"Slot {member.slot}: {member.vhdx_path} — {member.cleanup_status}; may remain attached={member.may_remain_attached}"
            for member in result.members
            if member.leftover_path or member.may_remain_attached or member.status == "CLEANUP FAILED"
        ]
        if problems:
            self._together_cleanup_blocker = (
                "Resolve these cleanup failures and restart the application before further VHDX tests:\n"
                + "\n".join(problems)
            )
            QMessageBox.critical(self, "VHDX cleanup required", self._together_cleanup_blocker)

    def _vhdx_together_failed(self, message: str) -> None:
        self._vhdx_backend.record_together(None)
        try:
            ambiguous = self._slot_store.ambiguous_slots(self._connected_devices)
        except SlotStoreError:
            ambiguous = set()
        self._refresh_backend_assessments(ambiguous)
        self._together_cleanup_blocker = (
            "Unexpected validation error; review cleanup in Logs / Details before restarting: " + message
        )
        self._log("Four-member validation error", self._together_cleanup_blocker)
        self._backend_tab.set_probe_batch_summary(["FAILED: " + self._together_cleanup_blocker])
        QMessageBox.warning(self, "Four-member validation failed", self._together_cleanup_blocker)

    def _vhdx_together_finished(self) -> None:
        if self._together_thread is not None:
            self._together_thread.deleteLater()
        self._together_thread = None
        self._set_storage_controls_busy(False)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _start_next_vhdx_batch_step(self) -> None:
        state = self._vhdx_batch
        if state is None or self._vhdx_thread is not None:
            return
        if state.stop_reason:
            self._finish_vhdx_batch()
            return
        if state.device_index >= len(state.devices):
            self._finish_vhdx_batch()
            return
        expected = state.devices[state.device_index]
        slot = expected.slot or 0
        if state.run_index >= state.runs:
            if state.performance:
                state.completed[slot] = (
                    len(state.current_results),
                    average_vhdx_comparisons(state.current_results),
                )
                self._backend_tab.set_performance_batch_results(state.completed)
            state.device_index += 1
            state.run_index = 0
            state.current_results = []
            QTimer.singleShot(0, self._start_next_vhdx_batch_step)
            return
        if state.cancelled or self._closing:
            self._finish_vhdx_batch()
            return
        try:
            device = self._batch_device(expected)
            if state.performance and not self._vhdx_performance_ready(device):
                raise ValueError("A successful capability probe for the current slot is required.")
        except (ValueError, SlotStoreError) as error:
            state.failures[slot] = str(error)
            self._log("VHDX batch error", f"Slot {slot}: {error}")
            state.device_index += 1
            state.run_index = 0
            state.current_results = []
            QTimer.singleShot(0, self._start_next_vhdx_batch_step)
            return
        action = "Testing performance" if state.performance else "Probing"
        self._backend_tab.set_batch_progress(
            f"{action} Slot {slot} ({state.device_index + 1} of {len(state.devices)}) "
            f"— run {state.run_index + 1} of {state.runs}"
        )
        self._launch_vhdx_thread(device, state.performance)

    def _record_vhdx_batch_run(self, result: VhdxProbeResult) -> None:
        state = self._vhdx_batch
        if state is None:
            return
        state.probe_results[result.slot] = result
        self._log(
            "VHDX batch run",
            f"Slot {result.slot}, run {state.run_index + 1}/{state.runs}: "
            f"{result.status}; cleanup: {result.cleanup_status}. "
            + (repr(result.benchmark) if result.benchmark is not None else result.explanation),
        )
        if result.benchmark is not None:
            item = result.benchmark
            self._log(
                "VHDX batch overhead",
                f"Slot {result.slot}, run {state.run_index + 1}: R/W/S "
                f"{item.read_overhead_percent:+.2f}% / {item.write_overhead_percent:+.2f}% / "
                f"{item.sustained_write_overhead_percent:+.2f}%.",
            )
        if result.leftover_path or result.cleanup_status.upper().startswith("FAILED"):
            state.stop_reason = f"Slot {result.slot}: cleanup failed; batch stopped."
        elif result.status == NEEDS_ADMINISTRATOR:
            state.stop_reason = "Administrator privileges are required; batch stopped."
        failed = bool(
            result.leftover_path
            or result.cleanup_status.upper().startswith("FAILED")
            or (state.performance and (result.status != SUPPORTED or result.benchmark is None))
        )
        if failed:
            state.failures[result.slot] = result.explanation
            state.device_index += 1
            state.run_index = 0
            state.current_results = []
        else:
            if result.benchmark is not None:
                state.current_results.append(result.benchmark)
            state.run_index += 1

    def _cancel_vhdx_batch(self) -> None:
        if self._vhdx_batch is None:
            return
        self._vhdx_batch.cancelled = True
        self._backend_tab.batch_cancel_button.setEnabled(False)
        self._backend_tab.set_batch_progress(
            "Cancellation requested — finishing the current run and cleanup first."
        )
        self._log("VHDX batch", "Cancellation requested between runs; current lifecycle will finish.")

    def _finish_vhdx_batch(self) -> None:
        state = self._vhdx_batch
        if state is None:
            return
        lines: list[str] = []
        for device in state.devices:
            slot = device.slot or 0
            result = state.probe_results.get(slot)
            if slot in state.failures:
                lines.append(f"Slot {slot}: FAILED — {state.failures[slot]}")
            elif state.performance and slot in state.completed:
                lines.append(f"Slot {slot}: {state.completed[slot][0]} performance run(s) averaged")
            elif not state.performance and result is not None:
                poolable = result.virtual_disk.can_pool if result.virtual_disk else None
                lines.append(f"Slot {slot}: {result.status} — CanPool={poolable}")
            else:
                lines.append(f"Slot {slot}: NOT COMPLETED")
        count = (
            len(state.completed) if state.performance
            else sum(result.status == SUPPORTED for result in state.probe_results.values())
        )
        lines.append(
            f"Overall: {count}/{len(state.devices)} "
            + ("performance results completed" if state.performance else "SUPPORTED")
        )
        if state.cancelled or self._closing:
            lines.append("Cancelled after the current run and cleanup.")
        if state.stop_reason:
            lines.append(state.stop_reason)
        self._backend_tab.set_probe_batch_summary(lines)
        self._backend_tab.set_batch_progress("Batch finished.")
        self._backend_tab.set_performance_batch_results(state.completed)
        self._backend_tab.batch_cancel_button.setVisible(False)
        self._vhdx_batch = None
        self._set_storage_controls_busy(False)
        self._log("VHDX batch summary", "\n".join(lines))
        if self._closing:
            QTimer.singleShot(0, self.close)
        else:
            QMessageBox.information(self, "VHDX batch summary", "\n".join(lines))

    def _set_benchmark_results_on_models(self) -> None:
        self._model.set_benchmark_results(self._benchmark_results)
        self._benchmark_model.set_benchmark_results(self._benchmark_results)

    def _log(self, category: str, message: str) -> None:
        self._logs_tab.add_entry(category, message)

    def _start_benchmark(self, device: StorageDevice) -> None:
        if self._storage_job_active() or device.slot is None:
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

        self._launch_benchmark_thread(
            device,
            random_checkbox.isChecked(),
        )

    def _launch_benchmark_thread(
        self,
        device: StorageDevice,
        include_random_4k: bool,
    ) -> None:
        if device.slot is None or not device.drive_letters:
            raise ValueError("A connected assigned slot with a drive letter is required.")
        drive_letter = device.drive_letters[0]
        root = Path(f"{drive_letter}\\")
        self._benchmark_thread = BenchmarkThread(
            root,
            device.slot,
            drive_letter,
            self._benchmark_settings,
            include_random_4k,
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
        self._set_storage_controls_busy(True)
        self._benchmarks_tab.status.setStyleSheet("")
        if self._benchmark_batch is None:
            self._benchmarks_tab.status.setText(
                f"Starting benchmark for Slot {device.slot}…"
            )
        self._log(
            "Benchmark",
            f"Started Slot {device.slot} on {drive_letter}; temporary-file safety checks passed.",
        )
        self._benchmark_thread.start()

    def _start_benchmark_batch(self, runs: int) -> None:
        if self._storage_job_active():
            return
        if runs not in (1, 3, 5):
            raise ValueError("Runs must be 1, 3, or 5.")
        devices = tuple(
            sorted(
                (
                    item
                    for item in self._model.devices()
                    if item.slot is not None and item.is_connected
                ),
                key=lambda item: item.slot or 0,
            )
        )
        if not devices:
            QMessageBox.warning(
                self,
                "Benchmark All",
                "No connected assigned slots are available.",
            )
            return

        warning = QMessageBox(self)
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle("Benchmark all connected assigned slots")
        warning.setText("This batch benchmark will write temporary data sequentially.")
        warning.setInformativeText(
            f"{len(devices)} slot(s) will run {runs} time(s) each. Only one USB drive "
            "is benchmarked at a time. Every run uses the existing 1 GiB temporary-file "
            "safety checks and cleanup path. Do not unplug a drive during its active run."
            + "\n\nTargets:\n"
            + "\n".join(
                f"Slot {item.slot}: {item.model or 'USB device'} — "
                f"{', '.join(item.drive_letters) or 'No drive letter'}"
                for item in devices
            )
        )
        warning.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        warning.setDefaultButton(QMessageBox.StandardButton.Cancel)
        random_checkbox = QCheckBox("Include lightweight random 4K test")
        warning.setCheckBox(random_checkbox)
        if warning.exec() != QMessageBox.StandardButton.Yes:
            self._log("Benchmark batch", "Batch confirmation was cancelled.")
            return

        self._benchmark_batch = _BenchmarkBatchState(
            devices=devices,
            runs=runs,
            include_random_4k=random_checkbox.isChecked(),
        )
        self._set_storage_controls_busy(True)
        self._progress.setVisible(True)
        self._cancel_benchmark_button.setVisible(True)
        self._log(
            "Benchmark batch",
            f"Started {len(devices)} slot(s), {runs} run(s) per slot; sequential execution enforced.",
        )
        self._start_next_benchmark_batch_step()

    def _start_next_benchmark_batch_step(self) -> None:
        state = self._benchmark_batch
        if state is None or self._benchmark_thread is not None:
            return
        if state.device_index >= len(state.devices):
            self._finish_benchmark_batch(cancelled=state.cancelled)
            return

        device = state.devices[state.device_index]
        slot = device.slot
        if slot is None:
            state.device_index += 1
            self._start_next_benchmark_batch_step()
            return

        if state.run_index >= state.runs:
            try:
                self._batch_device(device)
            except (ValueError, SlotStoreError) as error:
                self._fail_current_benchmark_batch_slot(str(error))
                return
            averaged = average_benchmark_results(state.current_results)
            state.completed[slot] = averaged
            self._save_benchmark_result(averaged)
            self._set_benchmark_results_on_models()
            self._install_benchmark_buttons()
            self._log(
                "Benchmark batch",
                f"Slot {slot} average across {averaged.run_count} run(s): read "
                f"{averaged.sequential_read_mbps:.1f}, write "
                f"{averaged.sequential_write_mbps:.1f}, sustained "
                f"{averaged.sustained_write_mbps:.1f} MB/s; {averaged.qualification}.",
            )
            state.device_index += 1
            state.run_index = 0
            state.current_results = []
            self._start_next_benchmark_batch_step()
            return

        if state.cancelled or state.stop_reason or self._closing:
            self._finish_benchmark_batch(cancelled=state.cancelled or self._closing)
            return

        ordinal = state.device_index + 1
        run_number = state.run_index + 1
        self._benchmarks_tab.status.setStyleSheet("")
        self._benchmarks_tab.status.setText(
            f"Benchmarking Slot {slot} of {len(state.devices)} — run "
            f"{run_number} of {state.runs}"
        )
        if not device.drive_letters:
            self._fail_current_benchmark_batch_slot(
                "No mounted drive letter is available."
            )
            return
        root = Path(f"{device.drive_letters[0]}\\")
        try:
            device = self._batch_device(device)
            check_benchmark_target(root, self._benchmark_settings)
        except (BenchmarkSafetyError, ValueError, SlotStoreError) as error:
            self._fail_current_benchmark_batch_slot(str(error))
            return
        self._log(
            "Benchmark batch",
            f"Slot {slot}, run {run_number}/{state.runs} started "
            f"({ordinal}/{len(state.devices)} slots).",
        )
        self._launch_benchmark_thread(device, state.include_random_4k)

    def _fail_current_benchmark_batch_slot(self, message: str, *, schedule: bool = True) -> None:
        state = self._benchmark_batch
        if state is None:
            return
        device = state.devices[state.device_index]
        slot = device.slot or 0
        state.failures[slot] = message
        self._log("Benchmark batch error", f"Slot {slot}: {message}")
        state.device_index += 1
        state.run_index = 0
        state.current_results = []
        if schedule:
            QTimer.singleShot(0, self._start_next_benchmark_batch_step)

    def _finish_benchmark_batch(self, *, cancelled: bool) -> None:
        state = self._benchmark_batch
        if state is None:
            return
        lines = [
            *(
                f"Slot {slot}: {result.qualification} — {result.run_count} run(s) averaged"
                for slot, result in sorted(state.completed.items())
            ),
            *(
                f"Slot {slot}: FAILED — {message}"
                for slot, message in sorted(state.failures.items())
            ),
            *(
                f"Slot {device.slot}: NOT COMPLETED — previous result retained"
                for device in state.devices
                if device.slot not in state.completed and device.slot not in state.failures
            ),
        ]
        heading = "Benchmark batch cancelled" if cancelled else "Benchmark batch complete"
        summary = "\n".join(lines) or "No slots completed."
        if state.stop_reason:
            summary += f"\n{state.stop_reason}"
        self._benchmark_batch = None
        self._progress.setVisible(False)
        self._cancel_benchmark_button.setVisible(False)
        self._cancel_benchmark_button.setEnabled(True)
        self._set_storage_controls_busy(False)
        self._benchmarks_tab.status.setStyleSheet("" if not state.failures else "color: #9a6700;")
        self._benchmarks_tab.status.setText(
            f"{heading}: {len(state.completed)} succeeded, {len(state.failures)} failed."
        )
        self._benchmarks_tab.status.setToolTip(summary)
        self._set_benchmark_results_on_models()
        self._install_benchmark_buttons()
        self._rebuild_raid_workspace()
        self._log("Benchmark batch", f"{heading}. {summary.replace(chr(10), '; ')}")
        if self._closing:
            QTimer.singleShot(0, self.close)
        else:
            QMessageBox.information(self, heading, summary)

    def _show_benchmark_progress(self, percent: int, message: str) -> None:
        self._progress.setValue(percent)
        self._benchmarks_tab.status.setStyleSheet("")
        state = self._benchmark_batch
        if state is None:
            self._benchmarks_tab.status.setText(message)
            return
        if state.cancelled:
            return
        device = state.devices[state.device_index]
        self._benchmarks_tab.status.setText(
            f"Benchmarking Slot {device.slot} of {len(state.devices)} — run "
            f"{state.run_index + 1} of {state.runs}: {message}"
        )

    def _cancel_benchmark(self) -> None:
        if self._benchmark_batch is not None:
            self._benchmark_batch.cancelled = True
            self._cancel_benchmark_button.setEnabled(False)
            self._benchmarks_tab.status.setText(
                "Cancellation requested — finishing the current run and cleanup first."
            )
            self._log("Benchmark batch", "Cancellation requested between runs.")
            return
        if self._benchmark_thread is None:
            return
        self._benchmark_thread.cancel()
        self._cancel_benchmark_button.setEnabled(False)
        self._benchmarks_tab.status.setText(
            "Cancelling safely and deleting the temporary file…"
        )
        self._log("Benchmark", "Cancellation requested; cleanup is in progress.")

    def _benchmark_succeeded(self, result: BenchmarkResult) -> None:
        state = self._benchmark_batch
        if state is not None:
            state.current_results.append(result)
            state.run_index += 1
            self._log(
                "Benchmark batch run",
                f"Slot {result.slot}, run {state.run_index}/{state.runs}: {result.to_dict()}",
            )
            return
        self._save_benchmark_result(result)
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

    def _save_benchmark_result(self, result: BenchmarkResult) -> None:
        self._benchmark_results[result.slot] = result
        try:
            self._benchmark_store.save(result)
        except BenchmarkStoreError as error:
            self._log("Benchmark results", str(error))
            QMessageBox.warning(self, "Could not save benchmark result", str(error))

    def _benchmark_failed(self, message: str) -> None:
        if self._benchmark_batch is not None:
            if "cleanup" in message.lower() or "could not be deleted" in message.lower():
                self._benchmark_batch.stop_reason = (
                    "Temporary benchmark cleanup failed; batch stopped."
                )
            self._fail_current_benchmark_batch_slot(message, schedule=False)
            return
        self._benchmarks_tab.status.setStyleSheet("color: #b00020;")
        self._benchmarks_tab.status.setText(
            "Benchmark failed; temporary-file cleanup was attempted."
        )
        self._log("Benchmark error", message)
        QMessageBox.warning(self, "Benchmark failed", message)

    def _benchmark_cancelled(self) -> None:
        if self._benchmark_batch is not None:
            self._benchmark_batch.cancelled = True
            return
        self._benchmarks_tab.status.setStyleSheet("")
        self._benchmarks_tab.status.setText(
            "Benchmark cancelled; temporary file deleted."
        )
        self._log("Benchmark", "Benchmark cancelled; temporary file deleted.")

    def _benchmark_finished(self) -> None:
        thread = self._benchmark_thread
        self._benchmark_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._benchmark_batch is not None:
            QTimer.singleShot(0, self._start_next_benchmark_batch_step)
            return
        self._progress.setVisible(False)
        self._cancel_benchmark_button.setVisible(False)
        self._cancel_benchmark_button.setEnabled(True)
        self._set_storage_controls_busy(False)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _scan_finished(self) -> None:
        thread = self._scan_thread
        self._scan_thread = None
        self._refresh_button.setEnabled(not self._storage_job_active())
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
        if scan_running or self._storage_job_active():
            self._closing = True
            self._device_change_timer.stop()
            self._reconciliation_timer.stop()
            self._remove_native_event_filter()
            if self._benchmark_batch is not None:
                self._benchmark_batch.cancelled = True
            elif self._benchmark_thread is not None:
                self._benchmark_thread.cancel()
            if self._vhdx_batch is not None:
                self._vhdx_batch.cancelled = True
            elif self._vhdx_thread is not None:
                self._vhdx_thread.cancel()
            if self._together_thread is not None:
                self._together_thread.cancel()
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


def _device_identity(device: StorageDevice) -> str:
    return (
        device.storage_unique_id
        or device.container_id
        or device.pnp_device_id
        or device.device_path
        or ""
    )
