from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.raid_plan import PlannerDrive, Raid10Estimate
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.raid10_planner import (
    Raid10PlanError,
    estimate_raid10,
    suggest_mirror_pairs,
)


class Raid10PlannerDialog(QDialog):
    def __init__(
        self,
        devices: tuple[StorageDevice, ...],
        benchmark_results: dict[int, BenchmarkResult],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("RAID10 Array Planner — simulation only")
        self.resize(1_080, 780)
        self._drives = _planner_drives(devices, benchmark_results)
        self._checkboxes: dict[int, QCheckBox] = {}
        self._pair_combos: list[tuple[QComboBox, QComboBox]] = []

        heading = QLabel("RAID10 Array Planner — READ-ONLY SIMULATION")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        safety = QLabel(
            "This planner calculates estimates only. It does not create arrays, "
            "format drives, or change any disk."
        )
        safety.setStyleSheet("color: #b00020; font-weight: 600;")

        selection_group = QGroupBox("1. Select an even number of connected slots")
        selection_layout = QVBoxLayout(selection_group)
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
            checkbox.setEnabled(drive.is_connected)
            checkbox.setChecked(drive.is_connected)
            checkbox.stateChanged.connect(self._selection_changed)
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
        pair_layout = QVBoxLayout(pair_group)
        pair_actions = QHBoxLayout()
        self._selection_status = QLabel()
        self._suggest_button = QPushButton("Suggest best pairing")
        self._suggest_button.clicked.connect(self._suggest_pairs)
        pair_actions.addWidget(self._selection_status, 1)
        pair_actions.addWidget(self._suggest_button)
        pair_layout.addLayout(pair_actions)
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
        self._pair_table.horizontalHeader().setStretchLastSection(True)
        result_layout.addWidget(self._pair_table)
        self._array_summary = QLabel("Select drives and define pairs to calculate estimates.")
        self._array_summary.setTextFormat(Qt.TextFormat.RichText)
        self._array_summary.setWordWrap(True)
        result_layout.addWidget(self._array_summary)

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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(safety)
        layout.addWidget(selection_group)
        layout.addWidget(pair_group)
        layout.addWidget(result_group, 1)
        layout.addWidget(warnings_group)
        layout.addWidget(model_group)
        layout.addWidget(buttons)

        self._selection_changed()

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
            self._clear_results()
            self._show_availability_warnings()
            return

        self._selection_status.setText(
            f"Selected: {len(selected)} drive(s), {len(selected) // 2} mirror pair(s)."
        )
        self._suggest_pairs()

    def _suggest_pairs(self) -> None:
        try:
            pairs = suggest_mirror_pairs(self._selected_drives())
        except Raid10PlanError as error:
            QMessageBox.warning(self, "Invalid drive selection", str(error))
            return
        self._rebuild_pair_rows(pairs)
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
            row_layout.addWidget(first_combo)
            row_layout.addWidget(QLabel("+"))
            row_layout.addWidget(second_combo)
            row_layout.addStretch(1)
            self._pairs_layout.addWidget(row)
            self._pair_combos.append((first_combo, second_combo))

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

    def _clear_results(self) -> None:
        self._pair_table.setRowCount(0)
        self._array_summary.setText(
            "Select an even number of connected drives to calculate estimates."
        )

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
