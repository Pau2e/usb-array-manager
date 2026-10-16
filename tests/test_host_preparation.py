import base64
import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from test_vhdx_probe import device
from test_vhdx_validation_state import successful_validation

from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import HostVolume
from usb_array_manager.services.host_preparation import (
    HostPreparationService,
    PreparationConfirmation,
    PreparationError,
    PreparationStore,
    WindowsHostFormatter,
    check_target,
    format_script,
    host_key,
)
from usb_array_manager.services.production_planning import (
    ALIGNMENT,
    GIB,
    plan_production,
)
from usb_array_manager.services.raid_backend import VhdxStorageSpacesBackend
from usb_array_manager.services.slot_store import SlotStore, SlotStoreError
from usb_array_manager.services.vhdx_probe import (
    ELIGIBILITY_PROBE_BYTES,
    DiskPartVhdxRunner,
    VhdxProbeSettings,
)
from usb_array_manager.ui.host_preparation import (
    HostConfirmationDialog,
    HostPreparationPage,
)


def member(slot=1, filesystem="FAT32", guid=None, letter=None, capacity=32 * GIB):
    current_letter = letter or f"{chr(71 + slot)}:"
    return replace(
        device(slot, filesystem),
        capacity_bytes=capacity,
        drive_letters=(current_letter,),
        host_volumes=(
            HostVolume(
                identity=guid
                or f"\\\\?\\Volume{{00000000-0000-0000-0000-{slot:012d}}}\\",
                drive_letter=current_letter,
                filesystem=filesystem,
                capacity_bytes=capacity - 4 * 1024**2,
                free_bytes=capacity - 8 * 1024**2,
                is_system=False,
                is_boot=False,
                health_status="Healthy",
            ),
        ),
    )


class HostPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.slots = SlotStore(self.root / "slots.json")
        self.before = member()
        self.slots.assign(self.before, 1)
        self.store = PreparationStore(self.root / "prep.json")
        self.formatter = Mock()
        self.after = member(
            filesystem="NTFS",
            guid="\\\\?\\Volume{11111111-1111-1111-1111-111111111111}\\",
            letter="Z:",
        )
        self.detector = Mock(side_effect=[[self.before], [self.after]])
        self.service = HostPreparationService(
            self.slots,
            self.store,
            detector=self.detector,
            formatter=self.formatter,
            elevated=lambda: True,
            protected=(),
            sleep=lambda _: None,
        )
        self.confirmation = PreparationConfirmation(self.before, True, "FORMAT SLOT 1")

    def test_system_boot_internal_and_unknown_protection_are_blocked(self):
        for field, value in (
            ("is_system_disk", True),
            ("is_boot_disk", True),
            ("is_system_disk", None),
            ("is_removable", False),
            ("bus_type", "SATA"),
            ("is_offline", None),
            ("is_read_only", True),
            ("health_status", None),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(PreparationError):
                    check_target(
                        1, self.before, [replace(self.before, **{field: value})]
                    )

    def test_duplicate_physical_or_volume_identity_and_strong_conflicts_block(self):
        variants = [
            [
                self.before,
                replace(member(2), storage_unique_id=self.before.storage_unique_id),
            ],
            [self.before, replace(member(2), host_volumes=self.before.host_volumes)],
            [replace(self.before, pnp_device_id="DIFFERENT")],
            [replace(self.before, capacity_bytes=self.before.capacity_bytes + GIB)],
            [replace(self.before, serial_number="SWAPPED")],
        ]
        for devices in variants:
            with self.subTest(devices=devices), self.assertRaises(PreparationError):
                check_target(1, self.before, devices)

    def test_saved_reference_survives_weaker_reconciliation(self):
        swapped = replace(self.before, serial_number="SWAPPED", capacity_bytes=16 * GIB)
        self.slots.reconcile([swapped])
        self.assertEqual(
            self.slots.preparation_reference(1).serial_number, self.before.serial_number
        )
        with self.assertRaises(PreparationError):
            self.service.inspect(1, [swapped])

    def test_unassigned_and_duplicate_reservations_block(self):
        with self.assertRaises(SlotStoreError):
            self.service.inspect(2, [member(2)])
        self.slots.assign(replace(self.before, slot=None), 2)
        with self.assertRaises(PreparationError):
            self.service.inspect(1, [self.before])

    def test_confirmation_and_backup_are_required_for_each_slot(self):
        for acknowledged, phrase in (
            (False, "FORMAT SLOT 1"),
            (True, ""),
            (True, "FORMAT SLOT 2"),
        ):
            with (
                self.subTest(acknowledged=acknowledged, phrase=phrase),
                self.assertRaises(PreparationError),
            ):
                self.service.prepare(
                    PreparationConfirmation(self.before, acknowledged, phrase)
                )
        self.formatter.format.assert_not_called()
        self.assertFalse(self.store.path.exists())
        second = member(2)
        self.slots.assign(second, 2)
        with self.assertRaises(PreparationError):
            self.service.prepare(PreparationConfirmation(second, True, "FORMAT SLOT 1"))

    def test_changed_mapping_after_confirmation_never_dispatches(self):
        for changed in (
            replace(self.before, disk_number=99),
            replace(
                self.before,
                drive_letters=("Z:",),
                host_volumes=(replace(self.before.host_volumes[0], drive_letter="Z:"),),
            ),
            replace(
                self.before,
                host_volumes=(
                    replace(
                        self.before.host_volumes[0],
                        identity=self.after.host_volumes[0].identity,
                    ),
                ),
            ),
            replace(self.before, storage_unique_id="REPLACEMENT"),
        ):
            with self.subTest(changed=changed):
                self.service.detector = lambda changed=changed: [changed]
                with self.assertRaises(PreparationError):
                    self.service.prepare(self.confirmation)
        self.formatter.format.assert_not_called()

    def test_no_elevation_no_dispatch_and_no_state_success(self):
        self.service.elevated = lambda: False
        with self.assertRaises(PreparationError):
            self.service.prepare(self.confirmation)
        self.formatter.format.assert_not_called()

    def test_adapter_also_requires_confirmation_and_checks_target(self):
        formatter = WindowsHostFormatter()
        with patch(
            "usb_array_manager.services.host_preparation.subprocess.run"
        ) as dispatch:
            with self.assertRaises(TypeError):
                formatter.format(self.before, ())
            with self.assertRaises(PreparationError):
                formatter.format(
                    self.before,
                    (),
                    confirmation=PreparationConfirmation(
                        self.before, False, "FORMAT SLOT 1"
                    ),
                    operation_lock=self.root / "helper.lock",
                )
            with self.assertRaises(PreparationError):
                formatter.format(
                    member(2),
                    (),
                    confirmation=self.confirmation,
                    operation_lock=self.root / "helper.lock",
                )
            dispatch.assert_not_called()

    def test_helper_lifetime_lock_blocks_recheck_in_new_app_instance(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(self.store.path.with_suffix(".formatter.lock")).replace(
            "'", "''"
        )
        script = (
            "$lock=[IO.File]::Open('"
            + lock_path
            + "',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None); [Console]::WriteLine('LOCKED'); $null=[Console]::ReadLine(); $lock.Dispose()"
        )
        child = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(script.encode("utf-16le")).decode(),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), b"LOCKED")
            with self.assertRaises(PreparationError), self.store.exclusive():
                self.fail("Active Windows helper must block another operation")
        finally:
            child.communicate(input=b"\n", timeout=15)
        with self.store.exclusive():
            pass

    def test_format_stage_persisted_before_dispatch_and_new_guid_rediscovered(self):
        def format_target(target, _protected, **_kwargs):
            data = self.store.load()
            self.assertEqual(data["members"]["1"]["status"], "FORMATTING")
            self.assertIsNone(data["validation"])
            self.assertEqual(target, self.before)

        self.formatter.format.side_effect = format_target
        logs = []
        result = self.service.prepare(self.confirmation, logs.append)
        self.assertEqual(result.slot, 1)
        self.assertEqual(result.drive_letters, ("Z:",))
        self.assertNotEqual(
            result.host_volumes[0].identity, self.before.host_volumes[0].identity
        )
        record = self.store.load()["members"]["1"]
        self.assertEqual(record["key"], host_key(self.after))
        self.assertEqual(
            [event["status"] for event in record["events"]],
            ["FORMATTING", "REDISCOVERING", "REVALIDATING", "PREPARED"],
        )
        self.assertEqual(self.service.status(1, [self.after])[0], "PREPARED")
        self.assertEqual(self.formatter.format.call_count, 1)
        self.assertTrue(
            any("backup acknowledged (not verified)" in line for line in logs)
        )

    def test_same_guid_is_requeried_not_assumed(self):
        self.detector.side_effect = [[self.before], [member(filesystem="NTFS")]]
        result = self.service.prepare(self.confirmation)
        self.assertEqual(self.detector.call_count, 2)
        self.assertEqual(result.host_volumes[0].filesystem, "NTFS")

    def test_format_failure_stops_single_member_and_requires_review(self):
        self.formatter.format.side_effect = RuntimeError("failed or uncertain")
        with self.assertRaises(RuntimeError):
            self.service.prepare(self.confirmation)
        self.assertEqual(self.store.load()["members"]["1"]["status"], "FAILED")
        self.assertEqual(self.detector.call_count, 1)
        with self.assertRaises(PreparationError):
            self.service.prepare(self.confirmation)
        self.assertEqual(self.formatter.format.call_count, 1)
        self.assertEqual(set(self.store.load()["members"]), {"1"})

    def test_rediscovery_failure_retries_queries_only_never_format(self):
        self.detector.side_effect = [[self.before]] + [[]] * 5
        with self.assertRaises(PreparationError):
            self.service.prepare(self.confirmation)
        self.assertEqual(self.formatter.format.call_count, 1)
        record = self.store.load()["members"]["1"]
        self.assertEqual(record["status"], "NEEDS REVIEW")
        self.assertEqual(
            record["observation"]["host_volumes"][0]["identity"],
            self.before.host_volumes[0].identity,
        )
        self.assertEqual(self.service.status(1, [self.after])[0], "NEEDS REVIEW")

    def test_unhealthy_or_ambiguous_new_volume_is_not_prepared(self):
        self.detector.side_effect = [[self.before]] + [
            [
                replace(
                    self.after,
                    host_volumes=(
                        replace(self.after.host_volumes[0], health_status=None),
                    ),
                )
            ]
        ] * 5
        with self.assertRaises(PreparationError):
            self.service.prepare(self.confirmation)
        self.assertEqual(self.store.load()["members"]["1"]["status"], "NEEDS REVIEW")

    def test_restart_in_flight_requires_readonly_review_not_reformat(self):
        self.service._record(
            1, "FORMATTING", "interrupted", self.before, invalidate=True
        )
        self.assertEqual(self.service.status(1, [self.after])[0], "NEEDS REVIEW")
        with self.assertRaises(PreparationError):
            self.service.prepare(self.confirmation)
        self.service.detector = lambda: [self.after]
        self.service.recheck(1)
        self.assertEqual(self.service.status(1, [self.after])[0], "PREPARED")
        self.formatter.format.assert_not_called()

    def test_persistent_preparation_is_revalidated_with_current_identity_and_letters(
        self,
    ):
        self.service.detector = lambda: [self.after]
        self.service.recheck(1)
        service = HostPreparationService(self.slots, self.store, protected=())
        letter_change = replace(
            self.after,
            drive_letters=("Y:",),
            host_volumes=(replace(self.after.host_volumes[0], drive_letter="Y:"),),
        )
        self.assertEqual(service.status(1, [letter_change])[0], "PREPARED")
        changed = replace(
            self.after,
            host_volumes=(
                replace(
                    self.after.host_volumes[0],
                    identity=self.before.host_volumes[0].identity,
                ),
            ),
        )
        self.assertNotEqual(service.status(1, [changed])[0], "PREPARED")

    def test_bad_store_or_save_failure_prevents_dispatch(self):
        self.store.path.write_text('{"schema_version":99}', encoding="utf-8")
        with self.assertRaises(PreparationError):
            self.service.prepare(self.confirmation)
        self.store.path.unlink()
        with patch.object(
            self.store, "save", side_effect=PreparationError("disk full")
        ):
            with self.assertRaises(PreparationError):
                self.service.prepare(self.confirmation)
        self.formatter.format.assert_not_called()

    def test_all_four_prepared_recalculates_actual_sizing_and_requires_fresh_proof(
        self,
    ):
        before = [member(slot) for slot in range(1, 5)]
        for item in before[1:]:
            self.slots.assign(item, item.slot)
        backend = VhdxStorageSpacesBackend()
        plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-16T01:00:00+00:00")
        backend.record_together(successful_validation(), before, plan)
        after = [
            member(
                slot,
                "NTFS",
                guid=f"\\\\?\\Volume{{22222222-2222-2222-2222-{slot:012d}}}\\",
            )
            for slot in range(1, 5)
        ]
        self.service.detector = lambda: after
        for slot in range(1, 5):
            self.service.recheck(slot)
        result = backend.production_plan(plan, after, application_drives=frozenset())
        self.assertEqual(
            next(
                check.status
                for check in result.checks
                if check.name == "Four-member VHDX validation"
            ),
            "BLOCKED",
        )
        backend.record_together(successful_validation(), after, plan)
        self.service.record_validation(after, successful_validation())
        receipt = self.store.load()["validation"]
        self.assertEqual(receipt["generation"], self.store.load()["generation"])
        self.assertTrue(receipt["at"])
        self.assertEqual(receipt["hosts"], [host_key(item) for item in after])
        result = backend.production_plan(plan, after, application_drives=frozenset())
        smallest = after[0].host_volumes[0].capacity_bytes
        reserve = max(2 * GIB, (smallest * 8 + 99) // 100)
        self.assertEqual(
            result.logical_bytes,
            (smallest - reserve - 256 * 1024**2) // ALIGNMENT * ALIGNMENT,
        )
        self.assertNotEqual(result.logical_bytes, 26 * GIB)
        self.assertEqual(
            next(
                check.status
                for check in result.checks
                if check.name == "Four-member VHDX validation"
            ),
            "READY",
        )
        restarted = VhdxStorageSpacesBackend().production_plan(
            plan, after, application_drives=frozenset()
        )
        self.assertEqual(
            next(
                check.status
                for check in restarted.checks
                if check.name == "Four-member VHDX validation"
            ),
            "BLOCKED",
        )

    def test_frozen_8192_mib_probe_independent_of_production_sizing(self):
        self.assertEqual(ELIGIBILITY_PROBE_BYTES, 8192 * 1024**2)
        self.assertEqual(VhdxProbeSettings().vhdx_size_bytes, 8192 * 1024**2)
        runner = DiskPartVhdxRunner()
        probe_path = self.root / "fake-probe.vhdx"
        with patch.object(
            runner,
            "_run_diskpart",
            side_effect=lambda _: probe_path.write_bytes(b"fixture"),
        ) as command:
            runner.create(probe_path, ELIGIBILITY_PROBE_BYTES)
        self.assertIn("maximum=8192", "\n".join(command.call_args.args[0]))
        self.assertIn("type=expandable", "\n".join(command.call_args.args[0]))
        changed = [member(slot, "NTFS", capacity=48 * GIB) for slot in range(1, 5)]
        result = plan_production(
            SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-16"),
            [(item.slot, "A", item) for item in changed],
            [],
            False,
        )
        self.assertNotEqual(result.logical_bytes, ELIGIBILITY_PROBE_BYTES)
        self.assertEqual(VhdxProbeSettings().vhdx_size_bytes, ELIGIBILITY_PROBE_BYTES)

    def test_script_is_exact_volume_object_format_with_same_inventory_and_fresh_guards(
        self,
    ):
        script = format_script(self.before, (str(self.root),))
        self.assertIn("Format-Volume -InputObject $last[0] -FileSystem NTFS", script)
        self.assertIn("Get-CimAssociatedInstance", script)
        self.assertIn("$null = Assert-Target", script)
        self.assertLess(
            script.index("$null = Assert-Target"),
            script.index("$formatted = Format-Volume"),
        )
        self.assertIn("Get-Volume -FilePath $path", script)
        self.assertNotIn("Format-Volume -DriveLetter", script)
        for forbidden in (
            "Clear-Disk",
            "Initialize-Disk",
            "New-Partition",
            "New-StoragePool",
            "New-VirtualDisk",
            "create vdisk",
            "Register-ScheduledTask",
            "-Force",
        ):
            self.assertNotIn(forbidden, script)

    def test_generated_script_parses_in_real_windows_powershell_without_execution(self):
        script_path = self.root / "parse-only.ps1"
        script_path.write_text(
            format_script(self.before, (str(self.root),)), encoding="utf-8-sig"
        )
        # ParseInput parses storage commands as text; it never invokes them.
        text = (
            "$tokens=$null; $errors=$null; $null=[System.Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText('"
            + str(script_path).replace("'", "''")
            + "'),[ref]$tokens,[ref]$errors); if($errors.Count){$errors | Out-String; exit 1}"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(text.encode("utf-16le")).decode(),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout.decode(errors="replace")
            + result.stderr.decode(errors="replace"),
        )

    def test_real_powershell_guard_dispatch_with_only_synthetic_storage_functions(self):
        from test_dirty_filesystem_policy import dirty_member

        # All inventory and every storage command are replaced. The fake format
        # writes a tiny marker in this test directory, never touches a volume.
        for scenario in (
            "valid",
            "changed_at_dispatch",
            "duplicate",
            "system",
            "protected",
            "stale_guid",
            "dirty_valid",
            "dirty_no_ack",
            "dirty_unknown",
            "dirty_physical_warning",
            "dirty_dispatch_warning",
            "dirty_dispatch_disk_warning",
        ):
            with self.subTest(scenario=scenario):
                target = (
                    dirty_member() if scenario.startswith("dirty_") else self.before
                )
                row = asdict(target)
                if scenario.startswith("dirty_"):
                    row["health_diagnostics"] = json.loads(row["health_details_json"])
                    row["host_volumes"][0]["health_diagnostics"] = json.loads(
                        row["host_volumes"][0]["health_details_json"]
                    )
                fixture = base64.b64encode(json.dumps([row]).encode()).decode()
                inventory = r"""
$script:reads++
$rows = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__FIXTURE__')) | ConvertFrom-Json
if ('__SCENARIO__' -eq 'changed_at_dispatch' -and $script:reads -ge 2) { $rows[0].serial_number = 'SWAPPED' }
if ('__SCENARIO__' -eq 'system') { $rows[0].is_system_disk = $true }
if ('__SCENARIO__' -eq 'stale_guid') { $rows[0].host_volumes[0].identity = 'different' }
if ('__SCENARIO__' -eq 'duplicate') { $rows = @($rows[0], $rows[0]) }
if ('__SCENARIO__' -eq 'dirty_unknown') { $rows[0].host_volumes[0].health_diagnostics.dirty_bit_set = $null }
if ('__SCENARIO__' -eq 'dirty_physical_warning') { $rows[0].health_diagnostics.get_disk.health_value = 'Warning' }
ConvertTo-Json -InputObject @($rows) -Depth 8 -Compress
""".replace("__FIXTURE__", fixture).replace("__SCENARIO__", scenario)
                marker = self.root / f"{scenario}.marker"
                mocks = (
                    r"""
$script:reads = 0
$script:volumeReads = 0
function Get-Disk {
    param($Number, [Parameter(ValueFromPipeline=$true)]$InputObject)
    process {
        $n = if ($null -ne $Number) { $Number } else { $InputObject.DiskNumber }
        $health = if ('__SCENARIO__' -eq 'dirty_dispatch_disk_warning' -and $script:reads -ge 2) { 'Warning' } else { 'Healthy' }
        [PSCustomObject]@{ Number=$n; HealthStatus=$health; OperationalStatus='Online'; IsSystem=$false; IsBoot=$false; BusType='USB'; IsReadOnly=$false; IsOffline=$false }
    }
}
function Get-Partition {
    param([Parameter(ValueFromPipeline=$true)]$InputObject)
    process { [PSCustomObject]@{ DiskNumber=$(if($InputObject.Protected){77}else{$InputObject.Number}); IsSystem=$false; IsBoot=$false } }
}
function Get-Volume {
    param([Parameter(ValueFromPipeline=$true)]$InputObject, $FilePath)
    process {
        if ($FilePath) { [PSCustomObject]@{ Protected=('__SCENARIO__' -ne 'protected'); Number=5 } }
        else {
            $script:volumeReads++
            $health = if ('__SCENARIO__'.StartsWith('dirty_')) { 'Warning' } else { 'Healthy' }
            $op = if ('__SCENARIO__' -eq 'dirty_dispatch_warning' -and $script:volumeReads -gt 1) { 'Degraded' } elseif ('__SCENARIO__'.StartsWith('dirty_')) { 'Full Repair Needed' } else { 'OK' }
            [PSCustomObject]@{ Path='__GUID__'; FileSystem='FAT32'; HealthStatus=$health; OperationalStatus=$op; Size=__SIZE__; ObjectId='EXACT-VOLUME-OBJECT' }
        }
    }
}
function Format-Volume {
    param($InputObject, $FileSystem, $NewFileSystemLabel, $Confirm, $ErrorAction)
    if ($InputObject.ObjectId -ne 'EXACT-VOLUME-OBJECT') { throw 'Wrong format object' }
    [IO.File]::WriteAllText('__MARKER__','fake format')
    [PSCustomObject]@{ FileSystem='NTFS'; HealthStatus='Healthy' }
}
""".replace("__SCENARIO__", scenario)
                    .replace("__GUID__", self.before.host_volumes[0].identity)
                    .replace(
                        "__SIZE__", str(self.before.host_volumes[0].capacity_bytes)
                    )
                    .replace("__MARKER__", str(marker).replace("'", "''"))
                )
                with patch(
                    "usb_array_manager.services.host_preparation._POWERSHELL_SCRIPT",
                    inventory,
                ):
                    script = mocks + format_script(
                        target,
                        (str(self.root),),
                        dirty_filesystem_acknowledged=scenario.startswith("dirty_")
                        and scenario != "dirty_no_ack",
                    )
                path = self.root / f"synthetic-{scenario}.ps1"
                path.write_text(script, encoding="utf-8-sig")
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(path),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                diagnostic = result.stdout.decode(
                    errors="replace"
                ) + result.stderr.decode(errors="replace")
                if scenario in {"valid", "dirty_valid"}:
                    self.assertEqual(result.returncode, 0, diagnostic)
                    self.assertTrue(marker.exists(), diagnostic)
                else:
                    self.assertNotEqual(result.returncode, 0, diagnostic)
                    self.assertFalse(marker.exists(), diagnostic)


class PreparationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_per_member_dialog_checkbox_unchecked_exact_phrase_and_cancel(self):
        dialog = HostConfirmationDialog(member())
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.backup.isChecked())
        self.assertFalse(dialog.format_button.isEnabled())
        dialog.backup.setChecked(True)
        dialog.phrase.setText("FORMAT SLOT 2")
        self.assertFalse(dialog.format_button.isEnabled())
        dialog.phrase.setText("FORMAT SLOT 1")
        self.assertTrue(dialog.format_button.isEnabled())
        dialog.reject()
        self.assertEqual(dialog.result(), 0)
        second = HostConfirmationDialog(member(2))
        self.addCleanup(second.close)
        self.assertFalse(second.backup.isChecked())
        self.assertEqual(second.phrase.text(), "")

    def test_page_no_format_all_no_deploy_and_validation_requires_four(self):
        page = HostPreparationPage()
        self.addCleanup(page.close)
        rows = {
            slot: (member(slot), "READY FOR PREPARATION", "FAT32")
            for slot in range(1, 5)
        }
        page.set_state(rows, "0/4 PREPARED")
        page.table.selectRow(0)
        self.assertTrue(page.prepare_button.isEnabled())
        self.assertFalse(page.validate_button.isEnabled())
        rows = {
            slot: (member(slot, "NTFS"), "PREPARED", "Revalidated")
            for slot in range(1, 5)
        }
        page.set_state(rows, "4/4 PREPARED")
        self.assertTrue(page.validate_button.isEnabled())
        self.assertFalse(page.prepare_button.isEnabled())
        page.set_busy(True)
        self.assertFalse(page.validate_button.isEnabled())
        self.assertFalse(page.recheck_button.isEnabled())
        from PySide6.QtWidgets import QPushButton

        labels = [button.text().lower() for button in page.findChildren(QPushButton)]
        self.assertFalse(
            any("format all" in label or "deploy array" in label for label in labels)
        )
        self.assertIn("NOT IMPLEMENTED IN THIS VERSION", page.capabilities.text())


