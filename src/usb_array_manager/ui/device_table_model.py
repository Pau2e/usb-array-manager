from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.storage_device import StorageDevice


def _format_capacity(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unavailable"

    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024

    return "Unavailable"


def _text(value: str | None) -> str:
    return value or "Unavailable"


def _optional_bool(value: bool | None) -> str:
    if value is None:
        return "Unavailable"
    return "Yes" if value else "No"


@dataclass(frozen=True, slots=True)
class _Column:
    heading: str
    value: Callable[[StorageDevice], str]


class DeviceTableModel(QAbstractTableModel):
    _COLUMNS = (
        _Column(
            "Slot",
            lambda device: f"Slot {device.slot}" if device.slot is not None else "Unassigned",
        ),
        _Column("Model", lambda device: _text(device.model)),
        _Column("Serial number", lambda device: _text(device.serial_number)),
        _Column("Capacity", lambda device: _format_capacity(device.capacity_bytes)),
        _Column(
            "Drive letter(s)",
            lambda device: ", ".join(device.drive_letters) or "None",
        ),
        _Column("Device path", lambda device: _text(device.device_path)),
        _Column("USB VID", lambda device: _text(device.usb_vid)),
        _Column("USB PID", lambda device: _text(device.usb_pid)),
        _Column("Bus type", lambda device: _text(device.bus_type)),
        _Column("Media type", lambda device: _text(device.media_type)),
        _Column("Can pool", lambda device: _optional_bool(device.can_pool)),
        _Column("Health", lambda device: _text(device.health_status)),
        _Column("Connected", lambda device: "Yes" if device.is_connected else "No"),
    )
    _BENCHMARK_HEADINGS = (
        "Read MB/s",
        "Write MB/s",
        "Sustained MB/s",
        "Qualification",
        "Benchmark",
    )

    def __init__(self) -> None:
        super().__init__()
        self._devices: list[StorageDevice] = []
        self._benchmark_results: dict[int, BenchmarkResult] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._devices)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return (
            0
            if parent.isValid()
            else len(self._COLUMNS) + len(self._BENCHMARK_HEADINGS)
        )

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            device = self._devices[index.row()]
            if index.column() < len(self._COLUMNS):
                return self._COLUMNS[index.column()].value(device)

            result = (
                self._benchmark_results.get(device.slot)
                if device.slot is not None
                else None
            )
            benchmark_column = index.column() - len(self._COLUMNS)
            if benchmark_column == 0:
                return _speed(result.sequential_read_mbps if result else None)
            if benchmark_column == 1:
                return _speed(result.sequential_write_mbps if result else None)
            if benchmark_column == 2:
                return _speed(result.sustained_write_mbps if result else None)
            if benchmark_column == 3:
                return result.qualification if result else "NOT TESTED"
            return ""

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() != 1:
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section < len(self._COLUMNS):
                return self._COLUMNS[section].heading
            return self._BENCHMARK_HEADINGS[section - len(self._COLUMNS)]
        return str(section + 1)

    def set_devices(self, devices: list[StorageDevice]) -> None:
        self.beginResetModel()
        self._devices = sorted(
            devices,
            key=lambda device: (
                device.slot is None,
                device.slot if device.slot is not None else 5,
                device.device_path or "",
                device.model or "",
            ),
        )
        self.endResetModel()

    def set_benchmark_results(
        self, results: dict[int, BenchmarkResult]
    ) -> None:
        self.beginResetModel()
        self._benchmark_results = dict(results)
        self.endResetModel()

    @classmethod
    def benchmark_action_column(cls) -> int:
        return len(cls._COLUMNS) + len(cls._BENCHMARK_HEADINGS) - 1

    def device_at(self, row: int) -> StorageDevice | None:
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None

    def devices(self) -> tuple[StorageDevice, ...]:
        return tuple(self._devices)


