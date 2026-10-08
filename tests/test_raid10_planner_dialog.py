import unittest

from PySide6.QtWidgets import QApplication

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.ui.raid10_planner_dialog import Raid10PlannerDialog


def device(slot: int) -> StorageDevice:
    return StorageDevice(
        model=f"USB Drive {slot}",
        serial_number=f"SERIAL-{slot}",
        capacity_bytes=32 * 1024**3,
        drive_letters=(f"{chr(ord('H') + slot)}:",),
        device_path=rf"\\.\PHYSICALDRIVE{slot}",
        pnp_device_id=f"USBSTOR\\DEVICE-{slot}",
        usb_vid="1234",
        usb_pid="5678",
        is_connected=True,
        slot=slot,
    )


def result(slot: int) -> BenchmarkResult:
    return BenchmarkResult(
        slot=slot,
        tested_at="2026-10-08T00:00:00+00:00",
        drive_letter=f"{chr(ord('H') + slot)}:",
        file_size_bytes=1024**3,
        sequential_read_mbps=150.0 + slot,
        sequential_write_mbps=60.0,
        burst_write_mbps=65.0,
        sustained_write_mbps=58.0 + slot,
        write_stability_cv=0.05,
        qualification="GOOD",
    )


class Raid10PlannerDialogTests(unittest.TestCase):
    def test_four_connected_slots_are_suggested_and_calculated(self) -> None:
        application = QApplication.instance() or QApplication([])
        devices = tuple(device(slot) for slot in range(1, 5))
        results = {slot: result(slot) for slot in range(1, 5)}

        dialog = Raid10PlannerDialog(devices, results)
        application.processEvents()

        self.assertEqual(len(dialog._pair_combos), 2)
        self.assertEqual(dialog._pair_table.rowCount(), 2)
        self.assertIn("Complete array estimates", dialog._array_summary.text())
        self.assertIn("ESTIMATE", dialog._pair_table.item(0, 3).text())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
