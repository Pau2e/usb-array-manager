from __future__ import annotations

from html import escape

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.models.raid_backend import BackendAssessment
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
            7: 155,
        }.items():
            self.table.setColumnWidth(column, width)
        self.status = QLabel("Choose an assigned, connected slot to benchmark.")
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel benchmark")
        self.cancel_button.setVisible(False)

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

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addLayout(status_row)


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
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        title = QLabel("RAID backend capability and deployment dry run")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        safety = QLabel(
            "PREVIEW ONLY — NOT EXECUTED. Version 0.6 can inspect capabilities and "
            "describe future operations, but it cannot modify any disk."
        )
        safety.setStyleSheet("color: #b00020; font-weight: 700;")
        safety.setWordWrap(True)
        self.backend_name = QLabel("Backend: —")
        self.status = QLabel("UNKNOWN")
        self.status.setStyleSheet("font-size: 16px; font-weight: 700;")
        self.explanation = QLabel("Waiting for saved-plan and inventory state.")
        self.explanation.setWordWrap(True)

        status_row = QHBoxLayout()
        status_row.addWidget(self.backend_name)
        status_row.addWidget(self.status)
        status_row.addWidget(self.explanation, 1)

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
        header.setStretchLastSection(True)

        self.requirements = QTextBrowser()
        self.requirements.setMinimumWidth(340)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        detail_splitter = QSplitter()
        detail_splitter.addWidget(self.requirements)
        detail_splitter.addWidget(self.preview)
        detail_splitter.setStretchFactor(0, 1)
        detail_splitter.setStretchFactor(1, 2)
        detail_splitter.setSizes((390, 780))

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(safety)
        layout.addLayout(status_row)
        layout.addWidget(self.drive_table, 1)
        layout.addWidget(detail_splitter, 1)

    def set_assessment(self, assessment: BackendAssessment) -> None:
        self.backend_name.setText(f"Backend: {assessment.backend_name}")
        colors = {
            "SUPPORTED": "#137333",
            "PARTIALLY SUPPORTED": "#9a6700",
            "UNSUPPORTED": "#b00020",
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
