import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from test_host_preparation import member
from test_host_health_diagnostics import packet

from usb_array_manager.services.host_health import (
    DIRTY_FORMAT_REVIEW,
    dirty_format_exception,
)
from usb_array_manager.services.host_preparation import (
    HostPreparationService,
    PreparationConfirmation,
    PreparationError,
    PreparationStore,
    check_target,
)
from usb_array_manager.services.slot_store import SlotStore
from usb_array_manager.ui.host_preparation import (
    HostConfirmationDialog,
    HostPreparationPage,
)


def dirty_member():
    target = member()
    return replace(
        target,
        health_details_json=json.dumps(
            {
                "source": "Win32_DiskDrive.Status",
                "selected_raw": "OK",
                "get_disk": {
                    "health_value": "Healthy",
                    "health_cim_raw": 0,
                    "operational_value": ["Online"],
                    "operational_cim_raw": [53264],
                },
                "get_disk_error": None,
            }
        ),
        host_volumes=(
            replace(
                target.host_volumes[0],
                health_status="Warning",
                health_details_json=json.dumps(packet()),
            ),
        ),
    )


class DirtyFilesystemPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = dirty_member()
        self.slots = SlotStore(self.root / "slots.json")
        self.slots.assign(self.target, 1)
        self.store = PreparationStore(self.root / "prep.json")
        self.formatter = Mock()
        self.service = HostPreparationService(
            self.slots,
            self.store,
            detector=lambda: [self.target],
            formatter=self.formatter,
            elevated=lambda: True,
            protected=(),
            sleep=lambda _: None,
        )

    def test_dirty_pattern_enables_review_only_and_does_not_write_or_execute(self):
        before = self.slots.path.read_bytes()
        with patch("subprocess.run") as process:
            self.assertTrue(dirty_format_exception(self.target))
            self.assertEqual(
                self.service.status(1, [self.target])[0], DIRTY_FORMAT_REVIEW
            )
            self.assertEqual(check_target(1, self.target, [self.target]), self.target)
            process.assert_not_called()
        self.assertEqual(self.target.host_volumes[0].health_status, "Warning")
        self.assertEqual(before, self.slots.path.read_bytes())
        self.assertFalse(self.store.path.exists())
        self.formatter.format.assert_not_called()
        with self.assertRaises(PreparationError):
            check_target(1, self.target, [self.target], filesystems=("NTFS",))

    def test_separate_ack_is_required_even_with_backup_and_phrase(self):
        for ack in (False, None, 1, "yes"):
            with self.subTest(ack=ack), self.assertRaises(PreparationError):
                self.service.prepare(
                    PreparationConfirmation(self.target, True, "FORMAT SLOT 1", ack)
                )
        self.formatter.format.assert_not_called()
        self.assertFalse(self.store.path.exists())

    def test_unknown_conflicting_and_missing_volume_evidence_blocks(self):
        for field, value in (
            ("dirty_bit_set", None),
            ("dirty_bit_set", False),
            ("logical_disk_dirty", None),
            ("logical_disk_dirty", False),
            ("storage_match_count", 2),
            ("source", "Win32_Volume.Status"),
            ("automount", None),
            ("storage_query_error", "failed"),
            ("storage", None),
            ("storage", []),
        ):
            data = packet()
            data[field] = value
            current = replace(
                self.target,
                host_volumes=(
                    replace(
                        self.target.host_volumes[0],
                        health_details_json=json.dumps(data),
                    ),
                ),
            )
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(PreparationError),
            ):
                check_target(1, self.target, [current])
        for field, value in (
            ("health_value", "Unknown"),
            ("health_cim_raw", None),
            ("operational_value", ["OK"]),
            ("operational_cim_raw", [2]),
            ("operational_value", ["Full Repair Needed", "Degraded"]),
        ):
            data = packet()
            data["storage"][field] = value
            current = replace(
                self.target,
                host_volumes=(
                    replace(
                        self.target.host_volumes[0],
                        health_details_json=json.dumps(data),
                    ),
                ),
            )
            with self.subTest(field=field), self.assertRaises(PreparationError):
                check_target(1, self.target, [current])

    def test_physical_health_and_all_exclusions_still_block(self):
        for field, value in (
            ("health_status", "Warning"),
            ("health_status", None),
            ("is_system_disk", True),
            ("is_boot_disk", True),
            ("is_system_disk", None),
            ("is_read_only", True),
        ):
            with self.subTest(field=field), self.assertRaises(PreparationError):
                check_target(1, self.target, [replace(self.target, **{field: value})])
        for raw_health in (None, "Warning", "Unknown"):
            evidence = json.loads(self.target.health_details_json)
            evidence["get_disk"]["health_value"] = raw_health
            current = replace(self.target, health_details_json=json.dumps(evidence))
            with self.subTest(raw=raw_health), self.assertRaises(PreparationError):
                check_target(1, self.target, [current])
        with self.assertRaises(PreparationError):
            check_target(
                1,
                self.target,
                [self.target],
                protected=(self.target.drive_letters[0] + "\\app\\state.json",),
            )
        with self.assertRaises(PreparationError):
            check_target(1, self.target, [self.target, self.target])
        with self.assertRaises(PreparationError):
            check_target(
                1, self.target, [replace(self.target, serial_number="SWAPPED")]
            )

    def test_changed_health_since_review_cannot_dispatch(self):
        confirmation = PreparationConfirmation(self.target, True, "FORMAT SLOT 1", True)
        changed = replace(member(), health_details_json=self.target.health_details_json)
        self.service.detector = lambda: [changed]
        with self.assertRaises(PreparationError):
            self.service.prepare(confirmation)
        self.formatter.format.assert_not_called()
        self.assertFalse(self.store.path.exists())

    def test_clean_label_with_unknown_or_conflicting_raw_health_blocks(self):
        for change in (
            {"dirty_bit_set": None},
            {"dirty_bit_set": True},
            {"storage_match_count": 2},
            {"storage": {"health_value": "Unknown"}},
        ):
            data = packet("Healthy", 0, False, "OK", 2)
            data.update(change)
            target = member()
            target = replace(
                target,
                host_volumes=(
                    replace(
                        target.host_volumes[0], health_details_json=json.dumps(data)
                    ),
                ),
            )
            with self.subTest(change=change), self.assertRaises(PreparationError):
                check_target(1, member(), [target])

    def test_readonly_recheck_keeps_dirty_status_and_never_marks_prepared(self):
        self.service.recheck(1)
        self.assertEqual(
            self.store.load()["members"]["1"]["status"], DIRTY_FORMAT_REVIEW
        )
        self.formatter.format.assert_not_called()

    def test_clean_to_dirty_requires_new_review_and_ack(self):
        with self.assertRaises(PreparationError):
            self.service.prepare(
                PreparationConfirmation(member(), True, "FORMAT SLOT 1")
            )
        self.formatter.format.assert_not_called()

    def test_explicit_ack_is_recorded_and_only_fake_formatter_runs(self):
        self.service.detector = Mock(
            side_effect=[[self.target], [member(filesystem="NTFS")]]
        )
        messages = []
        with patch("subprocess.run") as process:
            result = self.service.prepare(
                PreparationConfirmation(self.target, True, "FORMAT SLOT 1", True),
                messages.append,
            )
            process.assert_not_called()
        self.assertEqual(result.host_volumes[0].filesystem, "NTFS")
        self.formatter.format.assert_called_once()
        self.assertTrue(
            self.store.load()["members"]["1"]["confirmation"][
                "dirty_filesystem_acknowledged"
            ]
        )
        self.assertTrue(
            any("separate acknowledgement accepted" in message for message in messages)
        )

    def test_dialog_ack_unchecked_and_required_and_dirty_count_is_not_prepared(self):
        app = QApplication.instance() or QApplication([])
        dialog = HostConfirmationDialog(self.target)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.dirty_ack.isChecked())
        dialog.backup.setChecked(True)
        dialog.phrase.setText("FORMAT SLOT 1")
        self.assertFalse(dialog.format_button.isEnabled())
        dialog.dirty_ack.setChecked(True)
        self.assertTrue(dialog.format_button.isEnabled())
        dialog.confirmation().check()
        dialog.dirty_ack.setChecked(False)
        self.assertFalse(dialog.format_button.isEnabled())
        page = HostPreparationPage()
        self.addCleanup(page.close)
        rows = {slot: (member(slot), "BLOCKED", "Not selected") for slot in range(1, 5)}
        rows[1] = (self.target, DIRTY_FORMAT_REVIEW, "Extra acknowledgement required")
        page.set_state(rows, "0/4 PREPARED")
        page.table.selectRow(0)
        app.processEvents()
        self.assertTrue(page.prepare_button.isEnabled())
        self.assertFalse(page.validate_button.isEnabled())
