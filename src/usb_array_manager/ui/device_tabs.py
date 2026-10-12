from __future__ import annotations

from html import escape

from usb_array_manager import __version__

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.models.raid_backend import SUPPORTED, BackendAssessment
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import (
    VhdxBenchmarkComparison,
    VhdxProbeResult,
)
from usb_array_manager.ui.device_table_model import (
    BenchmarkTableModel,
    OverviewTableModel,
)


def _configured_table(model) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents
    )
    table.horizontalHeader().setStretchLastSection(True)
    return table


class OverviewTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = OverviewTableModel()
        self.table = _configured_table(self.model)
        self.table.setColumnWidth(1, 190)
        self.table.setColumnWidth(2, 215)
        self.table.setColumnWidth(5, 155)
        self.status = QLabel("Waiting for the first scan…")
        self.status.setWordWrap(True)
        self.assign_button = QPushButton("Assign/change slot…")
        self.assign_button.setEnabled(False)
        self.refresh_button = QPushButton("Refresh now")

        title = QLabel("Current USB storage status")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        note = QLabel(
            "Inventory and slot reconciliation are read-only. Select a row to "
            "assign or change its persistent logical slot."
        )
        note.setStyleSheet("color: #555;")
        note.setWordWrap(True)

        actions = QHBoxLayout()
        actions.addWidget(self.status, 1)
        actions.addWidget(self.assign_button)
        actions.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)


