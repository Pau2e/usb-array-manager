import unittest
from datetime import datetime, timezone

from PySide6.QtCore import Qt

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.ui.device_table_model import (
    BenchmarkTableModel,
    DeviceTableModel,
    OverviewTableModel,
)


class DeviceTableModelTests(unittest.TestCase):
    def test_one_device_is_one_table_row(self) -> None:
        model = DeviceTableModel()
        model.set_devices(
            [
                StorageDevice(
                    model="Example USB Disk",
                    serial_number="ABC123",
                    capacity_bytes=32 * 1024**3,
                    drive_letters=("I:",),
                    device_path=r"\\.\PHYSICALDRIVE4",
                    pnp_device_id=r"USBSTOR\DISK&VEN_EXAMPLE",
                    usb_vid="1234",
                    usb_pid="ABCD",
                    is_connected=True,
                    bus_type="USB",
                    media_type="SSD",
                    can_pool=False,
                    health_status="Healthy",
                    slot=2,
                )
            ]
        )

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.columnCount(), 18)
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole),
            "Slot 2",
        )
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole),
            "Example USB Disk",
        )
        self.assertEqual(
            model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole),
            "I:",
        )
        self.assertEqual(
            model.data(model.index(0, 8), Qt.ItemDataRole.DisplayRole),
            "USB",
        )
        self.assertEqual(
            model.data(model.index(0, 10), Qt.ItemDataRole.DisplayRole),
            "No",
        )
        self.assertEqual(
            model.data(model.index(0, 12), Qt.ItemDataRole.DisplayRole),
            "Yes",
        )
        self.assertEqual(
            model.data(model.index(0, 16), Qt.ItemDataRole.DisplayRole),
            "NOT TESTED",
        )

        model.set_benchmark_results(
            {
                2: BenchmarkResult(
                    slot=2,
                    tested_at=datetime.now(timezone.utc).isoformat(),
                    drive_letter="I:",
                    file_size_bytes=1024**3,
                    sequential_read_mbps=101.25,
                    sequential_write_mbps=42.5,
                    burst_write_mbps=60.0,
                    sustained_write_mbps=35.75,
                    write_stability_cv=0.1,
                    qualification="GOOD",
                )
            }
        )
        self.assertEqual(
            model.data(model.index(0, 13), Qt.ItemDataRole.DisplayRole),
            "101.2",
        )
        self.assertEqual(
            model.data(model.index(0, 16), Qt.ItemDataRole.DisplayRole),
            "GOOD",
        )

    def test_focused_tab_models_expose_only_relevant_columns(self) -> None:
        device = StorageDevice(
            model="Example USB Disk",
            serial_number="ABC123",
            capacity_bytes=32 * 1024**3,
            drive_letters=("I:",),
            device_path=r"\\.\PHYSICALDRIVE4",
            pnp_device_id=r"USBSTOR\DISK&VEN_EXAMPLE",
            usb_vid="1234",
            usb_pid="ABCD",
            is_connected=True,
            bus_type="USB",
            media_type="SSD",
            health_status="Healthy",
            slot=2,
        )
        result = BenchmarkResult(
            slot=2,
            tested_at="2026-10-09T00:00:00+00:00",
            drive_letter="I:",
            file_size_bytes=1024**3,
            sequential_read_mbps=101.25,
            sequential_write_mbps=42.5,
            burst_write_mbps=60.0,
            sustained_write_mbps=35.75,
            write_stability_cv=0.1,
            qualification="GOOD",
        )
        overview = OverviewTableModel()
        benchmarks = BenchmarkTableModel()
        for model in (overview, benchmarks):
            model.set_devices([device])
            model.set_benchmark_results({2: result})

        self.assertEqual(overview.columnCount(), 11)
        self.assertEqual(
            overview.headerData(2, Qt.Orientation.Horizontal), "Serial / identity"
        )
        self.assertEqual(overview.data(overview.index(0, 10)), "GOOD")
        self.assertEqual(benchmarks.columnCount(), 8)
        self.assertEqual(benchmarks.data(benchmarks.index(0, 4)), "35.8")
        self.assertEqual(
            benchmarks.data(benchmarks.index(0, 6)),
            "2026-10-09T00:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
