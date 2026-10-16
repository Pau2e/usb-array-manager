from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from usb_array_manager.services.host_preparation import PreparationConfirmation
from usb_array_manager.services.host_health import (
    DIRTY_FORMAT_REVIEW,
    dirty_format_exception,
)
from usb_array_manager.services.production_planning import gib


def member_details(device, status="", reason="") -> str:
    if device is None:
        return "No current device associated with this reserved slot."
    volume = device.host_volumes[0] if len(device.host_volumes) == 1 else None
    return "\n".join(
        (
            f"Slot {device.slot}: {status}",
            reason,
            f"Model: {device.model}",
            f"Serial: {device.serial_number}",
            f"Persistent storage identity: {device.storage_unique_id}",
            f"USB/PnP identity: {device.usb_device_id} / {device.pnp_device_id}",
            f"Container: {device.container_id}",
            f"Physical disk/device (current addresses only): {device.disk_number} / {device.device_path}",
            f"Current letter (display only): {', '.join(device.drive_letters)}",
            f"Volume GUID: {volume.identity if volume else 'UNPROVEN'}",
            f"Filesystem: {volume.filesystem if volume else 'Unknown'}",
            f"Capacity: physical {gib(device.capacity_bytes)} / host {gib(volume.capacity_bytes) if volume else 'Unknown'}",
            f"Free: {gib(volume.free_bytes) if volume else 'Unknown'}",
            f"Health: physical {device.health_status} / volume {volume.health_status if volume else 'Unknown'}",
            f"System/boot disk: {device.is_system_disk} / {device.is_boot_disk}",
            f"System/boot volume: {volume.is_system if volume else 'Unknown'} / {volume.is_boot if volume else 'Unknown'}",
            (
                "Application/Windows/state exclusion: initial check passed; checked again before formatting."
                if status in {"READY FOR PREPARATION", "PREPARED", DIRTY_FORMAT_REVIEW}
                else "Application/Windows/state exclusion: NOT YET PROVEN; identity/other prerequisites did not pass. System/boot flags alone do not prove this exclusion."
            ),
        )
    )


