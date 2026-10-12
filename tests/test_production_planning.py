import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_vhdx_probe import device
from test_vhdx_validation_state import successful_validation
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import HostVolume
from usb_array_manager.services.production_planning import ALIGNMENT, GIB, VHDX_OVERHEAD
from usb_array_manager.services.raid_backend import VhdxStorageSpacesBackend
from usb_array_manager.services.windows_cim import _POWERSHELL_SCRIPT, _record_to_device


def hosts(filesystem="NTFS"):
    return tuple(replace(device(slot, filesystem), host_volumes=(HostVolume(
        identity=f"\\\\?\\Volume{{00000000-0000-0000-0000-{slot:012d}}}\\",
        drive_letter=device(slot).drive_letters[0], filesystem=filesystem,
        capacity_bytes=(28 + slot) * GIB, free_bytes=(28 + slot) * GIB,
        is_system=False, is_boot=False,
    ),)) for slot in range(1, 5))


class ProductionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.backend = VhdxStorageSpacesBackend()
        self.plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-13T12:00:00+00:00")
        self.devices = hosts()
        self.backend.record_together(successful_validation(), self.devices, self.plan)

    def calculate(self, devices=None, plan=None, **kwargs):
        return self.backend.production_plan(plan or self.plan, devices or self.devices,
                                            application_drives=frozenset(), **kwargs)

    def check(self, result, name):
        return next(check.status for check in result.checks if check.name == name)

    def test_fat32_supported_backend_but_blocked_production(self):
        devices = hosts("FAT32")
        self.backend.record_together(successful_validation(), devices, self.plan)
        result = self.calculate(devices)
        self.assertEqual(self.backend.assess(self.plan, devices).status, "SUPPORTED")
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(self.check(result, "Four-member VHDX validation"), "READY")
        self.assertEqual(self.check(result, "Host filesystem"), "BLOCKED")
        self.assertTrue(all("NTFS" in member.action for member in result.members))
        self.assertNotIn("exFAT", result.preview)

    def test_ntfs_ready_and_mount_point_warning(self):
        result = self.calculate()
        self.assertEqual(self.check(result, "Host filesystem"), "READY")
        self.assertEqual(self.check(result, "Persistent volume identity"), "READY")
        self.assertEqual(self.check(result, "Free-space requirement"), "READY")
        self.assertEqual(result.status, "WARNING")

    def test_size_derived_from_smallest_host_with_reserve(self):
        result = self.calculate()
        self.assertEqual(result.smallest_bytes, 29 * GIB)
        self.assertEqual(result.reserve_bytes, (29 * GIB * 8 + 99) // 100)
        self.assertEqual(result.logical_bytes,
                         (result.smallest_bytes - result.reserve_bytes - VHDX_OVERHEAD) // ALIGNMENT * ALIGNMENT)
        self.assertLess(result.logical_bytes, result.smallest_bytes)
        changed = (replace(self.devices[0], host_volumes=(replace(self.devices[0].host_volumes[0], capacity_bytes=20 * GIB),)), *self.devices[1:])
        self.assertLess(self.calculate(changed).logical_bytes, result.logical_bytes)

    def test_insufficient_free_space_blocks_without_shrinking_recommendation(self):
        first = self.devices[0]
        changed = (replace(first, host_volumes=(replace(first.host_volumes[0], free_bytes=GIB),)), *self.devices[1:])
        result = self.calculate(changed)
        self.assertEqual(result.logical_bytes, self.calculate().logical_bytes)
        self.assertEqual(self.check(result, "Free-space requirement"), "BLOCKED")
        self.assertEqual(self.check(result, "Four-member VHDX validation"), "READY")

    def test_conservative_reserve_overhead_and_rounding_for_small_and_large_hosts(self):
        self.assertEqual(VHDX_OVERHEAD, 256 * 1024**2)
        for capacity_gib in (4, 10, 20, 25, 28.64, 64, 100):
            with self.subTest(capacity_gib=capacity_gib):
                capacity = int(capacity_gib * GIB)
                devices = tuple(replace(item, capacity_bytes=capacity,
                    host_volumes=(replace(item.host_volumes[0], capacity_bytes=capacity, free_bytes=capacity),))
                    for item in self.devices)
                result = self.calculate(devices)
                expected_reserve = max(2 * GIB, (capacity * 8 + 99) // 100)
                self.assertEqual(result.reserve_bytes, expected_reserve)
                unrounded = capacity - expected_reserve - 256 * 1024**2
                self.assertEqual(result.logical_bytes, unrounded // ALIGNMENT * ALIGNMENT)
                self.assertEqual(result.logical_bytes % ALIGNMENT, 0)
                self.assertLessEqual(result.logical_bytes, unrounded)
                self.assertLess(unrounded - result.logical_bytes, ALIGNMENT)
                self.assertGreaterEqual(capacity - result.logical_bytes - VHDX_OVERHEAD, expected_reserve)

    def test_current_size_estimate_and_capacity_chain_are_calculated_not_hardcoded(self):
        capacities = tuple(int(value * GIB) for value in (28.64, 28.66, 28.68, 28.64))
        devices = tuple(replace(item, capacity_bytes=capacity,
            filesystem_types=("FAT32",), host_volumes=(replace(item.host_volumes[0],
                filesystem="FAT32", capacity_bytes=capacity, free_bytes=capacity),))
            for item, capacity in zip(self.devices, capacities))
        result = self.calculate(devices)
        self.assertEqual(result.smallest_bytes, min(capacities))
        self.assertEqual(result.logical_bytes, 26 * GIB)
        self.assertAlmostEqual(result.reserve_bytes / GIB, 2.2912, places=4)
        self.assertAlmostEqual(result.physical_bytes / GIB, 114.62, places=2)
        self.assertEqual(result.virtual_raw_bytes, 104 * GIB)
        self.assertEqual(result.mirrored_bytes, 52 * GIB)
        self.assertGreater(result.formatted_bytes, 50 * GIB)
        self.assertLess(result.formatted_bytes, 52 * GIB)
        self.assertIn("PRE-FORMAT ESTIMATE", result.preview)
        self.assertIn("maximum=<RECALCULATED_VHDX_SIZE_MIB>", result.preview)
        sizing = next(check.detail for check in result.checks if check.name == "Production VHDX sizing")
        for detail in ("28.64 GiB", "max(2 GiB, 8%)", "2.29 GiB", "0.25 GiB", "26.00 GiB"):
            self.assertIn(detail, sizing)
        smaller_capacity = 24 * GIB
        smaller = (replace(devices[0], capacity_bytes=smaller_capacity,
            host_volumes=(replace(devices[0].host_volumes[0], capacity_bytes=smaller_capacity, free_bytes=smaller_capacity),)), *devices[1:])
        self.assertEqual(self.calculate(smaller).logical_bytes, int(21.75 * GIB))

    def test_letter_change_preserves_guid_paths_and_validation(self):
        first = self.devices[0]
        changed = (replace(first, drive_letters=("Z:",), host_volumes=(replace(first.host_volumes[0], drive_letter="Z:"),)), *self.devices[1:])
        result = self.calculate(changed)
        self.assertEqual(result.members[0].path, self.calculate().members[0].path)
        self.assertEqual(result.members[0].letter, "Z:")
        self.assertEqual(self.check(result, "Four-member VHDX validation"), "READY")
        self.assertNotIn('file="Z:', result.preview)

    def test_changed_volume_identity_invalidates_proof(self):
        first = self.devices[0]
        changed = (replace(first, host_volumes=(replace(first.host_volumes[0], identity=self.devices[1].host_volumes[0].identity),)), *self.devices[1:])
        result = self.calculate(changed)
        self.assertEqual(self.check(result, "Four-member VHDX validation"), "BLOCKED")
        self.assertEqual(self.check(result, "Persistent volume identity"), "BLOCKED")

    def test_missing_or_ambiguous_volume_never_guessed(self):
        for volumes in ((), self.devices[0].host_volumes * 2):
            with self.subTest(volumes=volumes):
                result = self.calculate((replace(self.devices[0], host_volumes=volumes), *self.devices[1:]))
                self.assertIsNone(result.members[0].path)
                self.assertEqual(result.status, "BLOCKED")

    def test_filesystem_change_invalidates_four_member_proof(self):
        first = self.devices[0]
        changed = (replace(first, filesystem_types=("FAT32",), host_volumes=(replace(first.host_volumes[0], filesystem="FAT32"),)), *self.devices[1:])
        self.assertEqual(self.check(self.calculate(changed), "Four-member VHDX validation"), "BLOCKED")

    def test_saved_plan_change_invalidates_proof(self):
        changed = replace(self.plan, pairs=((1, 3), (2, 4)))
        self.assertEqual(self.check(self.calculate(plan=changed), "Four-member VHDX validation"), "BLOCKED")

    def test_failed_later_validation_and_restart_block(self):
        self.backend.record_together(replace(successful_validation(), status="FAILED"), self.devices, self.plan)
        self.assertEqual(self.check(self.calculate(), "Four-member VHDX validation"), "BLOCKED")
        self.backend = VhdxStorageSpacesBackend()
        self.assertEqual(self.check(self.calculate(), "Four-member VHDX validation"), "BLOCKED")

    def test_capacity_chain_and_layout(self):
        result = self.calculate()
        self.assertEqual(result.physical_bytes, 4 * 64 * GIB)
        self.assertEqual(result.virtual_raw_bytes, 4 * result.logical_bytes)
        self.assertEqual(result.mirrored_bytes, result.virtual_raw_bytes // 2)
        self.assertLess(result.formatted_bytes, result.mirrored_bytes)
        self.assertIn("-NumberOfDataCopies 2 -NumberOfColumns 2", result.preview)
        layout = next(check.detail for check in result.checks if check.name == "Storage Spaces layout")
        self.assertIn("Two-way mirror / RAID10-like", layout)
        self.assertIn("Not true RAID10", layout)
        self.assertIn("two failures are NOT guaranteed", layout)

    def test_deterministic_unique_files(self):
        result = self.calculate()
        self.assertEqual(len({member.path for member in result.members}), 4)
        for member in result.members:
            self.assertTrue(member.path.endswith(f"slot{member.slot}.vhdx"))
        self.assertEqual(result, self.calculate())

    def test_preview_is_text_only_no_storage_or_file_execution(self):
        with patch("subprocess.run", side_effect=AssertionError("No execution")), patch.object(Path, "open", side_effect=AssertionError("No file access")):
            result = self.calculate()
        self.assertTrue(result.preview.startswith("PREVIEW ONLY — NOT EXECUTED"))
        for command in ("create vdisk", "attach vdisk", "New-StoragePool", "New-VirtualDisk", "Initialize-Disk", "Format-Volume"):
            self.assertIn(command, result.preview)
        self.assertIn("DESTRUCTIVE", result.preview)
        self.assertIn("No Scheduled Tasks", result.preview)
        self.assertNotIn("Clear-Disk", result.preview)
        self.assertNotIn("format fs=", result.preview)

    def test_system_boot_application_duplicate_and_disconnected_protection(self):
        for first in (
            replace(self.devices[0], is_system_disk=True),
            replace(self.devices[0], is_boot_disk=True),
            replace(self.devices[0], is_connected=False),
            replace(self.devices[0], disk_number=self.devices[1].disk_number),
            replace(self.devices[0], storage_unique_id=self.devices[1].storage_unique_id),
            replace(self.devices[0], host_volumes=(replace(self.devices[0].host_volumes[0], is_system=True),)),
        ):
            with self.subTest(first=first):
                self.assertEqual(self.calculate((first, *self.devices[1:])).status, "BLOCKED")
        result = self.backend.production_plan(self.plan, self.devices, application_drives=frozenset({self.devices[0].drive_letters[0].casefold()}))
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(self.calculate(ambiguous_slots={1}).status, "BLOCKED")

    def test_readonly_inventory_volume_conversion_and_backward_compatibility(self):
        record = dict(host_volumes=[dict(identity=self.devices[0].host_volumes[0].identity,
                     filesystem="NTFS", capacity_bytes="10000", free_bytes="9000",
                     is_system=False, is_boot=False, drive_letter="G:")])
        parsed = _record_to_device(record)
        self.assertEqual(parsed.host_volumes[0].free_bytes, 9000)
        self.assertEqual(_record_to_device({}).host_volumes, ())
        self.assertIn("Win32_Volume", _POWERSHELL_SCRIPT)
        for command in ("Format-Volume", "New-Partition", "Initialize-Disk", "New-VHD"):
            self.assertNotIn(command, _POWERSHELL_SCRIPT)

    def test_ui_readiness_preview_at_small_desktop_sizes(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from usb_array_manager.ui.device_tabs import BackendDeploymentTab
        app = QApplication.instance() or QApplication([])
        widget = BackendDeploymentTab()
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.set_devices(self.devices)
        widget.set_assessments({self.backend.name: self.backend.assess(self.plan, self.devices)}, self.backend.name)
        widget.set_production_plan(self.calculate())
        for width, height in ((1280, 720), (1366, 768)):
            with self.subTest(width=width):
                widget.resize(width, height)
                widget.show()
                app.processEvents()
                self.assertLessEqual(widget.minimumSizeHint().width(), width)
                self.assertLessEqual(widget.minimumSizeHint().height(), height)
                self.assertTrue(widget.production_readiness.status.isVisible())
                self.assertTrue(widget.together_button.isVisible())
                self.assertIn("Two-way mirror / RAID10-like", widget.production_readiness.details.toPlainText())
                self.assertTrue(widget.production_readiness.commands.isReadOnly())
                self.assertTrue(widget.production_readiness.commands.toPlainText().startswith("PREVIEW ONLY"))
        widget.close()
        widget.deleteLater()
        app.processEvents()

    def test_blocked_preview_contains_complete_future_sequence(self):
        devices = hosts("FAT32")
        self.backend.record_together(successful_validation(), devices, self.plan)
        result = self.calculate(devices)
        preview = self.backend.assess(self.plan, devices).dry_run_text
        self.assertEqual(preview, result.preview)
        self.assertTrue(preview.startswith("PREVIEW ONLY — BLOCKED — DO NOT EXECUTE"))
        self.assertNotIn("No production command outline was generated", preview)
        for item in (
            "Back up", "migrate non-NTFS", "Rediscover/revalidate", "re-inspect capacity/free space",
            "production VHDXs", "Attach and identity-verify all four", "CanPool=True",
            "Storage Spaces pool", "two-way mirrored virtual disk", "final NTFS filesystem",
            "Choose/validate", "boot-time VHDX attachment", "Verify final",
        ):
            self.assertIn(item, preview)
        for step in range(1, 15):
            self.assertTrue(any(line.startswith(f"{step}. ") for line in preview.splitlines()))

    def test_preview_authoritative_identity_and_paths_are_volume_guids(self):
        preview = self.calculate().preview
        for member in self.calculate().members:
            self.assertIn(f"Current drive letter (readability only): {member.letter}", preview)
            self.assertIn(f"Persistent Volume GUID (authoritative): {member.volume.identity}", preview)
            self.assertIn(f"Deterministic production VHDX path: {member.path}", preview)
        self.assertIn("never persistent identifiers", preview)
        self.assertNotIn("-> future slot", preview)

    def test_fat32_preview_never_carries_current_size_or_guid_into_creation_commands(self):
        devices = hosts("FAT32")
        self.backend.record_together(successful_validation(), devices, self.plan)
        result = self.calculate(devices)
        self.assertIn("PRE-FORMAT ESTIMATE", result.preview)
        self.assertIn("Never carry the pre-format size or old Volume GUID forward blindly", result.preview)
        self.assertIn("recalculate reserves and production VHDX sizing", result.preview)
        commands = result.preview.split("VHDX creation / attachment", 1)[1]
        self.assertEqual(commands.count("maximum=<RECALCULATED_VHDX_SIZE_MIB>"), 4)
        self.assertIn("-Size <RECALCULATED_MIRRORED_SIZE_BYTES>", commands)
        self.assertNotIn(f"maximum={result.logical_bytes // 1024**2}", commands)
        for member in result.members:
            self.assertNotIn(member.volume.identity, commands)
            self.assertIn(f"<REVALIDATED_VOLUME_GUID_SLOT_{member.slot}>", commands)

    def test_post_format_measurements_recalculate_size_and_invalidate_old_proof(self):
        devices = hosts("FAT32")
        self.backend.record_together(successful_validation(), devices, self.plan)
        before = self.calculate(devices)
        after_devices = hosts("NTFS")
        first = after_devices[0]
        after_devices = (replace(first, host_volumes=(replace(first.host_volumes[0], capacity_bytes=25 * GIB, free_bytes=25 * GIB),)), *after_devices[1:])
        after = self.calculate(after_devices)
        self.assertLess(after.logical_bytes, before.logical_bytes)
        self.assertEqual(self.check(after, "Four-member VHDX validation"), "BLOCKED")
        self.assertEqual(self.check(after, "Host filesystem"), "READY")
        self.assertEqual(after.status, "BLOCKED")

    def test_blocked_preview_generation_has_no_storage_execution_or_writes(self):
        devices = hosts("FAT32")
        with patch("subprocess.run", side_effect=AssertionError("No processes")), \
             patch.object(Path, "mkdir", side_effect=AssertionError("No creation")), \
             patch.object(Path, "unlink", side_effect=AssertionError("No deletion")):
            assessment = self.backend.assess(self.plan, devices)
        self.assertIn("New-StoragePool", assessment.dry_run_text)
        self.assertIn("DO NOT EXECUTE", assessment.dry_run_text)

    def test_deployment_and_readiness_ui_show_same_blocked_preview(self):
        from PySide6.QtWidgets import QApplication
        from usb_array_manager.ui.device_tabs import BackendDeploymentTab
        app = QApplication.instance() or QApplication([])
        devices = hosts("FAT32")
        result = self.calculate(devices)
        assessment = self.backend.assess(self.plan, devices)
        tab = BackendDeploymentTab()
        tab.set_assessments({self.backend.name: assessment}, self.backend.name)
        tab.set_production_plan(result)
        self.assertEqual(tab.preview.toPlainText(), tab.production_readiness.commands.toPlainText())
        self.assertIn("PRE-FORMAT ESTIMATE", tab.production_readiness.details.toPlainText())
        self.assertTrue(tab.preview.isReadOnly())
        self.assertTrue(tab.production_readiness.commands.isReadOnly())
        tab.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
