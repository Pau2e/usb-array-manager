from __future__ import annotations

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
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
