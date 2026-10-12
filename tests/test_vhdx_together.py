import json
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_batch_operations import device
from usb_array_manager.models.vhdx_probe import VhdxDiskInfo
from usb_array_manager.services.vhdx_probe import ELIGIBILITY_PROBE_BYTES, TEST_FOLDER_NAME, VhdxProbeError
from usb_array_manager.services.vhdx_together import (
    TogetherDiskPartRunner, _provider_query_script, validate_vhdxs_together,
)


def virtual_disk(slot):
    return VhdxDiskInfo(
        disk_number=slot + 7, identity=f"VIRTUAL-{slot}", bus_type="File Backed Virtual",
        media_type="Unspecified", can_pool=True, operational_status="OK", health_status="Healthy",
        size_bytes=ELIGIBILITY_PROBE_BYTES, storage_unique_id=f"VIRTUAL-{slot}",
        storage_device_id=str(slot + 7), storage_object_id=f"OBJECT-{slot}",
        friendly_name="Microsoft Virtual Disk",
    )


class FakeTogetherRunner:
    def __init__(self, *, create_failure=None, attach_failure=None, unpoolable=None,
                 ambiguous=False, duplicate=False, detach_failure=None, cancel=None):
        self.events = []
        self.attached = {}
        self.peak = 0
        self.create_failure = create_failure
        self.attach_failure = attach_failure
        self.unpoolable = unpoolable
        self.ambiguous = ambiguous
        self.duplicate = duplicate
        self.detach_failure = detach_failure
        self.cancel = cancel

    def _enumerate_disks(self):
        return {disk.disk_number: disk for disk in self.attached.values()}

    def create(self, path, size):
        slot = int(path.stem.removeprefix("slot"))
        self.events.append(("create", slot))
        if size != ELIGIBILITY_PROBE_BYTES:
            raise AssertionError("The test must use 8 GiB expandable disks")
        path.write_bytes(b"tiny file representing expandable VHDX")
        if slot == self.create_failure:
            raise VhdxProbeError("injected creation failure after partial file")

    def attach(self, path, *, prepare_filesystem):
        if prepare_filesystem:
            raise AssertionError("No filesystem setup is allowed")
        slot = int(path.stem.removeprefix("slot"))
        self.events.append(("attach", slot))
        disk = virtual_disk(slot)
        if self.duplicate and slot == 2:
            disk = virtual_disk(1)
        self.attached[path] = disk
        self.peak = max(self.peak, len(self.attached))
        if slot == self.attach_failure:
            raise VhdxProbeError("injected attachment failure after attaching")
        return disk

    def inspect_together(self, expected):
        self.events.append(("inspect", len(self.attached)))
        if len(self.attached) != 4:
            raise VhdxProbeError("All four must be attached simultaneously")
        if self.ambiguous:
            raise VhdxProbeError("slot3.vhdx: ambiguous Storage Spaces identity")
        result = dict(expected)
        for path, disk in result.items():
            slot = int(path.stem.removeprefix("slot"))
            result[path] = replace(disk, can_pool=True)
            if slot == self.unpoolable:
                result[path] = replace(disk, can_pool=False, cannot_pool_reasons=("In a Pool",))
        if self.cancel:
            self.cancel.set()
        return result

    def detach(self, path):
        slot = int(path.stem.removeprefix("slot"))
        self.events.append(("detach", slot))
        if slot == self.detach_failure:
            raise VhdxProbeError("injected detach failure")
        self.attached.pop(path, None)


class TogetherWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.roots = {}
        for slot in range(1, 5):
            self.roots[slot] = self.root / str(slot)
            self.roots[slot].mkdir()
        self.devices = tuple(device(slot) for slot in range(1, 5))
        self.progress = []

    def tearDown(self):
        self.temp.cleanup()

    def run_validation(self, runner, **kwargs):
        return validate_vhdxs_together(
            self.devices, runner=runner, roots=self.roots, elevation_checker=lambda: True,
            name_factory=lambda: "test-token", progress=self.progress.append, **kwargs,
        )

    def assert_clean(self):
        for root in self.roots.values():
            self.assertEqual(list(root.iterdir()), [])

    def test_four_successfully_attached_at_once_and_cleanup_after_inspection(self):
        runner = FakeTogetherRunner()
        result = self.run_validation(runner)
        self.assertEqual(result.status, "READY")
        self.assertTrue(result.simultaneous_verified)
        self.assertEqual(runner.peak, 4)
        self.assertEqual(runner.events, [("create", slot) for slot in range(1, 5)] + [("attach", slot) for slot in range(1, 5)] + [("inspect", 4)] + [("detach", slot) for slot in range(4, 0, -1)])
        self.assertTrue(all(member.identity_verified and member.disk.can_pool for member in result.members))
        self.assertIn("4/4 VHDX members READY simultaneously", result.explanation)
        self.assertTrue(any("Creating VHDX 1/4" in message for message in self.progress))
        self.assertTrue(any("Cleaning up 3/4" in message for message in self.progress))
        self.assert_clean()

    def test_one_nonpoolable_member_reports_partial_and_exact_reason(self):
        result = self.run_validation(FakeTogetherRunner(unpoolable=3))
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.members[2].status, "NOT READY")
        self.assertEqual(result.members[2].reason, "In a Pool")
        self.assert_clean()

    def test_one_ambiguous_identity_is_never_ready_and_cleans_all_four(self):
        runner = FakeTogetherRunner(ambiguous=True)
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(result.simultaneous_verified)
        self.assertEqual(result.members[2].status, "FAILED")
        self.assertEqual(len([event for event in runner.events if event[0] == "detach"]), 4)
        self.assert_clean()

    def test_creation_failure_member_three_cleans_all_created_files(self):
        runner = FakeTogetherRunner(create_failure=3)
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Slot 3 creation", result.explanation)
        self.assertEqual(runner.events, [("create", 1), ("create", 2), ("create", 3)])
        self.assertEqual(len(result.members), 4)
        self.assert_clean()

    def test_attachment_failure_member_four_cleans_every_attached_member(self):
        runner = FakeTogetherRunner(attach_failure=4)
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Slot 4 attachment", result.explanation)
        self.assertNotIn(("inspect", 4), runner.events)
        self.assertEqual([slot for action, slot in runner.events if action == "detach"], [4, 3, 2, 1])
        self.assertEqual(runner.attached, {})
        self.assert_clean()

    def test_duplicate_virtual_disk_mapping_aborts_before_shared_inspection(self):
        result = self.run_validation(FakeTogetherRunner(duplicate=True))
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Duplicate virtual-disk mapping", result.explanation)
        self.assert_clean()

    def test_attachment_failure_three_rolls_back_one_and_two(self):
        runner = FakeTogetherRunner(attach_failure=3)
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual([slot for action, slot in runner.events if action == "attach"], [1, 2, 3])
        self.assertEqual([slot for action, slot in runner.events if action == "detach"], [3, 2, 1])
        self.assert_clean()

    def test_pre_existing_file_is_never_deleted_or_overwritten(self):
        folder = self.roots[3] / TEST_FOLDER_NAME / "together-test-token"
        folder.mkdir(parents=True)
        existing = folder / "slot3.vhdx"
        existing.write_bytes(b"existing user data")
        runner = FakeTogetherRunner()
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(existing.read_bytes(), b"existing user data")
        self.assertEqual(runner.events, [])

    def test_shared_snapshot_cannot_claim_ready_if_a_disk_disappeared(self):
        class DisappearingRunner(FakeTogetherRunner):
            def inspect_together(self, expected):
                self.attached.pop(next(iter(self.attached)))
                return super().inspect_together(expected)

        result = self.run_validation(DisappearingRunner())
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(result.simultaneous_verified)
        self.assert_clean()

    def test_detach_failure_preserves_exact_file_and_continues_other_cleanup(self):
        runner = FakeTogetherRunner(detach_failure=2)
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        member = result.members[1]
        self.assertEqual(member.status, "CLEANUP FAILED")
        self.assertTrue(member.may_remain_attached)
        self.assertTrue(Path(member.leftover_path).exists())
        self.assertEqual(len(runner.attached), 1)
        for slot in (1, 3, 4):
            self.assertEqual(list(self.roots[slot].iterdir()), [])

    def test_delete_failure_reports_leftover_but_detaches_all_members(self):
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "slot2.vhdx":
                raise PermissionError("injected delete failure")
            return original(path, *args, **kwargs)

        runner = FakeTogetherRunner()
        with patch.object(Path, "unlink", unlink):
            result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertIsNotNone(result.members[1].leftover_path)
        self.assertFalse(result.members[1].may_remain_attached)
        self.assertEqual(runner.attached, {})

    def test_fat32_only_blocks_production_not_temporary_validation(self):
        self.devices = tuple(replace(item, filesystem_types=("FAT32",)) for item in self.devices)
        result = self.run_validation(FakeTogetherRunner())
        self.assertEqual(result.status, "READY")
        self.assert_clean()

    def test_system_member_four_is_rejected_before_any_member_is_created(self):
        self.devices = (*self.devices[:3], replace(self.devices[3], is_system_disk=True))
        runner = FakeTogetherRunner()
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Slot 4 preflight", result.explanation)
        self.assertEqual(result.members[3].status, "FAILED")
        self.assertEqual(runner.events, [])
        self.assert_clean()

    def test_non_elevated_test_creates_nothing(self):
        runner = FakeTogetherRunner()
        result = validate_vhdxs_together(self.devices, runner=runner, roots=self.roots, elevation_checker=lambda: False)
        self.assertEqual(result.status, "NEEDS ADMINISTRATOR")
        self.assertEqual(runner.events, [])
        self.assert_clean()

    def test_insufficient_space_on_member_four_creates_nothing(self):
        runner = FakeTogetherRunner()
        with patch("usb_array_manager.services.vhdx_together.shutil.disk_usage", side_effect=[shutil_usage(20 * 1024**3)] * 3 + [shutil_usage(1)]):
            result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Slot 4 preflight", result.explanation)
        self.assertEqual(runner.events, [])

    def test_cancel_between_attachment_stages_still_cleans_all_created_files(self):
        cancel = threading.Event()
        runner = FakeTogetherRunner()
        original = runner.attach

        def attach(path, **kwargs):
            disk = original(path, **kwargs)
            if path.stem == "slot2":
                cancel.set()
            return disk

        runner.attach = attach
        result = self.run_validation(runner, cancel_event=cancel)
        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual([slot for action, slot in runner.events if action == "attach"], [1, 2])
        self.assert_clean()

    def test_replaced_path_is_never_detached_or_deleted_as_an_owned_file(self):
        runner = FakeTogetherRunner()
        original_inspect = runner.inspect_together

        def inspect(expected):
            result = original_inspect(expected)
            path = next(path for path in expected if path.stem == "slot2")
            path.rename(path.with_suffix(".original"))
            path.write_bytes(b"unrelated replacement data")
            return result

        runner.inspect_together = inspect
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        member = result.members[1]
        self.assertEqual(Path(member.vhdx_path).read_bytes(), b"unrelated replacement data")
        self.assertNotIn(("detach", 2), runner.events)
        self.assertTrue(member.may_remain_attached)

    def test_changed_unique_id_cannot_be_ready_even_with_matching_number_and_size(self):
        runner = FakeTogetherRunner()
        original_inspect = runner.inspect_together

        def inspect(expected):
            result = original_inspect(expected)
            path = next(path for path in expected if path.stem == "slot3")
            result[path] = replace(result[path], storage_unique_id="UNRELATED-VIRTUAL-DISK")
            return result

        runner.inspect_together = inspect
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Slot 3", result.explanation)
        self.assertFalse(result.simultaneous_verified)
        self.assert_clean()

    def test_missing_host_during_rollback_reports_file_as_unverified_leftover(self):
        runner = FakeTogetherRunner(create_failure=3)
        create = runner.create
        offline_root = self.root / "offline-member1"

        def create_with_missing_host(path, size):
            if path.stem == "slot3":
                self.roots[1].rename(offline_root)
            create(path, size)

        runner.create = create_with_missing_host
        result = self.run_validation(runner)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.members[0].status, "CLEANUP FAILED")
        self.assertIsNotNone(result.members[0].leftover_path)
        self.assertTrue(any(offline_root.rglob("slot1.vhdx")))


