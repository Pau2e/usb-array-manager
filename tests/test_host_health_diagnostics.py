import base64
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from test_host_preparation import member

from usb_array_manager.services.host_health import describe_health
from usb_array_manager.services.host_preparation import PreparationError, check_target
from usb_array_manager.services.vhdx_probe import ELIGIBILITY_PROBE_BYTES
from usb_array_manager.services.windows_cim import _POWERSHELL_SCRIPT, _record_to_device
from usb_array_manager.ui.host_preparation import HostPreparationPage


def packet(
    health="Warning",
    code=1,
    dirty=True,
    operation="Full Repair Needed",
    operation_code=53263,
):
    return {
        "source": "Get-Volume.HealthStatus",
        "selected_raw": health,
        "storage_match_count": 1,
        "storage": {
            "health_value": health,
            "health_cim_raw": code,
            "operational_value": [operation],
            "operational_cim_raw": [operation_code],
        },
        "dirty_bit_set": dirty,
        "logical_disk_dirty": dirty,
        "win32_status": None,
        "file_system": "FAT32",
        "automount": True,
        "storage_query_error": None,
    }


class HealthProvenanceTests(unittest.TestCase):
    def test_warning_is_selected_windows_health_and_raw_details_survive(self):
        evidence = packet()
        record = {
            "health_status": "OK",
            "health_diagnostics": {
                "source": "Win32_DiskDrive.Status",
                "selected_raw": "OK",
            },
            "host_volumes": [
                {"health_status": "Warning", "health_diagnostics": evidence}
            ],
        }
        device = _record_to_device(record)
        self.assertEqual(device.health_status, "OK")
        self.assertEqual(device.host_volumes[0].health_status, "Warning")
        self.assertEqual(
            json.loads(device.host_volumes[0].health_details_json), evidence
        )
        self.assertEqual(
            json.loads(device.health_details_json)["source"], "Win32_DiskDrive.Status"
        )

    def test_healthy_and_warning_codes_normalize_without_operational_or_dirty_inference(
        self,
    ):
        for raw, expected in (
            (0, "Healthy"),
            ("0", "Healthy"),
            (1, "Warning"),
            ("1", "Warning"),
            (" Warning ", "Warning"),
            ("Healthy", "Healthy"),
        ):
            with self.subTest(raw=raw):
                converted = _record_to_device(
                    {
                        "host_volumes": [
                            {"health_status": raw, "health_diagnostics": packet()}
                        ]
                    }
                )
                self.assertEqual(converted.host_volumes[0].health_status, expected)

    def test_missing_unknown_and_unsupported_are_never_invented_as_warning(self):
        for value in (None, "", 99, "99", "NotSupported", False):
            with self.subTest(value=value):
                converted = _record_to_device(
                    {
                        "host_volumes": [
                            {"health_status": value, "health_diagnostics": packet()}
                        ]
                    }
                )
                normalized = converted.host_volumes[0].health_status
                self.assertNotEqual(normalized, "Warning")
                target = member()
                target = replace(
                    target,
                    host_volumes=(
                        replace(target.host_volumes[0], health_status=normalized),
                    ),
                )
                with self.assertRaises(PreparationError):
                    check_target(1, member(), [target])

    def test_dirty_and_operational_evidence_does_not_override_warning_gate(self):
        target = member()
        target = replace(
            target,
            host_volumes=(
                replace(
                    target.host_volumes[0],
                    health_status="Warning",
                    health_details_json=json.dumps(
                        packet(dirty=False, operation="OK", operation_code=2)
                    ),
                ),
            ),
        )
        with self.assertRaises(PreparationError):
            check_target(1, member(), [target])
        description = describe_health(target, "BLOCKED", "Host volume health Warning")
        self.assertIn(
            "Host volume: normalized='Warning'; health gate=BLOCKED", description
        )
        self.assertIn('"dirty_bit_set": false', description)

    def test_raw_view_explains_selected_source_codes_and_exact_gate(self):
        target = member()
        disk_evidence = {
            "source": "Win32_DiskDrive.Status",
            "selected_raw": "OK",
            "get_disk": {"health_value": "Healthy", "health_cim_raw": 0},
            "physical_candidates": [
                {"size_matches": False, "health": {"health_cim_raw": 0}}
            ],
        }
        target = replace(
            target,
            health_status="OK",
            health_details_json=json.dumps(disk_evidence),
            host_volumes=(
                replace(
                    target.host_volumes[0],
                    health_status="Warning",
                    health_details_json=json.dumps(packet()),
                ),
            ),
        )
        with patch("subprocess.run") as process:
            text = describe_health(target, "BLOCKED", "Host volume health Warning")
            process.assert_not_called()
        for value in (
            "Win32_DiskDrive.Status",
            "Get-Volume.HealthStatus",
            '"health_cim_raw": 1',
            "53263",
            "Full Repair Needed",
            '"dirty_bit_set": true',
            '"size_matches": false',
            "health_status or ''",
            "do not establish media failure",
        ):
            self.assertIn(value, text)

    def test_clean_slot_three_view_retains_dirty_false_and_healthy(self):
        target = member(3)
        evidence = packet("Healthy", 0, False, "OK", 2)
        target = replace(
            target,
            host_volumes=(
                replace(
                    target.host_volumes[0], health_details_json=json.dumps(evidence)
                ),
            ),
        )
        text = describe_health(
            target, "READY FOR PREPARATION", "All existing prechecks passed"
        )
        self.assertIn("Host volume: normalized='Healthy'; health gate=PASS", text)
        self.assertIn('"dirty_bit_set": false', text)
        self.assertIn('"health_cim_raw": 0', text)

    def test_legacy_missing_or_malformed_diagnostics_do_not_invent_provenance(self):
        for details in (None, "not-json", "[]"):
            target = replace(member(), health_details_json=details)
            text = describe_health(
                target, "READY FOR PREPARATION", "Existing prechecks"
            )
            self.assertIn("Raw health provenance unavailable", text)
            self.assertNotIn("Selected source: Get-Volume", text)
        self.assertIsNone(_record_to_device({}).health_details_json)

    def test_inventory_remains_readonly_and_encoded_command_fits_windows_limit(self):
        for forbidden in (
            "Format-Volume",
            "Repair-Volume",
            "Initialize-Disk",
            "Clear-Disk",
            "New-Partition",
            "Invoke-CimMethod",
            "fsutil",
            "chkdsk",
        ):
            self.assertNotIn(forbidden.lower(), _POWERSHELL_SCRIPT.lower())
        self.assertLess(
            len(base64.b64encode(_POWERSHELL_SCRIPT.encode("utf-16-le"))), 32000
        )
        self.assertIn("DirtyBitSet", _POWERSHELL_SCRIPT)
        self.assertIn("VolumeDirty", _POWERSHELL_SCRIPT)
        self.assertIn("-Depth 8", _POWERSHELL_SCRIPT)
        self.assertEqual(ELIGIBILITY_PROBE_BYTES, 8192 * 1024**2)

    def test_health_page_is_readonly_and_has_no_mutating_signal(self):
        app = QApplication.instance() or QApplication([])
        page = HostPreparationPage()
        self.addCleanup(page.close)
        rows = {
            slot: (member(slot), "BLOCKED", "Health Warning") for slot in range(1, 5)
        }
        page.set_state(rows, "0/4 PREPARED")
        calls = []
        page.prepare_requested.connect(calls.append)
        page.set_health_diagnostics({1: "raw Warning / 1; dirty=true; final BLOCKED"})
        page.table.selectRow(0)
        page.detail_tabs.setCurrentWidget(page.health_details)
        page.set_busy(True)
        app.processEvents()
        self.assertTrue(page.health_details.isReadOnly())
        self.assertIn("final BLOCKED", page.health_details.toPlainText())
        self.assertFalse(page.prepare_button.isEnabled())
        self.assertEqual(calls, [])

    def test_health_logs_are_deduplicated_and_diagnostics_never_query_windows(self):
        from test_host_preparation import PreparationWindowTests

        fixture = PreparationWindowTests(
            "test_cancel_confirmation_leaves_member_and_evidence_untouched"
        )
        fixture.setUp()
        try:
            window = fixture.window
            window._health_diagnostic_signatures.clear()
            with (
                patch.object(window, "_log") as log,
                patch("subprocess.run") as process,
            ):
                window._refresh_host_preparation()
                first = sum(
                    call.args[0] == "Host health provenance"
                    for call in log.call_args_list
                )
                window._refresh_host_preparation()
                second = sum(
                    call.args[0] == "Host health provenance"
                    for call in log.call_args_list
                )
                self.assertEqual(first, 4)
                self.assertEqual(second, first)
                process.assert_not_called()
        finally:
            fixture.doCleanups()
