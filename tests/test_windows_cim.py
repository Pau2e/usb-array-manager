import unittest

from usb_array_manager.services.windows_cim import _record_to_device


class RecordConversionTests(unittest.TestCase):
    def test_record_is_normalized(self) -> None:
        device = _record_to_device(
            {
                "model": " Example Flash Disk ",
                "serial_number": " ABC123 ",
                "capacity_bytes": "30752636928",
                "drive_letters": ["J:", "I:"],
                "device_path": r"\\.\PHYSICALDRIVE4",
                "pnp_device_id": r"USB\VID_1234&PID_ABCD\ABC123",
                "usb_device_id": None,
            }
        )

        self.assertEqual(device.model, "Example Flash Disk")
        self.assertEqual(device.serial_number, "ABC123")
        self.assertEqual(device.capacity_bytes, 30_752_636_928)
        self.assertEqual(device.drive_letters, ("J:", "I:"))
        self.assertEqual(device.usb_vid, "1234")
        self.assertEqual(device.usb_pid, "ABCD")
        self.assertTrue(device.is_connected)

    def test_missing_optional_values_are_allowed(self) -> None:
        device = _record_to_device(
            {
                "model": "USB Disk",
                "serial_number": None,
                "capacity_bytes": None,
                "drive_letters": None,
                "device_path": r"\\.\PHYSICALDRIVE7",
                "pnp_device_id": r"USBSTOR\DISK&VEN_GENERIC",
                "usb_device_id": None,
            }
        )

        self.assertIsNone(device.serial_number)
        self.assertIsNone(device.capacity_bytes)
        self.assertEqual(device.drive_letters, ())
        self.assertIsNone(device.usb_vid)
        self.assertIsNone(device.usb_pid)

    def test_parent_usb_id_supplies_vid_pid_and_serial_is_cleaned(self) -> None:
        device = _record_to_device(
            {
                "model": "USB Disk",
                "serial_number": "ABC123\x06\x18",
                "capacity_bytes": 1,
                "drive_letters": "I:",
                "device_path": r"\\.\PHYSICALDRIVE2",
                "pnp_device_id": r"USBSTOR\DISK&VEN_GENERIC",
                "usb_device_id": r"USB\VID_0781&PID_5599\ABC123",
            }
        )

        self.assertEqual(device.serial_number, "ABC123")
        self.assertEqual(device.drive_letters, ("I:",))
        self.assertEqual(device.usb_vid, "0781")
        self.assertEqual(device.usb_pid, "5599")


if __name__ == "__main__":
    unittest.main()
