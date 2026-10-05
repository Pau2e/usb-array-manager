from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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


def _speed(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"
