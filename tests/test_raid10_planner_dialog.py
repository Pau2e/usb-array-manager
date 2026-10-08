import unittest
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSizePolicy

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.raid10_plan_store import Raid10PlanStore
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
        self.assertIn("Evaluated all 3 valid pairing", dialog._suggestion_reason.text())
        self.assertIn("HEALTHY", dialog._array_health.text())
        self.assertEqual(dialog._content_splitter.orientation(), Qt.Orientation.Horizontal)
        self.assertGreaterEqual(dialog._failure_table.minimumHeight(), 150)
        self.assertTrue(
            dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
        )
        self.assertTrue(
            dialog.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
        )
        self.assertTrue(
            dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint
        )
        self.assertEqual(
            dialog._selection_group.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Maximum,
        )
        self.assertLessEqual(dialog._selection_group.layout().spacing(), 3)

        dialog._failure_checkboxes[1].setChecked(True)
        application.processEvents()
        self.assertIn("DEGRADED", dialog._array_health.text())
        self.assertEqual(dialog._failure_table.item(0, 2).text(), "DEGRADED")

        dialog._failure_checkboxes[2].setChecked(True)
        application.processEvents()
        self.assertIn("FAILED", dialog._array_health.text())

        dialog._reset_failures()
        self.assertIn("HEALTHY", dialog._array_health.text())
        self.assertFalse(any(dialog._failure_checkboxes[slot].isChecked() for slot in range(1, 5)))
        dialog.close()

    def test_saved_plan_layout_is_reloaded_and_checked(self) -> None:
        application = QApplication.instance() or QApplication([])
        devices = tuple(device(slot) for slot in range(1, 5))
        results = {slot: result(slot) for slot in range(1, 5)}
        with tempfile.TemporaryDirectory() as directory:
            store = Raid10PlanStore(Path(directory) / "raid10_plan.json")
            saved_plan = store.save(
                ((1, 4), (2, 3)),
                saved_at="2026-10-08T00:00:00+00:00",
            )
            dialog = Raid10PlannerDialog(
                devices,
                results,
                plan_store=store,
                saved_plan=saved_plan,
            )
            application.processEvents()

            loaded_pairs = tuple(
                (first.currentData(), second.currentData())
                for first, second in dialog._pair_combos
            )
            self.assertEqual(loaded_pairs, ((1, 4), (2, 3)))
            self.assertIn("Saved RAID10 plan:", dialog._readiness_status.text())
            self.assertTrue(dialog._export_plan_button.isEnabled())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
