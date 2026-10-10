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
                "usb_device_id": r"USB\VID_1234&PID_ABCD\ABC123",
                "bus_type": 7,
                "media_type": 4,
                "can_pool": False,
                "health_status": 0,
                "storage_unique_id": "unique-123",
                "storage_unique_id_format": 3,
                "container_id": "{CONTAINER-123}",
                "location_paths": ["PCIROOT(0)#USB(1)"],
                "disk_number": 4,
                "is_removable": False,
                "is_boot_disk": False,
                "is_system_disk": False,
                "is_read_only": False,
                "is_offline": False,
                "partition_count": 1,
                "partition_style": "GPT",
                "filesystem_types": ["NTFS"],
                "operational_status": ["Online"],
            }
        )

        self.assertEqual(device.model, "Example Flash Disk")
        self.assertEqual(device.serial_number, "ABC123")
        self.assertEqual(device.capacity_bytes, 30_752_636_928)
        self.assertEqual(device.drive_letters, ("J:", "I:"))
        self.assertEqual(device.usb_vid, "1234")
        self.assertEqual(device.usb_pid, "ABCD")
        self.assertTrue(device.is_connected)
        self.assertEqual(device.bus_type, "USB")
        self.assertEqual(device.media_type, "SSD")
        self.assertFalse(device.can_pool)
        self.assertEqual(device.health_status, "Healthy")
        self.assertEqual(device.storage_unique_id, "unique-123")
        self.assertEqual(device.storage_unique_id_format, "3")
        self.assertEqual(device.container_id, "{CONTAINER-123}")
        self.assertEqual(device.location_paths, ("PCIROOT(0)#USB(1)",))
        self.assertEqual(device.disk_number, 4)
        self.assertFalse(device.is_removable)
        self.assertFalse(device.is_boot_disk)
        self.assertFalse(device.is_system_disk)
        self.assertEqual(device.partition_count, 1)
        self.assertEqual(device.partition_style, "GPT")
        self.assertEqual(device.filesystem_types, ("NTFS",))
        self.assertEqual(device.operational_status, "Online")

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
                "bus_type": None,
                "media_type": None,
                "can_pool": None,
                "health_status": None,
            }
        )

        self.assertIsNone(device.serial_number)
        self.assertIsNone(device.capacity_bytes)
        self.assertEqual(device.drive_letters, ())
        self.assertIsNone(device.usb_vid)
        self.assertIsNone(device.usb_pid)
        self.assertIsNone(device.bus_type)
        self.assertIsNone(device.media_type)
        self.assertIsNone(device.can_pool)
        self.assertIsNone(device.health_status)
        self.assertIsNone(device.disk_number)
        self.assertIsNone(device.is_boot_disk)
        self.assertEqual(device.filesystem_types, ())

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
