import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from test_host_preparation import member

from usb_array_manager.services.host_preparation import (
    HostPreparationService,
    PreparationConfirmation,
    PreparationError,
    PreparationStore,
    WindowsHostFormatter,
    check_target,
)
from usb_array_manager.services.physical_identity import (
    AMBIGUOUS,
    NO_MATCH,
    PROVEN,
    compare_physical,
    normalize_field,
    reconcile_physical,
)
from usb_array_manager.services.slot_store import (
    SlotStore,
    SlotStoreError,
    _identity_from_device,
)
from usb_array_manager.services.vhdx_probe import (
    ELIGIBILITY_PROBE_BYTES,
    VhdxProbeSettings,
)
from usb_array_manager.ui.host_preparation import HostPreparationPage


def legacy_member(slot=1):
    return replace(
        member(slot),
        storage_unique_id=None,
        storage_unique_id_format=None,
        serial_number=f"SAVED-SERIAL-{slot}",
        pnp_device_id=rf"USBSTOR\DISK&VEN_SANDISK\SAVED-INSTANCE-{slot}",
        usb_device_id=rf"USB\VID_0781&PID_5581\SAVED-USB-INSTANCE-{slot}",
        container_id=f"{{00112233-4455-6677-8899-{slot:012d}}}",
    )


class PhysicalIdentityReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = legacy_member()
        snapshot = asdict(self.saved)
        snapshot.pop("slot")
        self.original = json.loads(
            json.dumps(
                {
                    "identity": _identity_from_device(self.saved),
                    "last_known_device": snapshot,
                }
            )
        )
        self.path = self.root / "slots.json"
        self.path.write_text(
            json.dumps({"schema_version": 1, "slots": {"1": self.original}}),
            encoding="utf-8",
        )
        self.slots = SlotStore(self.path)
        self.formatter = Mock()
        self.service = HostPreparationService(
            self.slots,
            PreparationStore(self.root / "prep.json"),
            detector=lambda: [self.saved],
            formatter=self.formatter,
            elevated=lambda: True,
            protected=(),
        )

    def test_v0753_null_storage_id_reconciles_with_complete_saved_physical_chain(self):
        result = self.service.identity_reconciliation(1, [self.saved])
        self.assertEqual(result.result, PROVEN)
        self.assertEqual(len(result.proven_candidates), 1)
        self.assertEqual(
            result.proven_candidates[0].policy, "saved-usb-parent+container+pnp+serial"
        )
        self.assertEqual(
            self.service.status(1, [self.saved])[0], "READY FOR PREPARATION"
        )
        self.assertIsNone(self.slots.preparation_reference(1).storage_unique_id)

    def test_diagnostics_never_write_legacy_records_or_run_storage_mutations(self):
        original = self.path.read_bytes()
        with patch(
            "usb_array_manager.services.host_preparation.subprocess.run"
        ) as command:
            for _ in range(3):
                result = self.service.identity_reconciliation(1, [self.saved])
                text = result.describe()
                self.service.status(1, [self.saved])
                self.service.exclusion_diagnostic(result)
            command.assert_not_called()
        self.assertIn("full-proof candidates: 1", text)
        self.assertIn("storage_unique_id: saved=None; current=None; ABSENT", text)
        self.assertIn("Identity migration: not attempted", text)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.service.store.path.exists())
        self.formatter.format.assert_not_called()

    def test_explicit_upgrade_preserves_originals_slot_and_records_real_evidence(self):
        current = replace(
            self.saved,
            storage_unique_id="NEWLY-AVAILABLE-REAL-ID",
            storage_unique_id_format="3",
        )
        self.service.detector = lambda: [current]
        logs = []
        with patch(
            "usb_array_manager.services.host_preparation.subprocess.run"
        ) as command:
            target = self.service.record_identity(1, logs.append)
            command.assert_not_called()
        record = json.loads(self.path.read_text())["slots"]["1"]
        self.assertEqual(target.slot, 1)
        self.assertEqual(record["identity"], self.original["identity"])
        self.assertEqual(
            record["last_known_device"], self.original["last_known_device"]
        )
        self.assertEqual(
            record["preparation_identity"]["reference"]["storage_unique_id"],
            "NEWLY-AVAILABLE-REAL-ID",
        )
        self.assertIn(
            "pnp_device_id", record["preparation_identity"]["evidence_fields"]
        )
        self.assertIn("container_id", record["preparation_identity"]["evidence_fields"])
        self.assertIn(
            "serial_number", record["preparation_identity"]["evidence_fields"]
        )
        self.assertEqual(
            self.slots.preparation_reference(1).storage_unique_id,
            "NEWLY-AVAILABLE-REAL-ID",
        )
        self.assertTrue(any("upgrade succeeded" in message for message in logs))
        self.assertFalse(self.service.identity_reconciliation(1, [current]).legacy)
        self.assertNotEqual(
            self.service.identity_reconciliation(
                1, [replace(current, storage_unique_id="SWAPPED")]
            ).result,
            PROVEN,
        )
        self.formatter.format.assert_not_called()

    def test_explicit_upgrade_does_not_fabricate_absent_storage_id(self):
        logs = []
        self.service.record_identity(1, logs.append)
        record = json.loads(self.path.read_text())["slots"]["1"]
        self.assertIsNone(
            record["preparation_identity"]["reference"]["storage_unique_id"]
        )
        self.assertTrue(any("no identifier fabricated" in message for message in logs))
        self.slots.reconcile([self.saved])
        self.assertIn(
            "preparation_identity", json.loads(self.path.read_text())["slots"]["1"]
        )

    def test_two_candidates_are_ambiguous_and_upgrade_refused(self):
        duplicate = replace(
            self.saved,
            disk_number=99,
            device_path=r"\\.\PHYSICALDRIVE99",
            drive_letters=("Y:",),
        )
        self.assertEqual(
            reconcile_physical(1, self.saved, [self.saved, duplicate]).result, AMBIGUOUS
        )
        original = self.path.read_bytes()
        self.service.detector = lambda: [self.saved, duplicate]
        with self.assertRaises(PreparationError):
            self.service.record_identity(1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.service.status(1, [self.saved, duplicate])[0], "BLOCKED")

    def test_zero_candidates_are_no_match_not_ambiguous(self):
        result = self.service.identity_reconciliation(1, [])
        self.assertEqual(result.result, NO_MATCH)
        self.assertIn("Zero current devices", result.reason)
        self.assertEqual(self.service.status(1, [])[0], "BLOCKED")
        original = self.path.read_bytes()
        with self.assertRaises(SlotStoreError):
            self.slots.record_preparation_identity(1, [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_shared_strong_identifier_with_conflicting_candidate_blocks_uniqueness(
        self,
    ):
        other = replace(legacy_member(2), container_id=self.saved.container_id)
        self.assertEqual(
            reconcile_physical(1, self.saved, [self.saved, other]).result, AMBIGUOUS
        )
        self.assertEqual(
            reconcile_physical(
                1, self.saved, [self.saved], other_references=(other,)
            ).result,
            AMBIGUOUS,
        )

    def test_letter_disk_number_device_path_and_volume_guid_are_not_physical_proof(
        self,
    ):
        moved = replace(
            self.saved,
            disk_number=99,
            device_path=r"\\.\PHYSICALDRIVE99",
            drive_letters=("Z:",),
            host_volumes=(
                replace(
                    self.saved.host_volumes[0],
                    drive_letter="Z:",
                    identity="\\\\?\\Volume{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}\\",
                ),
            ),
        )
        self.assertEqual(reconcile_physical(1, self.saved, [moved]).result, PROVEN)
        self.assertEqual(check_target(1, self.saved, [moved]), moved)
        wrong = replace(
            legacy_member(2),
            drive_letters=self.saved.drive_letters,
            host_volumes=self.saved.host_volumes,
            disk_number=self.saved.disk_number,
            device_path=self.saved.device_path,
        )
        self.assertEqual(reconcile_physical(1, self.saved, [wrong]).result, NO_MATCH)

    def test_model_capacity_or_serial_alone_cannot_upgrade(self):
        for changed in (
            replace(
                self.saved, pnp_device_id=None, usb_device_id=None, container_id=None
            ),
            replace(self.saved, pnp_device_id=None),
            replace(self.saved, serial_number=None),
            replace(self.saved, usb_device_id=None),
            replace(self.saved, container_id=None),
            replace(self.saved, container_id="not-a-guid"),
        ):
            with self.subTest(changed=changed):
                self.assertFalse(compare_physical(changed, changed).proven)
                self.assertEqual(
                    reconcile_physical(1, changed, [changed]).result, NO_MATCH
                )

    def test_conflict_or_loss_in_any_saved_strong_anchor_blocks(self):
        with_id = replace(self.saved, storage_unique_id="SAVED-ID")
        for field in (
            "pnp_device_id",
            "usb_device_id",
            "container_id",
            "serial_number",
            "storage_unique_id",
        ):
            for value in (None, "CONFLICT"):
                with self.subTest(field=field, value=value):
                    self.assertEqual(
                        reconcile_physical(
                            1, with_id, [replace(with_id, **{field: value})]
                        ).result,
                        NO_MATCH,
                    )

    def test_harmless_case_brace_padding_and_json_representation_normalize(self):
        equivalent = replace(
            self.saved,
            pnp_device_id=" " + self.saved.pnp_device_id.lower() + " ",
            usb_device_id=" " + self.saved.usb_device_id.lower() + " ",
            serial_number=" " + self.saved.serial_number.lower() + " ",
            container_id=self.saved.container_id.strip("{}").lower(),
        )
        self.assertEqual(reconcile_physical(1, self.saved, [equivalent]).result, PROVEN)
        roundtrip = json.loads(json.dumps(asdict(equivalent)))
        self.assertEqual(roundtrip["pnp_device_id"], equivalent.pnp_device_id)
        self.assertEqual(normalize_field("capacity_bytes", "30762547200"), 30762547200)
        self.assertIsNone(normalize_field("storage_unique_id", "  "))

    def test_normalization_preserves_distinct_serials_and_device_instance_forms(self):
        for a, b in (
            ("AB C", "ABC"),
            ("AB  C", "AB C"),
            ("0001", "1"),
            ("SERIAL&0", "SERIAL"),
        ):
            self.assertNotEqual(
                normalize_field("serial_number", a), normalize_field("serial_number", b)
            )
        for changed in (
            self.saved.pnp_device_id.replace("\\", "\\\\"),
            self.saved.pnp_device_id + "&0",
            r"\\?" + chr(92) + self.saved.pnp_device_id,
        ):
            self.assertNotEqual(
                normalize_field("pnp_device_id", self.saved.pnp_device_id),
                normalize_field("pnp_device_id", changed),
            )

    def test_system_boot_application_and_volume_warning_remain_blocked_after_proof(
        self,
    ):
        for current in (
            replace(self.saved, is_system_disk=True),
            replace(self.saved, is_boot_disk=True),
            replace(self.saved, is_system_disk=None),
            replace(self.saved, bus_type="SATA"),
            replace(
                self.saved,
                host_volumes=(
                    replace(self.saved.host_volumes[0], health_status="Warning"),
                ),
            ),
        ):
            self.assertEqual(
                reconcile_physical(1, self.saved, [current]).result, PROVEN
            )
            with self.assertRaises(PreparationError):
                check_target(1, self.saved, [current])
        with self.assertRaises(PreparationError):
            check_target(
                1,
                self.saved,
                [self.saved],
                protected=(self.saved.drive_letters[0] + r"\app\manager.py",),
            )

    def test_unproven_identity_cannot_reach_formatter_even_with_typed_confirmation(
        self,
    ):
        wrong = replace(self.saved, serial_number="WRONG")
        self.service.detector = lambda: [wrong]
        with self.assertRaises(PreparationError):
            self.service.prepare(
                PreparationConfirmation(self.saved, True, "FORMAT SLOT 1")
            )
        self.formatter.format.assert_not_called()
        unproven = replace(self.saved, container_id=None)
        with patch(
            "usb_array_manager.services.host_preparation.subprocess.run"
        ) as command:
            with self.assertRaises(PreparationError):
                WindowsHostFormatter().format(
                    unproven,
                    (),
                    confirmation=PreparationConfirmation(
                        unproven, True, "FORMAT SLOT 1"
                    ),
                    operation_lock=self.root / "no-dispatch.lock",
                )
            command.assert_not_called()

    def test_exclusion_diagnostics_do_not_equate_system_false_with_application_safe(
        self,
    ):
        proven = self.service.identity_reconciliation(1, [self.saved])
        self.service.protected = (self.saved.drive_letters[0] + r"\app.py",)
        self.assertIn("BLOCKED", self.service.exclusion_diagnostic(proven))
        self.assertIn(
            "NOT EVALUATED",
            self.service.exclusion_diagnostic(
                self.service.identity_reconciliation(1, [])
            ),
        )
        self.service.protected = (r"C:\app.py",)
        text = self.service.exclusion_diagnostic(proven)
        self.assertIn("INITIAL PATH CHECK PASS", text)
        self.assertIn("NOT RUN by diagnostics", text)

    def test_diagnostic_page_is_readonly_and_record_button_never_formats(self):
        app = QApplication.instance() or QApplication([])
        page = HostPreparationPage()
        self.addCleanup(page.close)
        rows = {
            slot: (legacy_member(slot), "BLOCKED", "Volume health Warning")
            for slot in range(1, 5)
        }
        page.set_state(rows, "0/4 PREPARED")
        text = self.service.identity_reconciliation(1, [self.saved]).describe()
        page.set_identity_diagnostics({1: text}, {1})
        page.table.selectRow(0)
        self.assertTrue(page.identity_details.isReadOnly())
        self.assertIn("PROVEN MATCH", page.identity_details.toPlainText())
        self.assertFalse(page.prepare_button.isEnabled())
        identity, destructive = [], []
        page.identity_requested.connect(identity.append)
        page.prepare_requested.connect(destructive.append)
        page.identity_button.click()
        app.processEvents()
        self.assertEqual(identity, [1])
        self.assertEqual(destructive, [])

    def test_probe_contract_is_still_exactly_8192_mib(self):
        self.assertEqual(ELIGIBILITY_PROBE_BYTES, 8192 * 1024**2)
        self.assertEqual(VhdxProbeSettings().vhdx_size_bytes, ELIGIBILITY_PROBE_BYTES)
