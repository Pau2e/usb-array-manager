import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QInputDialog

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.benchmark_store import BenchmarkStore
from usb_array_manager.services.raid10_plan_store import Raid10PlanStore
from usb_array_manager.services.slot_store import SlotStore
from usb_array_manager.ui.main_window import MainWindow


class MainWindowSlotTests(unittest.TestCase):
    def test_assign_button_saves_selected_device(self) -> None:
        application = QApplication.instance() or QApplication([])
        device = StorageDevice(
            model="Example USB Disk",
            serial_number="SERIAL",
            capacity_bytes=32 * 1024**3,
            drive_letters=("I:",),
            device_path=r"\\.\PHYSICALDRIVE4",
            pnp_device_id=r"USBSTOR\DISK&VEN_EXAMPLE\DEVICE-1",
            usb_vid="1234",
            usb_pid="ABCD",
            is_connected=True,
            usb_device_id=r"USB\VID_1234&PID_ABCD\DEVICE-1",
            container_id="{CONTAINER-1}",
            location_paths=("PCIROOT(0)#USB(1)",),
        )

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "usb_array_manager.ui.main_window.QTimer.singleShot"
            ):
                window = MainWindow()
            window._slot_store = SlotStore(Path(directory) / "slots.json")
            window._benchmark_store = BenchmarkStore(
                Path(directory) / "benchmark_results.json"
            )
            window._benchmark_results = {}
            window._connected_devices = [device]
            window._model.set_devices([device])
            window._table.selectRow(0)
            application.processEvents()

            with patch.object(
                QInputDialog,
                "getItem",
                return_value=("Slot 1", True),
            ):
                window._assign_selected_slot()

            displayed = window._slot_store.reconcile([device])
            self.assertEqual(len(window._benchmark_buttons), 1)
            self.assertTrue(window._benchmark_buttons[0].isEnabled())
            window.close()

        self.assertEqual(displayed[0].slot, 1)
        self.assertTrue(displayed[0].is_connected)

    def test_tabbed_workspace_shares_planner_and_failure_state(self) -> None:
        application = QApplication.instance() or QApplication([])
        devices = [
            StorageDevice(
                model=f"USB Drive {slot}",
                serial_number=f"SERIAL-{slot}",
                capacity_bytes=32 * 1024**3,
                drive_letters=(f"{chr(71 + slot)}:",),
                device_path=rf"\\.\PHYSICALDRIVE{slot}",
                pnp_device_id=f"USBSTOR\\DEVICE-{slot}",
                usb_vid="1234",
                usb_pid="5678",
                is_connected=True,
                health_status="Healthy",
            )
            for slot in range(1, 5)
        ]

        with tempfile.TemporaryDirectory() as directory:
            with patch("usb_array_manager.ui.main_window.QTimer.singleShot"):
                window = MainWindow()
            window._slot_store = SlotStore(Path(directory) / "slots.json")
            window._benchmark_store = BenchmarkStore(
                Path(directory) / "benchmark_results.json"
            )
            window._raid10_plan_store = Raid10PlanStore(
                Path(directory) / "raid10_plan.json"
            )
            window._saved_raid10_plan = None
            window._benchmark_results = {}
            for slot, item in enumerate(devices, start=1):
                window._slot_store.assign(item, slot)

            window._show_devices(devices)
            application.processEvents()

            self.assertEqual(
                [window._tabs.tabText(index) for index in range(window._tabs.count())],
                [
                    "Overview",
                    "Benchmarks",
                    "RAID10 Planner",
                    "Failure Simulator",
                    "Logs / Details",
                ],
            )
            workspace = window._raid10_workspace
            self.assertIsNotNone(workspace)
            self.assertIs(window._tabs.widget(2), workspace.planner_page)
            self.assertIs(window._tabs.widget(3), workspace.failure_page)
            workspace._failure_checkboxes[1].setChecked(True)
            application.processEvents()
            self.assertIn("DEGRADED", workspace._array_health.text())

            window._rebuild_raid_workspace()
            application.processEvents()
            self.assertIn("DEGRADED", window._raid10_workspace._array_health.text())
            self.assertIn("Inventory:", window._logs_tab.output.toPlainText())
            window.close()


if __name__ == "__main__":
    unittest.main()
