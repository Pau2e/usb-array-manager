import unittest

from PySide6.QtCore import Qt

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.ui.device_table_model import DeviceTableModel


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
                )
            ]
        )

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.columnCount(), 12)
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole),
            "Example USB Disk",
        )
        self.assertEqual(
            model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole),
            "I:",
        )
        self.assertEqual(
            model.data(model.index(0, 7), Qt.ItemDataRole.DisplayRole),
            "USB",
        )
        self.assertEqual(
            model.data(model.index(0, 9), Qt.ItemDataRole.DisplayRole),
            "No",
        )
        self.assertEqual(
            model.data(model.index(0, 11), Qt.ItemDataRole.DisplayRole),
            "Yes",
        )


if __name__ == "__main__":
    unittest.main()
