import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QInputDialog

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.benchmark_store import BenchmarkStore
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


if __name__ == "__main__":
    unittest.main()