class BenchmarksTab(QWidget):
    benchmark_all_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = BenchmarkTableModel()
        self.table = _configured_table(self.model)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(self.model.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in {
            0: 90,
            2: 95,
            3: 95,
            4: 135,
            5: 110,
            6: 185,
            7: 65,
            8: 155,
        }.items():
            self.table.setColumnWidth(column, width)
        self.status = QLabel("Choose an assigned, connected slot to benchmark.")
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel benchmark")
        self.cancel_button.setVisible(False)
        self.batch_runs = QComboBox()
        self.batch_runs.addItems(("1", "3", "5"))
        self.benchmark_all_button = QPushButton("Benchmark All")
        self.benchmark_all_button.clicked.connect(
            lambda: self.benchmark_all_requested.emit(int(self.batch_runs.currentText()))
        )

        title = QLabel("Drive benchmarks and qualification")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        note = QLabel(
            "Benchmarks keep the existing safety boundary: explicit confirmation, "
            "a temporary file on a mounted filesystem, and cleanup on every exit path."
        )
        note.setStyleSheet("color: #b00020; font-weight: 600;")
        note.setWordWrap(True)

        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.progress)
        status_row.addWidget(self.cancel_button)
        status_row.addWidget(QLabel("Runs:"))
        status_row.addWidget(self.batch_runs)
        status_row.addWidget(self.benchmark_all_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addLayout(status_row)

    def set_operation_running(self, running: bool) -> None:
        self.batch_runs.setEnabled(not running)
        self.benchmark_all_button.setEnabled(not running)


class LogsTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        title = QLabel("Activity and diagnostic details")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        note = QLabel(
            "Meaningful hardware, slot, benchmark, saved-plan, and validation "
            "events appear here. Routine unchanged reconciliation scans are omitted."
        )
        note.setStyleSheet("color: #555;")
        note.setWordWrap(True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.clear_button = QPushButton("Clear visible log")
        self.clear_button.clicked.connect(self.output.clear)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.output, 1)
        layout.addLayout(actions)

    def add_entry(self, category: str, message: str) -> None:
        timestamp = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
        self.output.appendPlainText(f"[{timestamp}] {category}: {message}")


class BackendDeploymentTab(QWidget):
    backend_changed = Signal(str)
    probe_requested = Signal(int)
    probe_all_requested = Signal()
    performance_requested = Signal(int)
    performance_all_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._assessments: dict[str, BackendAssessment] = {}
        self._probe_running = False
        self._successful_probe_identities: dict[int, str] = {}
        self._device_identities: dict[int, str] = {}
        title = QLabel("RAID backend capability and deployment dry run")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        safety = QLabel(
            f"Production deployment remains PREVIEW ONLY. Version {__version__} may create, "
            "attach, benchmark, detach, and delete one temporary test VHDX only."
        )
        safety.setStyleSheet("color: #b00020; font-weight: 700;")
        safety.setWordWrap(True)
        self.backend_name = QLabel("Backend: —")
        self.backend_name.setVisible(False)
        self.backend_selector = QComboBox()
        self.status = QLabel("UNKNOWN")
        self.status.setStyleSheet("font-size: 16px; font-weight: 700;")
        self.explanation = QLabel("Waiting for saved-plan and inventory state.")
        self.explanation.setWordWrap(True)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Backend:"))
        status_row.addWidget(self.backend_selector)
        status_row.addWidget(QLabel("Deployment assessment:"))
        status_row.addWidget(self.status)
        status_row.addWidget(self.explanation, 1)

        self.vhdx_panel = QGroupBox("Temporary VHDX capability probe")
        self.vhdx_slot = QComboBox()
        self.probe_button = QPushButton("Probe VHDX Backend (8 GiB)")
        self.probe_all_button = QPushButton("Probe All 4")
        self.performance_button = QPushButton("Run VHDX Performance Test")
        self.performance_button.setEnabled(False)
        self.performance_runs = QComboBox()
        self.performance_runs.addItems(("1", "3", "5"))
        self.performance_all_button = QPushButton("Run Performance Test All")
        self.performance_all_button.setEnabled(False)
        self.batch_progress = QLabel("No batch operation running.")
        self.batch_progress.setWordWrap(True)
        self.batch_summary = QLabel("No batch results yet.")
        self.batch_summary.setWordWrap(True)
        self.batch_cancel_button = QPushButton("Cancel after current run")
        self.batch_cancel_button.setVisible(False)
        self.vhdx_path = QLabel("—")
        self.vhdx_attach = QLabel("Not run")
        self.vhdx_identity = QLabel("—")
        self.vhdx_can_pool = QLabel("Unknown")
        self.vhdx_size = QLabel("Unknown")
        self.vhdx_pool_reason = QLabel("Not run")
        self.vhdx_compatibility = QLabel("UNKNOWN")
        self.vhdx_probe_explanation = QLabel("Not run")
        self.vhdx_benchmark = QLabel("Not run")
        self.vhdx_overhead = QLabel("Not measured")
        self.vhdx_cleanup = QLabel("Not run")
        for label in (
            self.vhdx_path,
            self.vhdx_attach,
            self.vhdx_identity,
            self.vhdx_pool_reason,
            self.vhdx_probe_explanation,
            self.vhdx_benchmark,
            self.vhdx_overhead,
            self.vhdx_cleanup,
        ):
            label.setWordWrap(True)
        self.vhdx_slot.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.vhdx_slot.setMinimumContentsLength(16)
        controls = QHBoxLayout()
        controls.addWidget(self.probe_button)
        controls.addWidget(self.probe_all_button)
        controls.addStretch(1)
        performance_controls = QHBoxLayout()
        performance_controls.addWidget(self.performance_button)
        performance_controls.addWidget(QLabel("Runs:"))
        performance_controls.addWidget(self.performance_runs)
        performance_controls.addWidget(self.performance_all_button)
        performance_controls.addStretch(1)
        probe_layout = QVBoxLayout(self.vhdx_panel)
        probe_scope = QLabel(
            "Probe size: 8 GiB expandable VHDX on the selected filesystem. "
            "The capability probe inspects this temporary disk, then detaches and deletes it."
        )
        probe_scope.setWordWrap(True)
        probe_layout.addWidget(probe_scope)
        slot_form = QFormLayout()
        slot_form.addRow("USB slot", self.vhdx_slot)
        probe_layout.addLayout(slot_form)
        probe_layout.addLayout(controls)
        probe_layout.addLayout(performance_controls)
        probe_details = QWidget()
        form = QFormLayout(probe_details)
        form.setContentsMargins(0, 6, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Temporary VHDX path", self.vhdx_path)
        form.addRow("Attach status", self.vhdx_attach)
        form.addRow("Virtual disk identity", self.vhdx_identity)
        form.addRow("Virtual disk size", self.vhdx_size)
        form.addRow("CanPool", self.vhdx_can_pool)
        form.addRow("Windows rejection reason", self.vhdx_pool_reason)
        form.addRow("Probe assessment", self.vhdx_compatibility)
        form.addRow("Probe details", self.vhdx_probe_explanation)
        form.addRow("Direct vs VHDX benchmark", self.vhdx_benchmark)
        form.addRow("Measured overhead", self.vhdx_overhead)
        form.addRow("Cleanup status", self.vhdx_cleanup)
        for label in (
            self.vhdx_path, self.vhdx_identity, self.vhdx_pool_reason,
            self.vhdx_probe_explanation, self.vhdx_benchmark,
            self.vhdx_overhead, self.vhdx_cleanup,
        ):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.probe_details_scroll = QScrollArea()
        self.probe_details_scroll.setWidgetResizable(True)
        self.probe_details_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.probe_details_scroll.setWidget(probe_details)
        probe_layout.addWidget(self.probe_details_scroll, 1)

        self.batch_panel = QGroupBox("Batch results")
        self.batch_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        batch_layout = QVBoxLayout(self.batch_panel)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.batch_progress, 1)
        progress_row.addWidget(self.batch_cancel_button)
        batch_layout.addLayout(progress_row)
        batch_layout.addWidget(self.batch_summary)
        legend = QLabel("R / W / S = read / write / sustained write · speeds in MB/s")
        legend.setStyleSheet("color: #555;")
        legend.setWordWrap(True)
        batch_layout.addWidget(legend)
        self.performance_table = QTableWidget(0, 5)
        self.performance_table.setHorizontalHeaderLabels(
            ("Slot", "Runs", "Direct R / W / S", "VHDX R / W / S", "Avg overhead %")
        )
        self.performance_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.performance_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.performance_table.verticalHeader().setVisible(False)
        performance_header = self.performance_table.horizontalHeader()
        performance_header.setStretchLastSection(False)
        performance_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 3, 4):
            performance_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.performance_table.horizontalHeaderItem(4).setToolTip(
            "Arithmetic mean of individual run overhead percentages: read / write / sustained write."
        )
        self.performance_table.setVisible(False)
        batch_layout.addWidget(self.performance_table)
        for label in (self.batch_progress, self.batch_summary):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.backend_selector.currentTextChanged.connect(self._backend_selected)
        self.probe_button.clicked.connect(self._emit_probe)
        self.probe_all_button.clicked.connect(lambda: self.probe_all_requested.emit())
        self.performance_button.clicked.connect(self._emit_performance)
        self.performance_all_button.clicked.connect(
            lambda: self.performance_all_requested.emit(
                int(self.performance_runs.currentText())
            )
        )
        self.vhdx_slot.currentIndexChanged.connect(self._update_probe_controls)

        self.drive_table = QTableWidget(0, 11)
        self.drive_table.setHorizontalHeaderLabels(
            (
                "Slot",
                "Pair",
                "Physical disk",
                "Model",
                "Identity",
                "Capacity",
                "Bus / media",
                "Removable",
                "CanPool",
                "Partitions / filesystem",
                "Health / status",
            )
        )
        self.drive_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.drive_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.drive_table.verticalHeader().setVisible(False)
        header = self.drive_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)

        self.requirements = QTextBrowser()
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self.deployment_tabs = QTabWidget()
        self.deployment_tabs.addTab(self.requirements, "Requirements / blockers")
        self.deployment_tabs.addTab(self.drive_table, "Disk mapping")
        self.deployment_tabs.addTab(self.preview, "Deployment preview")
        self.result_splitter = QSplitter(Qt.Orientation.Vertical)
        self.result_splitter.setChildrenCollapsible(False)
        self.result_splitter.addWidget(self.batch_panel)
        self.result_splitter.addWidget(self.deployment_tabs)
        self.result_splitter.setStretchFactor(0, 0)
        self.result_splitter.setStretchFactor(1, 1)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(self.vhdx_panel)
        self.workspace_splitter.addWidget(self.result_splitter)
        self.workspace_splitter.setStretchFactor(0, 2)
        self.workspace_splitter.setStretchFactor(1, 3)
        self.workspace_splitter.setSizes((480, 720))

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(safety)
        layout.addLayout(status_row)
        layout.addWidget(self.workspace_splitter, 1)

    def set_assessments(
        self,
        assessments: dict[str, BackendAssessment],
        selected_name: str,
    ) -> None:
        self._assessments = dict(assessments)
        self.backend_selector.blockSignals(True)
        self.backend_selector.clear()
        self.backend_selector.addItems(assessments)
        self.backend_selector.setCurrentText(selected_name)
        self.backend_selector.blockSignals(False)
        self._backend_selected(self.backend_selector.currentText())

    def set_devices(self, devices: tuple[StorageDevice, ...] | list[StorageDevice]) -> None:
        previous = self.vhdx_slot.currentData()
        self._device_identities = {
            device.slot: _device_identity(device)
            for device in devices
            if device.slot is not None and device.is_connected
        }
        self.vhdx_slot.clear()
        for device in devices:
            if device.slot is not None and device.is_connected:
                label = f"Slot {device.slot} — {device.model or 'USB device'}"
                self.vhdx_slot.addItem(label, device.slot)
        index = self.vhdx_slot.findData(previous)
        if index >= 0:
            self.vhdx_slot.setCurrentIndex(index)
        self._update_probe_controls()

    def set_probe_running(self, running: bool) -> None:
        self._probe_running = running
        self.probe_button.setEnabled(not running)
        self.probe_all_button.setEnabled(not running and self.vhdx_slot.count() == 4)
        self.vhdx_slot.setEnabled(not running)
        self.backend_selector.setEnabled(not running)
        self.performance_runs.setEnabled(not running)
        self._update_probe_controls()

    def set_batch_progress(self, message: str) -> None:
        self.batch_progress.setText(message)

    def set_probe_batch_summary(self, lines: list[str]) -> None:
        self.batch_summary.setText("<br>".join(escape(line) for line in lines))

    def set_performance_batch_results(
        self,
        results: dict[int, tuple[int, VhdxBenchmarkComparison]],
    ) -> None:
        self.performance_table.setRowCount(len(results))
        for row, (slot, (run_count, item)) in enumerate(sorted(results.items())):
            values = (
                f"Slot {slot}",
                str(run_count),
                f"{item.direct_read_mbps:.1f} / {item.direct_write_mbps:.1f} / {item.direct_sustained_write_mbps:.1f}",
                f"{item.vhdx_read_mbps:.1f} / {item.vhdx_write_mbps:.1f} / {item.vhdx_sustained_write_mbps:.1f}",
                f"{item.read_overhead_percent:+.1f}% / {item.write_overhead_percent:+.1f}% / {item.sustained_write_overhead_percent:+.1f}%",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setToolTip(value)
                self.performance_table.setItem(row, column, cell)
        self.performance_table.setFixedHeight(
            self.performance_table.horizontalHeader().sizeHint().height()
            + self.performance_table.verticalHeader().defaultSectionSize() * max(1, len(results))
            + self.performance_table.frameWidth() * 2
        )
        self.performance_table.setVisible(bool(results))

    def set_probe_result(self, result: VhdxProbeResult) -> None:
        index = self.vhdx_slot.findData(result.slot)
        if index >= 0:
            self.vhdx_slot.setCurrentIndex(index)
        self.vhdx_path.setText(result.vhdx_path or "—")
        self.vhdx_path.setToolTip(result.vhdx_path or "—")
        self.vhdx_attach.setText(result.attach_status)
        disk = result.virtual_disk
        self.vhdx_identity.setText(disk.identity if disk else "—")
        self.vhdx_identity.setToolTip(disk.identity if disk else "—")
        self.vhdx_size.setText(
            (f"{disk.size_bytes / 1024**2:g} MiB"
             + (" (approximate)" if not disk.size_is_exact else ""))
            if disk and disk.size_bytes else "Unknown"
        )
        self.vhdx_pool_reason.setText(
            disk.pool_rejection_details if disk and disk.can_pool is False else "—"
        )
        self.vhdx_probe_explanation.setText(result.explanation)
        self.vhdx_can_pool.setText(
            "Yes" if disk and disk.can_pool is True
            else "No" if disk and disk.can_pool is False
            else "Unknown"
        )
        self.vhdx_compatibility.setText(result.status)
        if result.status == SUPPORTED and result.source_identity and not result.leftover_path:
            self._successful_probe_identities[result.slot] = result.source_identity
        elif result.slot in self._successful_probe_identities:
            del self._successful_probe_identities[result.slot]
        self._update_probe_controls()
        self.vhdx_cleanup.setText(result.cleanup_status)
        if result.leftover_path:
            self.vhdx_cleanup.setStyleSheet("color: #b00020; font-weight: 700;")
            self.vhdx_cleanup.setText(
                f"{result.cleanup_status}. Manual cleanup required: {result.leftover_path}"
            )
        else:
            self.vhdx_cleanup.setStyleSheet("")
        if result.benchmark:
            item = result.benchmark
            self.vhdx_benchmark.setText(
                f"Direct: read {item.direct_read_mbps:.1f}, write {item.direct_write_mbps:.1f}, "
                f"sustained {item.direct_sustained_write_mbps:.1f} MB/s | "
                f"VHDX: read {item.vhdx_read_mbps:.1f}, write {item.vhdx_write_mbps:.1f}, "
                f"sustained {item.vhdx_sustained_write_mbps:.1f} MB/s"
            )
            self.vhdx_overhead.setText(
                f"Read {item.read_overhead_percent:+.1f}% | "
                f"Write {item.write_overhead_percent:+.1f}% | "
                f"Sustained {item.sustained_write_overhead_percent:+.1f}%"
            )
        else:
            self.vhdx_benchmark.setText("Not run")
            self.vhdx_overhead.setText("Not measured")

    def _backend_selected(self, name: str) -> None:
        assessment = self._assessments.get(name)
        if assessment is not None:
            self.set_assessment(assessment)
        is_vhdx = name == "VHDX-backed Storage Spaces"
        self.vhdx_panel.setVisible(is_vhdx)
        self.batch_panel.setVisible(is_vhdx)
        self.backend_changed.emit(name)

    def _emit_probe(self) -> None:
        slot = self.vhdx_slot.currentData()
        if slot is not None:
            self.probe_requested.emit(int(slot))

    def _emit_performance(self) -> None:
        slot = self.vhdx_slot.currentData()
        if slot is not None and self.performance_button.isEnabled():
            self.performance_requested.emit(int(slot))

    def _update_probe_controls(self) -> None:
        slot = self.vhdx_slot.currentData()
        identity = self._device_identities.get(slot) if slot is not None else None
        probe_succeeded = (
            slot is not None
            and bool(identity)
            and self._successful_probe_identities.get(slot) == identity
        )
        self.performance_button.setEnabled(
            not self._probe_running and probe_succeeded
        )
        all_slots_ready = (
            len(self._device_identities) == 4
            and all(
                self._successful_probe_identities.get(slot) == identity
                for slot, identity in self._device_identities.items()
            )
        )
        self.probe_all_button.setEnabled(
            not self._probe_running and self.vhdx_slot.count() == 4
        )
        self.performance_all_button.setEnabled(
            not self._probe_running and all_slots_ready
        )

    def set_assessment(self, assessment: BackendAssessment) -> None:
        self.backend_name.setText(f"Backend: {assessment.backend_name}")
        colors = {
            "SUPPORTED": "#137333",
            "PARTIALLY SUPPORTED": "#9a6700",
            "UNSUPPORTED": "#b00020",
            "PROBE FAILED": "#b00020",
            "NEEDS ADMINISTRATOR": "#9a6700",
            "UNKNOWN": "#555",
        }
        self.status.setText(assessment.status)
        self.status.setStyleSheet(
            f"color: {colors.get(assessment.status, '#555')}; "
            "font-size: 16px; font-weight: 700;"
        )
        self.explanation.setText(assessment.explanation)
        self.drive_table.setRowCount(len(assessment.drives))
        for row, drive in enumerate(assessment.drives):
            values = (
                f"Slot {drive.slot}",
                drive.pair,
                drive.physical_disk,
                drive.model,
                drive.identity,
                _capacity_text(drive.capacity_bytes),
                f"{drive.bus_type} / {drive.media_type}",
                drive.removable,
                drive.can_pool,
                drive.partition_state,
                drive.health,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.drive_table.setItem(row, column, item)

        sections: list[str] = []
        if assessment.blockers:
            sections.append("<b>Safety blockers</b><ul>" + "".join(
                f"<li>{escape(item)}</li>" for item in assessment.blockers
            ) + "</ul>")
        if assessment.requirements:
            sections.append("<b>Requirements / unknowns</b><ul>" + "".join(
                f"<li>{escape(item)}</li>" for item in assessment.requirements
            ) + "</ul>")
        if not sections:
            sections.append(
                "No capability blockers were reported. A future release would still "
                "require a new safety review and explicit destructive confirmation."
            )
        sections.append("<b>Planned operations by drive</b>")
        for drive in assessment.drives:
            sections.append(
                f"<b>Slot {drive.slot}</b><ul>"
                + "".join(f"<li>{escape(item)}</li>" for item in drive.operations)
                + "</ul>"
            )
        self.requirements.setHtml("".join(sections))
        self.preview.setPlainText(assessment.dry_run_text)


def _capacity_text(value: int | None) -> str:
    return f"{value / 1024**3:.1f} GiB" if value is not None else "Unknown"


def _device_identity(device: StorageDevice) -> str:
    return (
        device.storage_unique_id
        or device.container_id
        or device.pnp_device_id
        or device.device_path
        or ""
    )
