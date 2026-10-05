import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.slot_store import (
    SlotConflictError,
    SlotStore,
    SlotStoreError,
)


def make_device(
    *,
    serial: str = "DUPLICATE-SERIAL",
    container_id: str | None = "{CONTAINER-1}",
    usb_device_id: str | None = r"USB\VID_0781&PID_5581\DEVICE-1",
    pnp_device_id: str | None = r"USBSTOR\DISK&VEN_SANDISK\DEVICE-1",
    location: str = "PCIROOT(0)#USB(1)",
    drive_letter: str = "I:",
    device_path: str = r"\\.\PHYSICALDRIVE4",
) -> StorageDevice:
    return StorageDevice(
        model="USB SanDisk 3.2Gen1 USB Device",
        serial_number=serial,
        capacity_bytes=30_762_547_200,
        drive_letters=(drive_letter,),
        device_path=device_path,
        pnp_device_id=pnp_device_id,
        usb_vid="0781",
        usb_pid="5581",
        is_connected=True,
        bus_type="USB",
        media_type="Removable Media",
        can_pool=None,
        health_status="OK",
        usb_device_id=usb_device_id,
        container_id=container_id,
        location_paths=(location,),
    )


class SlotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary_directory.name) / "slots.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_assignment_survives_restart_and_drive_number_changes(self) -> None:
        original = make_device()
        SlotStore(self.config_path).assign(original, 1)

        reconnected = replace(
            original,
            drive_letters=("Z:",),
            device_path=r"\\.\PHYSICALDRIVE9",
        )
        displayed = SlotStore(self.config_path).reconcile([reconnected])

        self.assertEqual(len(displayed), 1)
        self.assertEqual(displayed[0].slot, 1)
        self.assertEqual(displayed[0].drive_letters, ("Z:",))
        self.assertEqual(displayed[0].device_path, r"\\.\PHYSICALDRIVE9")

    def test_disconnected_device_keeps_reserved_slot(self) -> None:
        store = SlotStore(self.config_path)
        store.assign(make_device(), 2)

        displayed = SlotStore(self.config_path).reconcile([])

        self.assertEqual(len(displayed), 1)
        self.assertEqual(displayed[0].slot, 2)
        self.assertFalse(displayed[0].is_connected)
        self.assertIn(2, SlotStore(self.config_path).reserved_slots())

    def test_duplicate_serials_match_using_container_id(self) -> None:
        first = make_device(container_id="{FIRST}", location="PORT-1")
        second = make_device(
            container_id="{SECOND}",
            usb_device_id=r"USB\VID_0781&PID_5581\DEVICE-2",
            pnp_device_id=r"USBSTOR\DISK&VEN_SANDISK\DEVICE-2",
            location="PORT-2",
            drive_letter="J:",
            device_path=r"\\.\PHYSICALDRIVE5",
        )
        store = SlotStore(self.config_path)
        store.assign(first, 1)
        store.assign(second, 2)

        displayed = SlotStore(self.config_path).reconcile([second, first])
        slots_by_container = {device.container_id: device.slot for device in displayed}

        self.assertEqual(slots_by_container["{FIRST}"], 1)
        self.assertEqual(slots_by_container["{SECOND}"], 2)

    def test_identical_devices_fall_back_to_unique_location(self) -> None:
        first = make_device(
            container_id=None,
            usb_device_id=None,
            pnp_device_id=None,
            location="PORT-1",
        )
        second = make_device(
            container_id=None,
            usb_device_id=None,
            pnp_device_id=None,
            location="PORT-2",
            drive_letter="J:",
            device_path=r"\\.\PHYSICALDRIVE5",
        )
        store = SlotStore(self.config_path)
        store.assign(first, 1)
        store.assign(second, 2)

        displayed = SlotStore(self.config_path).reconcile([second, first])
        slots_by_location = {
            device.location_paths[0]: device.slot for device in displayed
        }

        self.assertEqual(slots_by_location["PORT-1"], 1)
        self.assertEqual(slots_by_location["PORT-2"], 2)

    def test_cannot_assign_two_devices_to_one_slot(self) -> None:
        store = SlotStore(self.config_path)
        store.assign(make_device(), 1)

        with self.assertRaises(SlotConflictError):
            store.assign(make_device(container_id="{SECOND}"), 1)

    def test_identity_does_not_use_drive_letter_or_device_path(self) -> None:
        store = SlotStore(self.config_path)
        store.assign(make_device(), 1)

        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        identity = data["slots"]["1"]["identity"]

        self.assertNotIn("drive_letters", identity)
        self.assertNotIn("device_path", identity)

    def test_invalid_json_is_not_silently_overwritten(self) -> None:
        self.config_path.write_text("not-json", encoding="utf-8")

        with self.assertRaises(SlotStoreError):
            SlotStore(self.config_path).reconcile([make_device()])

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), "not-json")

    def test_legacy_configuration_is_copied_to_visible_location(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy" / "slots.json"
        legacy_path.parent.mkdir()
        SlotStore(legacy_path).assign(make_device(), 3)

        store = SlotStore(self.config_path, legacy_path=legacy_path)

        self.assertEqual(store.reserved_slots(), {3})
        self.assertTrue(self.config_path.exists())
        self.assertTrue(legacy_path.exists())


if __name__ == "__main__":
    unittest.main()