class PreparationWindowTests(unittest.TestCase):
    def setUp(self):
        # Reuse the established fixture: temporary stores, no startup device scan.
        from test_inventory_benchmark_tabs import InventoryBenchmarkTabTests

        InventoryBenchmarkTabTests.setUp(self)
        self.window.refresh_devices = Mock()
        self.before = member()
        self.after = member(
            filesystem="NTFS",
            letter="Z:",
            guid="\\\\?\\Volume{33333333-3333-3333-3333-333333333333}\\",
        )
        self.window._slot_store.assign(self.before, 1)
        self.window._show_devices([self.before])
        self.formatter = Mock()
        self.service = HostPreparationService(
            self.window._slot_store,
            PreparationStore(self.root / "host-preparation-ui.json"),
            detector=Mock(side_effect=[[self.before], [self.after]]),
            formatter=self.formatter,
            elevated=lambda: True,
            protected=(),
            sleep=lambda _: None,
        )
        self.window._preparation_service = self.service
        self.window._refresh_host_preparation()

    def finish(self):
        from test_inventory_benchmark_tabs import InventoryBenchmarkTabTests

        InventoryBenchmarkTabTests.finish(self)

    def test_cancel_confirmation_leaves_member_and_evidence_untouched(self):
        with patch("usb_array_manager.ui.main_window.HostConfirmationDialog") as dialog:
            dialog.return_value.exec.return_value = 0
            self.window._start_host_preparation(1)
        self.formatter.format.assert_not_called()
        self.assertFalse(self.service.store.path.exists())
        self.assertFalse(self.window._storage_job_active())

    def test_confirmed_worker_rediscovers_and_updates_shared_inventory(self):
        with patch("usb_array_manager.ui.main_window.HostConfirmationDialog") as dialog:
            dialog.return_value.exec.return_value = 1
            dialog.return_value.confirmation.return_value = PreparationConfirmation(
                self.before, True, "FORMAT SLOT 1"
            )
            self.window._start_host_preparation(1)
            self.assertTrue(self.window._storage_job_active())
            self.assertFalse(self.window._backend_tab.probe_button.isEnabled())
            self.assertFalse(
                self.window._backend_tab.host_preparation.recheck_button.isEnabled()
            )
            self.finish()
        self.formatter.format.assert_called_once()
        displayed = self.window._model.devices()
        self.assertEqual(displayed[0].slot, 1)
        self.assertEqual(
            displayed[0].host_volumes[0].identity, self.after.host_volumes[0].identity
        )
        self.assertEqual(self.window._benchmark_model.devices()[0], displayed[0])
        self.assertIn(
            "1/4 PREPARED", self.window._backend_tab.host_preparation.summary.text()
        )
        self.assertFalse(
            self.window._backend_tab.host_preparation.validate_button.isEnabled()
        )
        self.window.refresh_devices.assert_called_once()

    def test_free_space_only_refresh_updates_readiness_after_restoration(self):
        devices = [member(slot, "NTFS") for slot in range(1, 5)]
        for item in devices:
            self.window._slot_store.assign(item, item.slot)
        self.window._saved_raid10_plan = SavedRaid10Plan(
            ((1, 2), (3, 4)), "2026-10-16T00:00:00+00:00"
        )
        self.window._show_devices(devices)
        changed = [
            replace(
                devices[0],
                host_volumes=(replace(devices[0].host_volumes[0], free_bytes=GIB),),
            ),
            *devices[1:],
        ]
        self.window._show_devices(changed)
        details = self.window._backend_tab.production_readiness.details.toPlainText()
        self.assertIn("1.00 GiB", details)

    def test_observed_host_change_invalidates_proof_even_after_reverting(self):
        devices = [member(slot, "NTFS") for slot in range(1, 5)]
        for item in devices:
            self.window._slot_store.assign(item, item.slot)
        plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-16T00:00:00+00:00")
        self.window._saved_raid10_plan = plan
        self.window._show_devices(devices)
        self.window._vhdx_backend.record_together(
            successful_validation(), devices, plan
        )
        proven = self.window._vhdx_backend.production_plan(
            plan, devices, application_drives=frozenset()
        )
        self.assertEqual(proven.checks[0].status, "READY")
        changed = [
            replace(
                devices[0],
                host_volumes=(
                    replace(
                        devices[0].host_volumes[0],
                        identity=self.after.host_volumes[0].identity,
                    ),
                ),
            ),
            *devices[1:],
        ]
        self.window._show_devices(changed)
        self.window._show_devices(devices)
        invalid = self.window._vhdx_backend.production_plan(
            plan, devices, application_drives=frozenset()
        )
        self.assertEqual(invalid.checks[0].status, "BLOCKED")