def shutil_usage(free):
    from collections import namedtuple
    return namedtuple("Usage", "total used free")(free, 0, free)


class SharedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.expected = {Path(f"X:/test/slot{slot}.vhdx"): virtual_disk(slot) for slot in range(1, 5)}
        self.inventory = {disk.disk_number: disk for disk in self.expected.values()}
        self.images = [
            {"requested_path": str(path), "image_path": str(path), "device_path": rf"\\.\PhysicalDrive{disk.disk_number}", "attached": True, "storage_type": "VHDX", "error": ""}
            for path, disk in self.expected.items()
        ]
        rows = [
            {"DeviceId": str(disk.disk_number), "UniqueId": disk.storage_unique_id,
             "ObjectId": disk.storage_object_id, "FriendlyName": disk.friendly_name,
             "BusType": "File Backed Virtual", "MediaType": "Unspecified", "CanPool": True,
             "Size": disk.size_bytes, "CannotPoolReason": [], "HealthStatus": "Healthy", "OperationalStatus": "OK"}
            for disk in self.expected.values()
        ]
        self.payload = {"images": self.images, "queries": [{"provider": "Get-PhysicalDisk", "rows": rows, "error": ""}]}

    def inspect(self):
        runner = TogetherDiskPartRunner()
        with patch.object(runner, "_enumerate_disks", return_value=self.inventory), patch.object(runner, "_run_powershell", side_effect=[json.dumps(self.payload), json.dumps(self.images)]) as query:
            result = runner.inspect_together(self.expected)
        self.assertIn("Get-DiskImage", query.call_args_list[0].args[0])
        self.assertIn("Get-PhysicalDisk", query.call_args_list[0].args[0])
        return result

    def test_shared_snapshot_matches_all_four_paths_and_provider_identities(self):
        result = self.inspect()
        self.assertEqual(set(result), set(self.expected))
        self.assertTrue(all(disk.can_pool for disk in result.values()))

    def test_path_mismatch_is_rejected_even_if_disk_number_and_poolability_match(self):
        self.images[2]["image_path"] = "X:/unrelated.vhdx"
        with self.assertRaisesRegex(VhdxProbeError, "slot3.vhdx"):
            self.inspect()

    def test_duplicate_storage_candidate_is_ambiguous(self):
        self.payload["queries"][0]["rows"].append(self.payload["queries"][0]["rows"][2].copy())
        with self.assertRaisesRegex(VhdxProbeError, "ambiguous"):
            self.inspect()

    def test_nonpoolable_reason_is_returned_for_the_correct_member(self):
        self.payload["queries"][0]["rows"][2].update(CanPool=False, CannotPoolReason=[7])
        result = self.inspect()
        self.assertFalse(result[Path("X:/test/slot3.vhdx")].can_pool)
        self.assertEqual(result[Path("X:/test/slot3.vhdx")].pool_rejection_details, "Insufficient Capacity (7)")

    def test_missing_provider_information_does_not_reuse_single_probe_canpool(self):
        self.payload["queries"] = [{"provider": "Get-PhysicalDisk", "rows": [], "error": "provider failed"}]
        result = self.inspect()
        self.assertTrue(all(disk.can_pool is None for disk in result.values()))

    def test_numeric_storage_type_three_is_vhdx(self):
        for image in self.images:
            image["storage_type"] = "3"
            image["size"] = ELIGIBILITY_PROBE_BYTES
        self.assertTrue(all(disk.can_pool for disk in self.inspect().values()))

    def test_disk_removed_during_capability_queries_fails_final_path_check(self):
        final_images = [dict(image) for image in self.images]
        final_images[3]["attached"] = False
        runner = TogetherDiskPartRunner()
        with patch.object(runner, "_enumerate_disks", return_value=self.inventory), patch.object(runner, "_run_powershell", side_effect=[json.dumps(self.payload), json.dumps(final_images)]):
            with self.assertRaisesRegex(VhdxProbeError, "slot4.vhdx"):
                runner.inspect_together(self.expected)

    def test_powershell_array_wrapper_is_reported_as_malformed_not_ready_one(self):
        rows = self.payload["queries"][0]["rows"]
        self.payload["queries"][0]["rows"] = [{"value": rows, "Count": len(rows)}]
        diagnostics = []
        runner = TogetherDiskPartRunner(diagnostic_callback=diagnostics.append)
        with patch.object(runner, "_enumerate_disks", return_value=self.inventory), patch.object(runner, "_run_powershell", side_effect=[json.dumps(self.payload), json.dumps(self.images)]):
            result = runner.inspect_together(self.expected)
        self.assertTrue(all(disk.can_pool is None for disk in result.values()))
        self.assertTrue(any("malformed shared disk records" in message for message in diagnostics))
        self.assertFalse(any("READY — 1 object" in message for message in diagnostics))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Requires Windows PowerShell")
    def test_real_windows_powershell_serializes_four_separate_physical_records(self):
        # Execute only synthetic PSCustomObjects. No storage query or disk operation.
        records = []
        for disk in self.expected.values():
            records.append(
                "[PSCustomObject]@{"
                f"DeviceId = '{disk.disk_number}'; UniqueId = '{disk.storage_unique_id}'; "
                f"ObjectId = '{disk.storage_object_id}'; FriendlyName = 'Microsoft Virtual Disk'; "
                "BusType = 'File Backed Virtual'; MediaType = 'Unspecified'; CanPool = $true; "
                f"Size = {disk.size_bytes}; CannotPoolReason = @(); "
                "HealthStatus = 'Healthy'; OperationalStatus = 'OK'}"
            )
        expression = "@(" + ", ".join(records) + ")"
        script = "$queries = @()\n" + _provider_query_script("Synthetic physical disks", expression)
        script += "[PSCustomObject]@{ queries = $queries } | ConvertTo-Json -Depth 8 -Compress"
        runner = TogetherDiskPartRunner()
        payload = json.loads(runner._run_powershell(script))
        rows = payload["queries"][0]["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["DeviceId"] for row in rows], ["8", "9", "10", "11"])
        self.assertTrue(all(row["CanPool"] is True for row in rows))
        snapshot = {"images": self.images, "queries": payload["queries"]}
        with patch.object(runner, "_enumerate_disks", return_value=self.inventory), patch.object(runner, "_run_powershell", side_effect=[json.dumps(snapshot), json.dumps(self.images)]):
            result = runner.inspect_together(self.expected)
        self.assertTrue(all(disk.can_pool is True for disk in result.values()))


if __name__ == "__main__":
    unittest.main()
