from __future__ import annotations

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.raid_failure import Raid10FailureSimulation
from usb_array_manager.models.raid_plan import PlannerDrive, Raid10Estimate
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.raid10_plan_store import (
    Raid10PlanStore,
    Raid10PlanStoreError,
)
from usb_array_manager.services.raid10_planner import (
    Raid10PlanError,
    estimate_raid10,
    suggest_mirror_pairing,
)
from usb_array_manager.services.raid10_failure_simulator import (
    ARRAY_DEGRADED,
    ARRAY_FAILED,
    FailureSimulationError,
    simulate_failures,
)
from usb_array_manager.services.raid10_readiness import (
    NOT_READY,
    READY,
    readiness_summary,
    validate_raid10_readiness,
)


class Raid10PlannerDialog(QDialog):
    plan_saved = Signal(object)
    diagnostic = Signal(str, str)

    def __init__(
        self,
        devices: tuple[StorageDevice, ...],
        benchmark_results: dict[int, BenchmarkResult],
        parent: QWidget | None = None,
        *,
        plan_store: Raid10PlanStore | None = None,
        saved_plan: SavedRaid10Plan | None = None,
        ambiguous_slots: set[int] | frozenset[int] = frozenset(),
        embedded: bool = False,
        initial_pairs: tuple[tuple[int, int], ...] | None = None,
        initial_failed_slots: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("RAID10 Array Planner — simulation only")
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setMinimumSize(1_100, 650)
        self.resize(1_500, 800)
        self._drives = _planner_drives(devices, benchmark_results)
        self._devices = devices
        self._benchmark_results = benchmark_results
        self._plan_store = plan_store or Raid10PlanStore()
        self._saved_plan = saved_plan
        self._ambiguous_slots = frozenset(ambiguous_slots)
        self._checkboxes: dict[int, QCheckBox] = {}
        self._pair_combos: list[tuple[QComboBox, QComboBox]] = []
        self._current_estimate: Raid10Estimate | None = None
        self._failure_checkboxes: dict[int, QCheckBox] = {}
        self._initial_failed_slots = frozenset(initial_failed_slots)

        heading = QLabel("RAID10 Array Planner — READ-ONLY SIMULATION")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        safety = QLabel(
            "This planner calculates estimates only. It does not create arrays, "
            "format drives, or change any disk."
        )
        safety.setStyleSheet("color: #b00020; font-weight: 600;")

        selection_group = QGroupBox("1. Select an even number of connected slots")
        self._selection_group = selection_group
        selection_group.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        selection_layout = QVBoxLayout(selection_group)
        selection_layout.setContentsMargins(10, 8, 10, 8)
        selection_layout.setSpacing(3)
        for slot in sorted(self._drives):
            drive = self._drives[slot]
            state = "Connected" if drive.is_connected else "Disconnected"
            benchmark = (
                f"{drive.sustained_write_mbps:.1f} MB/s sustained write"
                if drive.sustained_write_mbps is not None
                else "NOT TESTED"
            )
            checkbox = QCheckBox(
                f"Slot {slot} — {drive.model} — {_capacity(drive.capacity_bytes)} "
                f"— {state} — {benchmark}"
            )
            initial_slots = (
                {member for pair in initial_pairs for member in pair}
                if initial_pairs is not None
                else set(saved_plan.slots) if saved_plan is not None else set()
            )
            checkbox.setChecked(slot in initial_slots if initial_slots else drive.is_connected)
            checkbox.setEnabled(drive.is_connected)
            checkbox.stateChanged.connect(self._selection_changed)
            checkbox.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Fixed,
            )
            self._checkboxes[slot] = checkbox
            selection_layout.addWidget(checkbox)

        unassigned_count = sum(
            device.is_connected and device.slot is None for device in devices
        )
        if unassigned_count:
            note = QLabel(
                f"{unassigned_count} connected drive(s) are unassigned and cannot be "
                "used until a logical slot is assigned."
            )
            note.setStyleSheet("color: #9a6700;")
            selection_layout.addWidget(note)

        pair_group = QGroupBox("2. Define mirror pairs")
        pair_group.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        pair_layout = QVBoxLayout(pair_group)
        pair_actions = QHBoxLayout()
        self._selection_status = QLabel()
        self._suggest_button = QPushButton("Suggest best pairing")
        self._suggest_button.clicked.connect(self._suggest_pairs)
        pair_actions.addWidget(self._selection_status, 1)
        pair_actions.addWidget(self._suggest_button)
        pair_layout.addLayout(pair_actions)
        self._suggestion_reason = QLabel()
        self._suggestion_reason.setWordWrap(True)
        self._suggestion_reason.setStyleSheet("color: #2457a6;")
        pair_layout.addWidget(self._suggestion_reason)
        self._pairs_container = QWidget()
        self._pairs_layout = QVBoxLayout(self._pairs_container)
        self._pairs_layout.setContentsMargins(0, 0, 0, 0)
        pair_layout.addWidget(self._pairs_container)

        calculate_button = QPushButton("Calculate RAID10 estimates")
        calculate_button.clicked.connect(self._calculate)
        pair_layout.addWidget(calculate_button, alignment=Qt.AlignmentFlag.AlignRight)

        result_group = QGroupBox("3. ESTIMATED results — not guaranteed performance")
        result_layout = QVBoxLayout(result_group)
        self._pair_table = QTableWidget(0, 7)
        self._pair_table.setHorizontalHeaderLabels(
            (
                "Pair",
                "Members",
                "Usable capacity",
                "EST. sustained write",
                "Conservative read",
                "Theoretical max read",
                "Bottleneck",
            )
        )
        self._pair_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._pair_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._pair_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._pair_table.horizontalHeader().setStretchLastSection(True)
        self._pair_table.verticalHeader().setVisible(False)
        self._pair_table.verticalHeader().setDefaultSectionSize(30)
        self._pair_table.setMinimumHeight(150)
        self._pair_table.setStyleSheet(
            "QTableWidget { font-size: 10pt; } QHeaderView::section { font-size: 10pt; }"
        )
        result_layout.addWidget(self._pair_table)
        self._array_summary = QLabel("Select drives and define pairs to calculate estimates.")
        self._array_summary.setTextFormat(Qt.TextFormat.RichText)
        self._array_summary.setWordWrap(True)
        result_layout.addWidget(self._array_summary)

        plan_actions = QHBoxLayout()
        self._save_plan_button = QPushButton("Save this plan")
        self._save_plan_button.clicked.connect(self._save_plan)
        self._export_plan_button = QPushButton("Export summary…")
        self._export_plan_button.clicked.connect(self._export_summary)
        plan_actions.addStretch(1)
        plan_actions.addWidget(self._save_plan_button)
        plan_actions.addWidget(self._export_plan_button)
        result_layout.addLayout(plan_actions)

        readiness_group = QGroupBox("Saved-plan readiness")
        readiness_layout = QVBoxLayout(readiness_group)
        self._readiness_status = QLabel("No saved RAID10 plan.")
        self._readiness_status.setWordWrap(True)
        self._readiness_details = QTextBrowser()
        self._readiness_details.setMaximumHeight(130)
        readiness_layout.addWidget(self._readiness_status)
        readiness_layout.addWidget(self._readiness_details)

        failure_group = QGroupBox("4. RAID10 failure and degraded-mode simulator")
        failure_layout = QVBoxLayout(failure_group)
        failure_actions = QHBoxLayout()
        failure_note = QLabel(
            "Mark slots as Simulated failed. This changes the simulation only."
        )
        failure_note.setStyleSheet("color: #b00020; font-weight: 600;")
        self._reset_failures_button = QPushButton("Reset simulated failures")
        self._reset_failures_button.clicked.connect(self._reset_failures)
        failure_actions.addWidget(failure_note, 1)
        failure_actions.addWidget(self._reset_failures_button)
        failure_layout.addLayout(failure_actions)
        self._failure_controls = QHBoxLayout()
        failure_layout.addLayout(self._failure_controls)

        self._failure_table = QTableWidget(0, 5)
        self._failure_table.setHorizontalHeaderLabels(
            ("Pair", "Members", "Status", "Working slots", "Current read ESTIMATE")
        )
        self._failure_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._failure_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._configure_failure_table_columns()
        self._failure_table.verticalHeader().setVisible(False)
        self._failure_table.verticalHeader().setDefaultSectionSize(34)
        self._failure_table.setMinimumHeight(150)
        self._failure_table.setStyleSheet(
            "QTableWidget { font-size: 10pt; } QHeaderView::section { font-size: 10pt; }"
        )
        failure_layout.addWidget(self._failure_table)
        self._array_health = QLabel("Calculate a valid pairing to start the simulator.")
        self._array_health.setWordWrap(True)
        failure_layout.addWidget(self._array_health)
        self._failure_details = QTextBrowser()
        self._failure_details.setMaximumHeight(125)
        failure_layout.addWidget(self._failure_details)

        warnings_group = QGroupBox("Warnings")
        warnings_layout = QVBoxLayout(warnings_group)
        self._warnings = QTextBrowser()
        self._warnings.setMaximumHeight(125)
        warnings_layout.addWidget(self._warnings)

        model_group = QGroupBox("Performance model and assumptions")
        model_layout = QVBoxLayout(model_group)
        model_text = QTextBrowser()
        model_text.setMaximumHeight(145)
        model_text.setHtml(
            "<ul>"
            "<li>Writes to a mirror pair must reach both drives, so the slower "
            "member can limit writes.</li>"
            "<li>Conservative read uses the slower member. The theoretical maximum "
            "assumes reads can be distributed across both members perfectly.</li>"
            "<li>Mirror pairs are assumed to be striped in parallel. Actual results "
            "may be lower.</li>"
            "<li>USB topology, shared hub bandwidth, controller overhead, filesystem, "
            "workload, and RAID implementation are not modeled.</li>"
            "</ul>"
        )
        model_layout.addWidget(model_text)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(selection_group)
        left_layout.addWidget(pair_group)
        left_layout.addStretch(1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(result_group, 1)
        right_layout.addWidget(warnings_group)
        right_layout.addWidget(readiness_group)
        right_layout.addWidget(model_group)

        self._content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._content_splitter.addWidget(left_panel)
        self._content_splitter.addWidget(right_panel)
        self._content_splitter.setStretchFactor(0, 1)
        self._content_splitter.setStretchFactor(1, 1)
        self._content_splitter.setSizes((760, 700))

        self.planner_page = QWidget()
        planner_layout = QVBoxLayout(self.planner_page)
        planner_layout.addWidget(heading)
        planner_layout.addWidget(safety)
        planner_layout.addWidget(self._content_splitter, 1)

        self.failure_page = QWidget()
        failure_heading = QLabel("RAID10 Failure Simulator — SIMULATION ONLY")
        failure_heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        failure_safety = QLabel(
            "Failure selections affect the shared planner estimate only. "
            "They never change a disk or saved slot."
        )
        failure_safety.setStyleSheet("color: #b00020; font-weight: 600;")
        failure_layout_page = QVBoxLayout(self.failure_page)
        failure_layout_page.addWidget(failure_heading)
        failure_layout_page.addWidget(failure_safety)
        failure_layout_page.addWidget(failure_group, 1)

        if not embedded:
            tabs = QTabWidget()
            tabs.addTab(self.planner_page, "RAID10 Planner")
            tabs.addTab(self.failure_page, "Failure Simulator")
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            layout = QVBoxLayout(self)
            layout.addWidget(tabs, 1)
            layout.addWidget(buttons)

        loaded_pairs = initial_pairs or (
            saved_plan.pairs if saved_plan is not None else None
        )
        if loaded_pairs is not None and all(
            slot in self._drives for pair in loaded_pairs for slot in pair
        ):
            pairs = [
                (self._drives[first], self._drives[second])
                for first, second in loaded_pairs
            ]
            self._selection_status.setText(
                f"Loaded plan state: {len(loaded_pairs) * 2} drive(s), "
                f"{len(loaded_pairs)} mirror pair(s)."
            )
            self._rebuild_pair_rows(pairs)
            self._suggestion_reason.setText(
                "Loaded the shared logical-slot layout."
            )
            self._calculate()
        else:
            self._selection_changed()
        self._show_saved_plan_readiness()

    def current_pairs(self) -> tuple[tuple[int, int], ...] | None:
        if self._current_estimate is None:
            return None
        return tuple(
            (pair.first.slot, pair.second.slot) for pair in self._current_estimate.pairs
        )

    def simulated_failed_slots(self) -> frozenset[int]:
        return frozenset(
            slot
            for slot, checkbox in self._failure_checkboxes.items()
            if checkbox.isChecked()
        )

    def _selected_drives(self) -> list[PlannerDrive]:
        return [
            self._drives[slot]
            for slot, checkbox in self._checkboxes.items()
            if checkbox.isChecked()
        ]

    def _selection_changed(self) -> None:
        selected = self._selected_drives()
        valid = len(selected) >= 2 and len(selected) % 2 == 0
        self._suggest_button.setEnabled(valid)
        if not valid:
            self._selection_status.setText(
                f"Selected: {len(selected)} — choose an even number of at least 2 drives."
            )
            self._clear_pair_rows()
            self._suggestion_reason.clear()
            self._clear_results()
            self._show_availability_warnings()
            return

        self._selection_status.setText(
            f"Selected: {len(selected)} drive(s), {len(selected) // 2} mirror pair(s)."
        )
        self._suggest_pairs()

    def _suggest_pairs(self) -> None:
        try:
            suggestion = suggest_mirror_pairing(self._selected_drives())
        except Raid10PlanError as error:
            QMessageBox.warning(self, "Invalid drive selection", str(error))
            return
        self._rebuild_pair_rows(list(suggestion.pairs))
        self._suggestion_reason.setText(suggestion.explanation)
        self._calculate()

    def _rebuild_pair_rows(
        self,
        pairs: list[tuple[PlannerDrive, PlannerDrive]],
    ) -> None:
        self._clear_pair_rows()
        selected_slots = sorted(drive.slot for drive in self._selected_drives())
        for index, (first, second) in enumerate(pairs):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(QLabel(_pair_name(index)))
            first_combo = _slot_combo(selected_slots, first.slot)
            second_combo = _slot_combo(selected_slots, second.slot)
            first_combo.currentIndexChanged.connect(self._manual_pairing_changed)
            second_combo.currentIndexChanged.connect(self._manual_pairing_changed)
            row_layout.addWidget(first_combo)
            row_layout.addWidget(QLabel("+"))
            row_layout.addWidget(second_combo)
            row_layout.addStretch(1)
            self._pairs_layout.addWidget(row)
            self._pair_combos.append((first_combo, second_combo))

    def _manual_pairing_changed(self) -> None:
        self._suggestion_reason.setText(
            "Manual override selected. Click Calculate RAID10 estimates to validate it."
        )
        self._clear_failure_simulator()

    def _clear_pair_rows(self) -> None:
        self._pair_combos.clear()
        while self._pairs_layout.count():
            item = self._pairs_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _calculate(self) -> None:
        selected = self._selected_drives()
        if len(selected) < 2 or len(selected) % 2:
            return

        chosen_slots = [
            combo.currentData()
            for pair in self._pair_combos
            for combo in pair
        ]
        expected_slots = {drive.slot for drive in selected}
        if len(chosen_slots) != len(selected) or set(chosen_slots) != expected_slots:
            QMessageBox.warning(
                self,
                "Invalid mirror pairs",
                "Use every selected slot exactly once. A slot cannot be in two pairs.",
            )
            return

        pairs = [
            (self._drives[first.currentData()], self._drives[second.currentData()])
            for first, second in self._pair_combos
        ]
        try:
            estimate = estimate_raid10(pairs)
        except Raid10PlanError as error:
            QMessageBox.warning(self, "Invalid RAID10 plan", str(error))
            return
        self._show_estimate(estimate)

    def _show_estimate(self, estimate: Raid10Estimate) -> None:
        self._pair_table.setRowCount(len(estimate.pairs))
        for row, pair in enumerate(estimate.pairs):
            values = (
                pair.label,
                f"Slot {pair.first.slot} + Slot {pair.second.slot}",
                _capacity(pair.usable_capacity_bytes),
                _speed(pair.estimated_write_mbps),
                _speed(pair.conservative_read_mbps),
                _speed(pair.theoretical_max_read_mbps),
                pair.bottleneck,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column not in (1, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._pair_table.setItem(row, column, item)
        self._pair_table.resizeColumnsToContents()

        self._array_summary.setText(
            "<b>Complete array estimates</b><br>"
            f"Total raw capacity: {_capacity(estimate.total_raw_capacity_bytes)} &nbsp; | &nbsp; "
            f"Total usable capacity: {_capacity(estimate.total_usable_capacity_bytes)} &nbsp; | &nbsp; "
            f"Efficiency: {_percent(estimate.capacity_efficiency_percent)}<br>"
            f"EST. sustained write: {_speed(estimate.estimated_sustained_write_mbps)} &nbsp; | &nbsp; "
            f"Conservative EST. read: {_speed(estimate.conservative_read_mbps)} &nbsp; | &nbsp; "
            f"Theoretical maximum read: {_speed(estimate.theoretical_max_read_mbps)}"
        )

        warnings = list(estimate.warnings)
        warnings.extend(self._availability_warnings())
        if len(estimate.pairs) == 1:
            warnings.append(
                "Only one mirror pair is selected; conventional RAID10 normally uses "
                "at least four drives (two mirror pairs)."
            )
        self._warnings.setHtml(
            "<ul>" + "".join(f"<li>{warning}</li>" for warning in warnings) + "</ul>"
            if warnings
            else "No planner warnings for the selected layout."
        )
        self._set_failure_estimate(estimate)

    def _save_plan(self) -> None:
        if self._current_estimate is None:
            QMessageBox.warning(
                self,
                "No valid plan",
                "Calculate a valid mirror layout before saving it.",
            )
            return
        pairs = tuple(
            (pair.first.slot, pair.second.slot) for pair in self._current_estimate.pairs
        )
        try:
            self._saved_plan = self._plan_store.save(pairs)
        except (Raid10PlanStoreError, ValueError) as error:
            QMessageBox.warning(self, "Could not save RAID10 plan", str(error))
            return
        self._show_saved_plan_readiness()
        self.plan_saved.emit(self._saved_plan)
        self.diagnostic.emit(
            "Saved plan",
            f"Saved {len(pairs)} mirror pair(s) to {self._plan_store.path}; "
            f"{self._readiness_status.text()}",
        )
        QMessageBox.information(
            self,
            "RAID10 plan saved",
            "The logical-slot layout was saved locally. No USB drive was changed.\n\n"
            f"{self._plan_store.path}",
        )

    def _show_saved_plan_readiness(self) -> None:
        if self._saved_plan is None:
            self._readiness_status.setStyleSheet("")
            self._readiness_status.setText("No saved RAID10 plan.")
            self._readiness_details.setText(
                "Save a calculated layout to run the persistent preflight checks."
            )
            self._export_plan_button.setEnabled(False)
            return
        readiness = validate_raid10_readiness(
            self._saved_plan,
            self._devices,
            self._benchmark_results,
            ambiguous_slots=self._ambiguous_slots,
        )
        color = "#137333" if readiness.status == READY else "#9a6700"
        if readiness.status == NOT_READY:
            color = "#b00020"
        self._readiness_status.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: 700;"
        )
        self._readiness_status.setText(
            f"Saved RAID10 plan: {readiness.status} — benchmark freshness: 30 days"
        )
        if readiness.issues:
            self._readiness_details.setHtml(
                "<ul>"
                + "".join(
                    f"<li><b>{escape(issue.severity)}:</b> "
                    f"{escape(issue.message)}</li>"
                    for issue in readiness.issues
                )
                + "</ul>"
            )
        else:
            self._readiness_details.setText(
                "All strict preflight checks passed for the saved logical-slot layout."
            )
        self._export_plan_button.setEnabled(True)

    def _export_summary(self) -> None:
        if self._saved_plan is None:
            return
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export read-only RAID10 plan summary",
            str(self._plan_store.path.with_name("raid10_plan_summary.txt")),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        readiness = validate_raid10_readiness(
            self._saved_plan,
            self._devices,
            self._benchmark_results,
            ambiguous_slots=self._ambiguous_slots,
        )
        try:
            Path(path).write_text(
                readiness_summary(self._saved_plan, readiness),
                encoding="utf-8",
            )
        except OSError as error:
            QMessageBox.warning(self, "Could not export plan summary", str(error))
            return
        QMessageBox.information(
            self,
            "Plan summary exported",
            f"Saved read-only documentation to:\n{path}",
        )

    def _clear_results(self) -> None:
        self._pair_table.setRowCount(0)
        self._array_summary.setText(
            "Select an even number of connected drives to calculate estimates."
        )
        self._clear_failure_simulator()

    def _set_failure_estimate(self, estimate: Raid10Estimate) -> None:
        previous_failures = {
            slot
            for slot, checkbox in self._failure_checkboxes.items()
            if checkbox.isChecked()
        }
        previous_failures.update(self._initial_failed_slots)
        self._initial_failed_slots = frozenset()
        self._current_estimate = estimate
        self._clear_failure_controls()
        selected_slots = sorted(
            drive.slot
            for pair in estimate.pairs
            for drive in (pair.first, pair.second)
        )
        for slot in selected_slots:
            checkbox = QCheckBox(f"Slot {slot} — Simulated failed")
            checkbox.setChecked(slot in previous_failures)
            checkbox.stateChanged.connect(self._simulate_failures)
            self._failure_checkboxes[slot] = checkbox
            self._failure_controls.addWidget(checkbox)
        self._failure_controls.addStretch(1)
        self._reset_failures_button.setEnabled(True)
        self._simulate_failures()

    def _simulate_failures(self) -> None:
        if self._current_estimate is None:
            return
        failed_slots = {
            slot
            for slot, checkbox in self._failure_checkboxes.items()
            if checkbox.isChecked()
        }
        try:
            simulation = simulate_failures(self._current_estimate, failed_slots)
        except FailureSimulationError as error:
            QMessageBox.warning(self, "Invalid failure simulation", str(error))
            return
        self._show_failure_simulation(simulation)

    def _show_failure_simulation(
        self,
        simulation: Raid10FailureSimulation,
    ) -> None:
        self._failure_table.setRowCount(len(simulation.pair_states))
        for row, pair in enumerate(simulation.pair_states):
            values = (
                pair.label,
                " + ".join(f"Slot {slot}" for slot in pair.member_slots),
                pair.status,
                _slots(pair.working_slots),
                _read_range(
                    pair.conservative_read_mbps,
                    pair.theoretical_max_read_mbps,
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._failure_table.setItem(row, column, item)
        self._configure_failure_table_columns()

        color = "#137333"
        if simulation.array_status == ARRAY_DEGRADED:
            color = "#9a6700"
        elif simulation.array_status == ARRAY_FAILED:
            color = "#b00020"
        self._array_health.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: 700;"
        )
        self._array_health.setText(
            f"Complete RAID10 status: {simulation.array_status}"
        )

        self._failure_details.setHtml(
            f"<b>Why:</b> {simulation.explanation}<br>"
            f"<b>Remaining working drives:</b> "
            f"{_slots(simulation.remaining_working_slots)}<br>"
            f"<b>Lost redundancy:</b> "
            f"{_names(simulation.lost_redundancy_pairs)}<br>"
            f"<b>Mirror pairs at risk:</b> {_names(simulation.at_risk_pairs)}<br>"
            f"<b>Can another failure be tolerated?</b> "
            f"{simulation.additional_failure_tolerance}<br>"
            f"<b>Current read performance:</b> "
            f"{_read_range(simulation.conservative_read_mbps, simulation.theoretical_max_read_mbps)}"
        )

    def _configure_failure_table_columns(self) -> None:
        header = self._failure_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(self._failure_table.columnCount() - 1):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        header.setSectionResizeMode(
            self._failure_table.columnCount() - 1,
            QHeaderView.ResizeMode.Stretch,
        )

    def _reset_failures(self) -> None:
        for checkbox in self._failure_checkboxes.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
        self._simulate_failures()

    def _clear_failure_controls(self) -> None:
        self._failure_checkboxes.clear()
        while self._failure_controls.count():
            item = self._failure_controls.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _clear_failure_simulator(self) -> None:
        self._current_estimate = None
        self._clear_failure_controls()
        self._failure_table.setRowCount(0)
        self._array_health.setStyleSheet("")
        self._array_health.setText(
            "Calculate a valid pairing to start the simulator."
        )
        self._failure_details.clear()
        self._reset_failures_button.setEnabled(False)

    def _show_availability_warnings(self) -> None:
        warnings = self._availability_warnings()
        self._warnings.setHtml(
            "<ul>" + "".join(f"<li>{warning}</li>" for warning in warnings) + "</ul>"
            if warnings
            else "No availability warnings."
        )

    def _availability_warnings(self) -> list[str]:
        return [
            f"Slot {drive.slot} is disconnected and cannot be selected."
            for drive in self._drives.values()
            if not drive.is_connected
        ]


def _planner_drives(
    devices: tuple[StorageDevice, ...],
    results: dict[int, BenchmarkResult],
) -> dict[int, PlannerDrive]:
    drives: dict[int, PlannerDrive] = {}
    for device in devices:
        if device.slot is None:
            continue
        result = results.get(device.slot)
        drives[device.slot] = PlannerDrive(
            slot=device.slot,
            model=device.model or "Unknown USB storage device",
            capacity_bytes=device.capacity_bytes,
            sequential_read_mbps=(
                result.sequential_read_mbps if result is not None else None
            ),
            sustained_write_mbps=(
                result.sustained_write_mbps if result is not None else None
            ),
            is_connected=device.is_connected,
        )
    return drives


def _slot_combo(slots: list[int], selected_slot: int) -> QComboBox:
    combo = QComboBox()
    for slot in slots:
        combo.addItem(f"Slot {slot}", slot)
    combo.setCurrentIndex(slots.index(selected_slot))
    return combo


def _pair_name(index: int) -> str:
    return f"Pair {chr(ord('A') + index)}" if index < 26 else f"Pair {index + 1}"


def _capacity(value: int | None) -> str:
    return f"{value / 1024**3:.1f} GiB" if value is not None else "Unavailable"


def _speed(value: float | None) -> str:
    return f"{value:.1f} MB/s ESTIMATE" if value is not None else "Unavailable"


def _percent(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "Unavailable"


def _slots(slots: tuple[int, ...]) -> str:
    return ", ".join(f"Slot {slot}" for slot in slots) or "None"


def _names(names: tuple[str, ...]) -> str:
    return ", ".join(names) or "None"


def _read_range(conservative: float | None, theoretical: float | None) -> str:
    if conservative is None or theoretical is None:
        return "Unavailable"
    if abs(conservative - theoretical) < 1e-9:
        return f"{conservative:.1f} MB/s ESTIMATE"
    return (
        f"{conservative:.1f} MB/s conservative / "
        f"{theoretical:.1f} MB/s theoretical max"
    )