def _speed(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"


def _compact_identity(device: StorageDevice) -> str:
    primary = device.serial_number or "No serial"
    identity = (
        device.storage_unique_id
        or device.container_id
        or device.usb_device_id
        or device.pnp_device_id
    )
    if not identity or identity == device.serial_number:
        return primary
    shortened = identity if len(identity) <= 32 else f"{identity[:29]}…"
    return f"{primary} · {shortened}"


def _full_identity(device: StorageDevice) -> str:
    fields = (
        ("Serial", device.serial_number),
        ("Storage unique ID", device.storage_unique_id),
        ("Container ID", device.container_id),
        ("USB device ID", device.usb_device_id),
        ("PnP device ID", device.pnp_device_id),
    )
    available = [f"{label}: {value}" for label, value in fields if value]
    return "\n".join(available) or "No persistent identity information available."


def _local_timestamp(value: str | None) -> str:
    if not value:
        return "Never"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed.astimezone()
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


class _FocusedDeviceTableModel(QAbstractTableModel):
    _HEADINGS: tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self._devices: list[StorageDevice] = []
        self._benchmark_results: dict[int, BenchmarkResult] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._devices)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._HEADINGS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._HEADINGS[section]
        return str(section + 1)

    def set_devices(self, devices: list[StorageDevice]) -> None:
        self.beginResetModel()
        self._devices = sorted(
            devices,
            key=lambda device: (
                device.slot is None,
                device.slot if device.slot is not None else 5,
                device.device_path or "",
                device.model or "",
            ),
        )
        self.endResetModel()

    def set_benchmark_results(
        self, results: dict[int, BenchmarkResult]
    ) -> None:
        self.beginResetModel()
        self._benchmark_results = dict(results)
        self.endResetModel()

    def device_at(self, row: int) -> StorageDevice | None:
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None

    def devices(self) -> tuple[StorageDevice, ...]:
        return tuple(self._devices)

    def _result(self, device: StorageDevice) -> BenchmarkResult | None:
        return (
            self._benchmark_results.get(device.slot)
            if device.slot is not None
            else None
        )


class OverviewTableModel(_FocusedDeviceTableModel):
    _HEADINGS = (
        "Slot",
        "Model",
        "Serial / identity",
        "Capacity",
        "Drive letter",
        "Physical device",
        "Bus type",
        "Media type",
        "Health",
        "Connected",
        "Qualification",
    )

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            device = self._devices[index.row()]
            result = self._result(device)
            values = (
                f"Slot {device.slot}" if device.slot is not None else "Unassigned",
                _text(device.model),
                _compact_identity(device),
                _format_capacity(device.capacity_bytes),
                ", ".join(device.drive_letters) or "None",
                _text(device.device_path),
                _text(device.bus_type),
                _text(device.media_type),
                _text(device.health_status),
                "Yes" if device.is_connected else "No",
                result.qualification if result else "NOT TESTED",
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 2:
            return _full_identity(self._devices[index.row()])
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() in (1, 2, 5):
                return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            return Qt.AlignmentFlag.AlignCenter
        return None


class BenchmarkTableModel(_FocusedDeviceTableModel):
    _HEADINGS = (
        "Logical slot",
        "Model",
        "Read MB/s",
        "Write MB/s",
        "Sustained write MB/s",
        "Qualification",
        "Last benchmark",
        "Benchmark control",
    )

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            device = self._devices[index.row()]
            result = self._result(device)
            values = (
                f"Slot {device.slot}" if device.slot is not None else "Unassigned",
                _text(device.model),
                _speed(result.sequential_read_mbps if result else None),
                _speed(result.sequential_write_mbps if result else None),
                _speed(result.sustained_write_mbps if result else None),
                result.qualification if result else "NOT TESTED",
                _local_timestamp(result.tested_at if result else None),
                "",
            )
            return values[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() == 1:
                return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            return Qt.AlignmentFlag.AlignCenter
        return None

    @classmethod
    def benchmark_action_column(cls) -> int:
        return len(cls._HEADINGS) - 1
