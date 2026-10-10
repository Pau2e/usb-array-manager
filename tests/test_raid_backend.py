import unittest
from dataclasses import replace

from usb_array_manager.models.raid_backend import (
    PARTIALLY_SUPPORTED,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
)
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.raid_backend import WindowsStorageSpacesBackend


def device(slot: int) -> StorageDevice:
    return StorageDevice(
        model=f"USB Disk {slot}",
        serial_number=f"SERIAL-{slot}",
        capacity_bytes=64 * 1024**3,
        drive_letters=(f"{chr(70 + slot)}:",),
        device_path=rf"\\.\PHYSICALDRIVE{slot + 4}",
        pnp_device_id=f"USBSTOR\\DISK-{slot}",
        usb_vid="1234",
        usb_pid="5678",
        is_connected=True,
        bus_type="USB",
        media_type="SSD",
        can_pool=True,
        health_status="Healthy",
        storage_unique_id=f"UNIQUE-{slot}",
        slot=slot,
        disk_number=slot + 4,
        is_removable=False,
        is_boot_disk=False,
        is_system_disk=False,
        is_read_only=False,
        is_offline=False,
        partition_count=0,
        partition_style="RAW",
        operational_status="Online",
    )


class WindowsStorageSpacesBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = WindowsStorageSpacesBackend()
        self.plan = SavedRaid10Plan(
            ((1, 2), (3, 4)),
            "2026-10-10T10:00:00+00:00",
        )
        self.devices = tuple(device(slot) for slot in range(1, 5))

    def test_supported_plan_produces_text_only_preview(self) -> None:
        assessment = self.backend.assess(self.plan, self.devices)

        self.assertEqual(assessment.status, SUPPORTED)
        self.assertEqual(len(assessment.drives), 4)
        self.assertIn("PREVIEW ONLY — NOT EXECUTED", assessment.dry_run_text)
        self.assertIn("No execution API exists in v0.6", assessment.dry_run_text)
        self.assertFalse(hasattr(self.backend, "execute"))

    def test_existing_filesystem_is_partially_supported(self) -> None:
        devices = list(self.devices)
        devices[0] = replace(
            devices[0],
            can_pool=False,
            partition_count=1,
            partition_style="GPT",
            filesystem_types=("NTFS",),
        )

        assessment = self.backend.assess(self.plan, devices)

        self.assertEqual(assessment.status, PARTIALLY_SUPPORTED)
        self.assertTrue(any("erase" in item for item in assessment.requirements))
        self.assertIn("FUTURE DESTRUCTIVE", assessment.dry_run_text)

    def test_system_disk_is_always_rejected(self) -> None:
        devices = list(self.devices)
        devices[1] = replace(devices[1], is_system_disk=True)

        assessment = self.backend.assess(self.plan, devices)

        self.assertEqual(assessment.status, UNSUPPORTED)
        self.assertTrue(any("system or boot" in item for item in assessment.blockers))
        self.assertIn("SAFETY STOP", assessment.dry_run_text)
        self.assertNotIn("New-StoragePool", assessment.dry_run_text)

    def test_duplicate_physical_disk_and_ambiguous_slot_are_rejected(self) -> None:
        devices = list(self.devices)
        devices[1] = replace(
            devices[1],
            storage_unique_id=devices[0].storage_unique_id,
        )

        assessment = self.backend.assess(
            self.plan,
            devices,
            ambiguous_slots={3},
        )

        self.assertEqual(assessment.status, UNSUPPORTED)
        self.assertTrue(any("already used" in item for item in assessment.blockers))
        self.assertTrue(any("ambiguous" in item for item in assessment.blockers))

    def test_disconnected_or_mismatched_saved_slot_is_rejected(self) -> None:
        devices = [replace(self.devices[0], is_connected=False), *self.devices[1:3]]

        assessment = self.backend.assess(self.plan, devices)

        self.assertEqual(assessment.status, UNSUPPORTED)
        self.assertTrue(any("disconnected" in item for item in assessment.blockers))
        self.assertTrue(any("Slot 4" in item for item in assessment.blockers))

    def test_missing_probe_data_is_unknown(self) -> None:
        devices = list(self.devices)
        devices[0] = replace(devices[0], can_pool=None)

        assessment = self.backend.assess(self.plan, devices)

        self.assertEqual(assessment.status, UNKNOWN)


if __name__ == "__main__":
    unittest.main()
