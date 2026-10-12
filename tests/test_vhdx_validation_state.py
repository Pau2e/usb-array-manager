import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_vhdx_probe import device
from usb_array_manager.models.raid_backend import SUPPORTED
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.vhdx_probe import (
    VhdxDiskInfo, VhdxProbeResult, VhdxTogetherMember, VhdxTogetherResult,
)
from usb_array_manager.services.raid10_plan_store import Raid10PlanStore
from usb_array_manager.services.raid_backend import VhdxStorageSpacesBackend


PROBE_REQUIREMENT = "Probe one selected USB slot successfully before deployment planning."


def successful_validation():
    members = tuple(VhdxTogetherMember(
        slot=slot, vhdx_path=f"X:/temporary/slot{slot}.vhdx",
        disk=VhdxDiskInfo(slot + 7, f"VIRTUAL-{slot}", "File Backed Virtual", "SSD", True, "OK", "Healthy"),
        identity_verified=True, status="READY", reason="",
        cleanup_status="Detached and temporary file deleted",
    ) for slot in range(1, 5))
    return VhdxTogetherResult("READY", "4/4 members READY simultaneously", members, True)


class VhdxValidationStateTests(unittest.TestCase):
    def setUp(self):
        self.backend = VhdxStorageSpacesBackend()
        self.devices = tuple(device(slot, "FAT32") for slot in range(1, 5))
        self.plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-13T12:00:00+00:00")
        self.result = successful_validation()

    def test_four_member_success_satisfies_probe_requirement_preserving_fat32_blockers(self):
        before = self.backend.assess(self.plan, self.devices)
        self.assertIn(PROBE_REQUIREMENT, before.requirements)
        self.backend.record_together(self.result, self.devices)
        after = self.backend.assess(self.plan, self.devices)
        self.assertNotIn(PROBE_REQUIREMENT, after.requirements)
        self.assertEqual(after.blockers, before.blockers)
        self.assertEqual(len(after.blockers), 4)
        self.assertTrue(all("FAT32" in blocker for blocker in after.blockers))
        self.assertEqual(after.status, SUPPORTED)
        self.assertEqual(after.dry_run_text, before.dry_run_text)
        self.assertIsNone(self.backend.probe_for_slot(1))

    def test_later_failure_restores_probe_requirement(self):
        self.backend.record_together(self.result, self.devices)
        self.assertNotIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)
        self.backend.record_together(replace(self.result, status="FAILED"), self.devices)
        self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)

    def test_all_four_fields_and_successful_cleanup_are_required(self):
        member = self.result.members[2]
        invalid_members = (
            replace(member, identity_verified=False),
            replace(member, disk=replace(member.disk, can_pool=False)),
            replace(member, disk=replace(member.disk, can_pool=None)),
            replace(member, cleanup_status="Delete failed"),
            replace(member, leftover_path=member.vhdx_path),
            replace(member, may_remain_attached=True),
        )
        for invalid in invalid_members:
            with self.subTest(member=invalid):
                result = replace(self.result, members=(*self.result.members[:2], invalid, self.result.members[3]))
                self.backend.record_together(result, self.devices)
                self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)
        self.backend.record_together(replace(self.result, simultaneous_verified=False), self.devices)
        self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)
        self.backend.record_together(replace(self.result, members=self.result.members[:3]), self.devices)
        self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)

    def test_restart_with_reloaded_plan_does_not_inherit_session_validation(self):
        self.backend.record_together(self.result, self.devices)
        with tempfile.TemporaryDirectory() as directory:
            store = Raid10PlanStore(Path(directory) / "plan.json")
            store.save(self.plan.pairs)
            restarted_backend = VhdxStorageSpacesBackend()
            assessment = restarted_backend.assess(store.load(), self.devices)
        self.assertIn(PROBE_REQUIREMENT, assessment.requirements)
        self.assertEqual(len(assessment.blockers), 4)

    def test_identity_replacement_disconnection_and_ambiguity_invalidate_evidence(self):
        self.backend.record_together(self.result, self.devices)
        replacements = (
            replace(self.devices[1], storage_unique_id="REPLACED-DEVICE"),
            replace(self.devices[1], is_connected=False),
        )
        for changed in replacements:
            with self.subTest(device=changed):
                current = (self.devices[0], changed, *self.devices[2:])
                self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, current).requirements)
        self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices, ambiguous_slots={3}).requirements)

    def test_missing_source_snapshot_cannot_satisfy_prerequisite(self):
        self.backend.record_together(self.result)
        self.assertIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)

    def test_independent_single_slot_success_remains_valid_after_four_member_failure(self):
        disk = self.result.members[0].disk
        self.backend.record_probe(VhdxProbeResult(
            slot=1, status=SUPPORTED, vhdx_path="X:/single.vhdx", attach_status="Attached; detached",
            virtual_disk=disk, cleanup_status="Detached and temporary file deleted", filesystem="FAT32",
            explanation="Single-slot probe succeeded", source_identity=self.devices[0].storage_unique_id,
        ))
        self.backend.record_together(replace(self.result, status="FAILED"), self.devices)
        self.assertNotIn(PROBE_REQUIREMENT, self.backend.assess(self.plan, self.devices).requirements)


if __name__ == "__main__":
    unittest.main()
