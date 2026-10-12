import unittest
from dataclasses import replace

from PySide6.QtWidgets import QApplication, QLabel

from test_vhdx_probe import device
from test_vhdx_validation_state import successful_validation
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.services.raid_backend import VhdxStorageSpacesBackend
from usb_array_manager.ui.device_tabs import BackendDeploymentTab


class BackendPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.backend = VhdxStorageSpacesBackend()
        self.plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-13T12:00:00+00:00")
        self.devices = tuple(device(slot, "NTFS") for slot in range(1, 5))
        self.backend.record_together(successful_validation(), self.devices, self.plan)

    def assess(self, devices=None):
        return self.backend.assess(self.plan, devices or self.devices)

    def test_backend_capability_label_appears_once(self):
        tab = BackendDeploymentTab()
        assessment = self.assess()
        tab.set_assessments({assessment.backend_name: assessment}, assessment.backend_name)
        labels = " ".join(label.text() for label in tab.findChildren(QLabel))
        self.assertEqual(labels.count("Backend capability:"), 1)
        self.assertEqual(tab.status.text(), "SUPPORTED")
        self.assertNotIn("Backend capability", tab.explanation.text())
        tab.deleteLater()

    def test_only_fat32_uses_only_filesystem_wording(self):
        devices = tuple(replace(item, filesystem_types=("FAT32",)) for item in self.devices)
        assessment = self.assess(devices)
        self.assertEqual(assessment.explanation,
                         "Production deployment: BLOCKED — host filesystems require migration to NTFS.")
        self.assertNotIn("saved-plan", assessment.explanation)
        self.assertEqual(len(assessment.blockers), 4)

    def test_filesystem_and_saved_plan_blockers_mention_both(self):
        devices = tuple(replace(item, filesystem_types=("FAT32",)) for item in self.devices)
        devices = (replace(devices[0], is_system_disk=True), *devices[1:])
        assessment = self.assess(devices)
        self.assertEqual(assessment.explanation,
                         "Production deployment: BLOCKED — host filesystem and saved-plan validation requirements remain.")
        self.assertTrue(any("system" in blocker for blocker in assessment.blockers))

    def test_saved_plan_blocker_alone_does_not_claim_filesystem_migration(self):
        assessment = self.assess((replace(self.devices[0], is_system_disk=True), *self.devices[1:]))
        self.assertEqual(assessment.explanation,
                         "Production deployment: BLOCKED — saved-plan validation requirements remain.")
        self.assertNotIn("filesystem", assessment.explanation)

    def test_no_blockers_summary_ready_without_changing_assessment(self):
        assessment = self.assess()
        self.assertEqual(assessment.explanation, "Production deployment: READY")
        self.assertEqual(assessment.status, "SUPPORTED")
        self.assertEqual(assessment.blockers, ())
        self.assertEqual(assessment.requirements, ())

    def test_all_planned_operations_headings_have_explicit_separation(self):
        tab = BackendDeploymentTab()
        tab.set_assessment(self.assess())
        text = tab.requirements.toPlainText()
        for slot in range(1, 5):
            self.assertIn(f"Planned operations by drive — Slot {slot}", text)
        self.assertNotIn("driveSlot", text)
        tab.deleteLater()


if __name__ == "__main__":
    unittest.main()