class HostConfirmationDialog(QDialog):
    """No destructive side effect; accepted data is checked again by the service."""

    def __init__(self, target, parent=None):
        super().__init__(parent)
        self.target = target
        self.setWindowTitle(f"Destructive host preparation — Slot {target.slot}")
        self.resize(700, 600)
        layout = QVBoxLayout(self)
        warning = QLabel(
            f"ALL EXISTING DATA ON SLOT {target.slot} WILL BE DESTROYED.\n"
            "The existing USB host volume will be formatted to NTFS.\n"
            "No automatic restoration is provided. Keep your backup independently."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b00020; font-weight: 700;")
        layout.addWidget(warning)
        details = QPlainTextEdit(member_details(target, "CONFIRMATION REQUIRED"))
        details.setReadOnly(True)
        layout.addWidget(details, 1)
        self.backup = QCheckBox(
            "I have backed up or intentionally do not need the data on this USB member."
        )
        self.backup.setChecked(False)
        layout.addWidget(
            QLabel(
                "Backup acknowledgement — the application has not verified your backup."
            )
        )
        layout.addWidget(self.backup)
        self.dirty_required = dirty_format_exception(target)
        self.dirty_ack = QCheckBox(
            "I understand and acknowledge these dirty-filesystem risks."
        )
        self.dirty_ack.setChecked(False)
        self.dirty_ack.setVisible(self.dirty_required)
        layout.addWidget(self.dirty_ack)
        if self.dirty_required:
            dirty_warning = QLabel(
                DIRTY_FORMAT_REVIEW + "\n"
                "The existing filesystem may be inconsistent. Existing data may be damaged or unrecoverable. Formatting will destroy that filesystem anyway."
            )
            dirty_warning.setWordWrap(True)
            dirty_warning.setStyleSheet("color: #b00020; font-weight: 700;")
            layout.addWidget(dirty_warning)
        layout.addWidget(QLabel(f"Type exactly: FORMAT SLOT {target.slot}"))
        self.phrase = QLineEdit()
        layout.addWidget(self.phrase)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.format_button = self.buttons.addButton(
            "Erase selected host and format NTFS",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.format_button.setStyleSheet("color: #b00020; font-weight: 700;")
        self.format_button.setEnabled(False)
        self.format_button.setAutoDefault(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.backup.toggled.connect(self._check)
        self.dirty_ack.toggled.connect(self._check)
        self.phrase.textChanged.connect(self._check)
        layout.addWidget(self.buttons)

    def _check(self):
        self.format_button.setEnabled(
            self.backup.isChecked()
            and self.phrase.text() == f"FORMAT SLOT {self.target.slot}"
            and (not self.dirty_required or self.dirty_ack.isChecked())
        )

    def confirmation(self):
        return PreparationConfirmation(
            self.target,
            self.backup.isChecked(),
            self.phrase.text(),
            self.dirty_ack.isChecked(),
        )


class HostPreparationThread(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(
        self, service, slot, confirmation=None, parent=None, *, identity_only=False
    ):
        super().__init__(parent)
        self.service, self.slot, self.confirmation = service, slot, confirmation
        self.identity_only = identity_only

    def run(self):
        try:
            result = (
                self.service.record_identity(self.slot, self.progress.emit)
                if self.identity_only
                else (
                    self.service.prepare(self.confirmation, self.progress.emit)
                    if self.confirmation
                    else self.service.recheck(self.slot, self.progress.emit)
                )
            )
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.completed.emit(result)


class HostPreparationPage(QWidget):
    prepare_requested = Signal(int)
    recheck_requested = Signal(int)
    validation_requested = Signal()
    identity_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._scanning = False
        self._active_action = None
        self._rows = {}
        self._identity_diagnostics = {}
        self._health_diagnostics = {}
        self._recordable_slots = set()
        layout = QVBoxLayout(self)
        warning = QLabel(
            "HOST PREPARATION — DESTRUCTIVE, ONE MEMBER AT A TIME\n"
            "Formatting erases the selected USB host. Backup acknowledgement and typed confirmation are required."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b00020; font-weight: 700;")
        layout.addWidget(warning)
        self.summary = QLabel("0/4 PREPARED — waiting for inventory")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(4, 7)
        self.table.setHorizontalHeaderLabels(
            (
                "Slot",
                "Model / serial",
                "Letter",
                "Filesystem",
                "Capacity",
                "Health (disk / volume)",
                "Preparation status",
            )
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch
        )
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.currentCellChanged.connect(lambda *_: self._selection())
        layout.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.identity_details = QPlainTextEdit()
        self.identity_details.setReadOnly(True)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self.details, "Member / safety")
        self.detail_tabs.addTab(
            self.identity_details, "Identity reconciliation (read-only)"
        )
        self.health_details = QPlainTextEdit()
        self.health_details.setReadOnly(True)
        self.detail_tabs.addTab(
            self.health_details, "Raw health / decision (read-only)"
        )
        layout.addWidget(self.detail_tabs, 1)
        self.recheck_button = QPushButton("Recheck selected host (read-only)")
        self.validate_button = QPushButton("Run post-format 4-member 8 GiB validation")
        self.identity_button = QPushButton(
            "Record proven legacy identity (no USB changes)"
        )
        harmless = QHBoxLayout()
        harmless.addWidget(self.recheck_button)
        harmless.addWidget(self.validate_button)
        harmless.addWidget(self.identity_button)
        harmless.addStretch()
        layout.addLayout(harmless)
        layout.addSpacing(16)
        self.prepare_button = QPushButton(
            "Review selected FAT32 host for destructive preparation…"
        )
        self.prepare_button.setStyleSheet(
            "color: #b00020; border: 2px solid #b00020; padding: 8px; font-weight: 700;"
        )
        self.prepare_button.clicked.connect(
            lambda: self._request("prepare", self.prepare_requested)
        )
        self.recheck_button.clicked.connect(
            lambda: self._request("recheck", self.recheck_requested)
        )
        self.validate_button.clicked.connect(
            lambda: self._request("validation", self.validation_requested)
        )
        self.identity_button.clicked.connect(
            lambda: self._request("identity", self.identity_requested)
        )
        layout.addWidget(self.prepare_button)
        self.progress = QLabel(
            "Select a member to inspect its complete physical and host identity."
        )
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        self.availability = QLabel()
        self.availability.setWordWrap(True)
        layout.addWidget(self.availability)
        self.capabilities = QLabel(
            "Production VHDX creation / Storage Spaces deployment / final volume / startup attachment:\n"
            "NOT IMPLEMENTED IN THIS VERSION. Deployment commands remain PREVIEW ONLY."
        )
        self.capabilities.setWordWrap(True)
        layout.addWidget(self.capabilities)
        self._action_buttons = {
            "prepare": self.prepare_button,
            "recheck": self.recheck_button,
            "identity": self.identity_button,
            "validation": self.validate_button,
        }
        self._button_labels = {
            name: button.text() for name, button in self._action_buttons.items()
        }
        self._selection()

    def selected_slot(self):
        selected = self.table.selectionModel().selectedRows()
        return selected[0].row() + 1 if len(selected) == 1 else 0

    def _request(self, action, signal):
        # Snapshot the selected logical slot before any focus/worker transitions.
        slot = self.selected_slot()
        if not self._action_buttons[action].isEnabled():
            self.progress.setText(
                "Action not started: another operation or inventory scan is active, or this action is not eligible."
            )
            return
        if action == "validation":
            signal.emit()
        else:
            signal.emit(slot)

    def begin_action(self, action, slot=None):
        if self._active_action is not None:
            self.progress.setText(
                "Another Host Preparation action is already running; duplicate request rejected."
            )
            return False
        self._active_action = action
        text = {
            "recheck": "Rechecking",
            "identity": "Recording proven identity",
            "prepare": "Reviewing destructive preparation",
            "validation": "Reviewing 4-member validation",
        }[action]
        message = f"Slot {slot}: {text}…" if slot else text + "…"
        self._action_buttons[action].setText(text + "…")
        self.progress.setText(message)
        self._selection()
        return True

    def end_action(self):
        self._active_action = None
        for action, button in self._action_buttons.items():
            button.setText(self._button_labels[action])
        self._selection()

    def set_scanning(self, scanning):
        self._scanning = bool(scanning)
        self._selection()

    def set_state(self, rows, summary):
        self._rows = rows
        self.summary.setText(summary)
        blocker = QSignalBlocker(self.table)
        for slot in range(1, 5):
            device, status, reason = rows[slot]
            volume = (
                device.host_volumes[0]
                if device and len(device.host_volumes) == 1
                else None
            )
            values = (
                str(slot),
                f"{device.model} / {device.serial_number}"
                if device
                else "Disconnected/unassigned",
                ", ".join(device.drive_letters) if device else "—",
                volume.filesystem if volume else "—",
                gib(device.capacity_bytes) if device else "—",
                f"{device.health_status} / {volume.health_status if volume else 'UNKNOWN'}"
                if device
                else "—",
                status,
            )
            for column, value in enumerate(values):
                item = self.table.item(slot - 1, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(slot - 1, column, item)
                if item.text() != str(value):
                    item.setText(str(value))
                item.setToolTip(member_details(device, status, reason))
        del blocker
        self._selection()

    def set_busy(self, busy):
        self._busy = busy
        self.table.setEnabled(not busy)
        self._selection()

    def set_identity_diagnostics(self, diagnostics, recordable_slots):
        self._identity_diagnostics = diagnostics
        self._recordable_slots = set(recordable_slots)
        self._selection()

    def set_health_diagnostics(self, diagnostics):
        self._health_diagnostics = dict(diagnostics)
        self._selection()

    def _selection(self):
        available = not (self._busy or self._scanning or self._active_action)
        row = self._rows.get(self.selected_slot())
        self.details.setPlainText(
            member_details(*row) if row else "Select one logical member."
        )
        self.identity_details.setPlainText(
            self._identity_diagnostics.get(
                self.selected_slot(),
                "Select a member for read-only identity diagnostics.",
            )
        )
        self.identity_button.setEnabled(
            available and self.selected_slot() in self._recordable_slots
        )
        self.health_details.setPlainText(
            self._health_diagnostics.get(
                self.selected_slot(), "Select a member for read-only health provenance."
            )
        )
        self.prepare_button.setEnabled(
            available
            and bool(row)
            and row[1] in {"READY FOR PREPARATION", DIRTY_FORMAT_REVIEW}
        )
        self.recheck_button.setEnabled(available and bool(row))
        self.validate_button.setEnabled(
            available
            and len(self._rows) == 4
            and all(row[1] == "PREPARED" for row in self._rows.values())
        )
        self.validate_button.setToolTip(
            "Requires all four current hosts to be PREPARED; inventory and other storage operations must be idle."
        )
        self.availability.setText(
            "Working… actions are unavailable until the current operation finishes."
            if self._busy or self._active_action
            else "Refreshing inventory… actions will become available when the scan finishes."
            if self._scanning
            else f"Selected logical slot: {self.selected_slot()}."
            if row
            else "Select one logical member."
        )
