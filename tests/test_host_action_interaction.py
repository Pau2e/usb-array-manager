import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from PySide6.QtCore import QItemSelectionModel, QObject, QTimer, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_host_preparation import member

from usb_array_manager.ui.host_preparation import HostPreparationPage


class HostSelectionInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = HostPreparationPage()
        self.addCleanup(self.page.close)
        self.rows = {
            slot: (member(slot), "READY FOR PREPARATION", "Ready")
            for slot in range(1, 5)
        }
        self.rows[2] = (member(2), "BLOCKED", "Identity unproven")
        self.page.set_state(self.rows, "0/4 PREPARED")
        self.page.set_identity_diagnostics({1: "first", 2: "second"}, {1})

    def test_selected_rows_override_stale_current_cell_and_clear_disables(self):
        table = self.page.table
        table.selectRow(0)
        self.assertTrue(self.page.prepare_button.isEnabled())
        table.selectionModel().select(
            table.model().index(1, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.assertEqual(table.currentRow(), 0)  # Deliberately stale current index.
        self.assertEqual(self.page.selected_slot(), 2)
        self.assertFalse(self.page.prepare_button.isEnabled())
        self.assertFalse(self.page.identity_button.isEnabled())
        self.assertIn("second", self.page.identity_details.toPlainText())
        table.clearSelection()
        self.assertEqual(self.page.selected_slot(), 0)
        self.assertFalse(self.page.recheck_button.isEnabled())

    def test_mouse_keyboard_selection_and_refresh_preserve_correct_slot(self):
        self.page.resize(1100, 700)
        self.page.show()
        self.app.processEvents()
        table = self.page.table
        rect = table.visualItemRect(table.item(0, 0))
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(self.page.selected_slot(), 1)
        self.assertTrue(self.page.identity_button.isEnabled())
        QTest.keyClick(table, Qt.Key.Key_Down)
        self.assertEqual(self.page.selected_slot(), 2)
        self.assertFalse(self.page.prepare_button.isEnabled())
        existing = table.item(1, 0)
        self.page.set_state(self.rows, "Updated inventory")
        self.assertIs(table.item(1, 0), existing)
        self.assertEqual(self.page.selected_slot(), 2)
        received = []
        self.page.recheck_requested.connect(received.append)
        QTest.mouseClick(self.page.recheck_button, Qt.MouseButton.LeftButton)
        self.assertEqual(received, [2])

    def test_scanning_busy_and_validation_eligibility_are_independent(self):
        self.page.table.selectRow(0)
        self.page.set_scanning(True)
        for button in self.page._action_buttons.values():
            self.assertFalse(button.isEnabled())
        self.assertIn("Refreshing inventory", self.page.availability.text())
        self.page.set_busy(True)
        self.page.set_scanning(False)
        self.assertFalse(self.page.recheck_button.isEnabled())
        self.page.set_busy(False)
        self.assertTrue(self.page.recheck_button.isEnabled())
        self.assertFalse(self.page.validate_button.isEnabled())
        prepared = {
            slot: (member(slot, "NTFS"), "PREPARED", "Proven") for slot in range(1, 5)
        }
        for count in range(4):
            rows = {
                slot: prepared[slot] if slot <= count else self.rows[slot]
                for slot in range(1, 5)
            }
            self.page.set_state(rows, f"{count}/4 PREPARED")
            self.assertFalse(self.page.validate_button.isEnabled())
        self.page.set_state(prepared, "4/4 PREPARED")
        self.assertTrue(self.page.validate_button.isEnabled())
        self.page.set_scanning(True)
        self.assertFalse(self.page.validate_button.isEnabled())


class HostActionWorkerInteractionTests(unittest.TestCase):
    def setUp(self):
        from test_host_preparation import PreparationWindowTests

        self.fixture = PreparationWindowTests(
            "test_cancel_confirmation_leaves_member_and_evidence_untouched"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.window = self.fixture.window
        self.page = self.window._backend_tab.host_preparation
        self.page.table.selectRow(0)
        self.release = threading.Event()
        self.addCleanup(self._finish_worker)

    def _finish_worker(self):
        self.release.set()
        self.fixture.finish()

    def test_one_mouse_click_starts_once_immediate_feedback_and_slot_snapshot(self):
        for identity in (False, True):
            with self.subTest(identity=identity):
                self.release.clear()
                self.page.table.selectRow(0)
                self.page.set_identity_diagnostics({}, {1})

                def work(slot, log):
                    if not self.release.wait(3):
                        raise RuntimeError("Test worker timed out")
                    return member(slot)

                method = "record_identity" if identity else "recheck"
                with patch.object(
                    self.fixture.service, method, side_effect=work
                ) as action:
                    button = (
                        self.page.identity_button
                        if identity
                        else self.page.recheck_button
                    )
                    original_text = button.text()
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    self.assertIsNotNone(self.window._host_thread)
                    self.assertEqual(self.window._host_thread.slot, 1)
                    self.assertIn(
                        "Recording" if identity else "Rechecking",
                        self.page.progress.text(),
                    )
                    self.assertFalse(button.isEnabled())
                    self.assertNotEqual(button.text(), original_text)
                    heartbeat = []
                    QTimer.singleShot(0, lambda: heartbeat.append(True))
                    self.fixture.app.processEvents()
                    self.assertEqual(heartbeat, [True])
                    self.assertTrue(self.window._host_thread.isRunning())
                    # Another row cannot retarget the already-created worker.
                    self.page.table.selectRow(2)
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    self.window._record_host_identity(1)
                    self.window._recheck_host(1)
                    self.assertIn("rejected", self.page.progress.text())
                    self._finish_worker()
                    action.assert_called_once()
                    self.assertEqual(action.call_args.args[0], 1)
                    self.assertIn("completed successfully", self.page.progress.text())
                    self.assertEqual(button.text(), original_text)
                    self.assertFalse(self.page.validate_button.isEnabled())
                    self.page.table.selectRow(0)
                    self.assertTrue(self.page.recheck_button.isEnabled())
        self.fixture.formatter.format.assert_not_called()

    def test_scan_start_and_finish_disable_then_enable_without_an_extra_click(self):
        from usb_array_manager.ui.main_window import MainWindow

        with patch("usb_array_manager.ui.main_window.DeviceScanThread") as scan:
            MainWindow.refresh_devices(self.window)
            self.assertTrue(self.page._scanning)
            self.assertFalse(self.page.recheck_button.isEnabled())
            self.window._recheck_host(1)
            self.assertIn("Inventory is refreshing", self.page.progress.text())
            self.assertIsNone(self.window._host_thread)
            self.window._scan_finished()
            self.assertFalse(self.page._scanning)
            self.assertTrue(self.page.recheck_button.isEnabled())
            scan.return_value.start.assert_called_once()
        with patch.object(
            self.fixture.service, "recheck", return_value=member()
        ) as action:
            QTest.mouseClick(self.page.recheck_button, Qt.MouseButton.LeftButton)
            self._finish_worker()
            action.assert_called_once()

    def test_failed_worker_shows_failure_and_restores_controls(self):
        with (
            patch.object(
                self.fixture.service,
                "recheck",
                side_effect=RuntimeError("Read-only query failed"),
            ),
            patch("usb_array_manager.ui.main_window.QMessageBox.warning"),
        ):
            self.page.recheck_button.click()
            self._finish_worker()
        self.assertIn("Read-only query failed", self.page.progress.text())
        self.assertIn("FAILED", self.page.progress.text())
        self.assertTrue(self.page.recheck_button.isEnabled())
        self.assertIsNone(self.page._active_action)
        self.fixture.formatter.format.assert_not_called()

    def test_thread_start_failure_restores_controls_and_shows_reason(self):
        with patch("usb_array_manager.ui.main_window.HostPreparationThread") as thread:
            thread.return_value.start.side_effect = RuntimeError("Cannot start worker")
            self.page.recheck_button.click()
        self.assertIn("Cannot start worker", self.page.progress.text())
        self.assertTrue(self.page.recheck_button.isEnabled())
        self.assertFalse(self.window._storage_job_active())

    def test_postformat_validation_handler_also_rejects_unprepared_hosts(self):
        with patch.object(self.window, "_start_vhdx_together") as validation:
            self.window._start_host_validation()
            validation.assert_not_called()
        self.assertIn("all four hosts must be PREPARED", self.page.progress.text())
        self.assertFalse(self.page.validate_button.isEnabled())

    def test_validation_cancellation_restores_prepared_host_button(self):
        devices = [member(slot, "NTFS") for slot in range(1, 5)]
        for item in devices:
            self.window._slot_store.assign(item, item.slot)
        self.fixture.service.detector = lambda: devices
        for slot in range(1, 5):
            self.fixture.service.recheck(slot)
        self.window._show_devices(devices)
        self.assertTrue(self.page.validate_button.isEnabled())
        with patch.object(self.window, "_start_vhdx_together") as validation:
            self.page.validate_button.click()
            validation.assert_called_once()
        self.assertIn("cancelled", self.page.progress.text())
        self.assertTrue(self.page.validate_button.isEnabled())
        self.assertFalse(self.window._storage_job_active())

    def test_validation_completion_and_failure_restore_button_without_retry(self):
        class ValidationSignals(QObject):
            validation_finished = Signal(object)
            validation_failed = Signal(str)
            finished = Signal()

        devices = [member(slot, "NTFS") for slot in range(1, 5)]
        for item in devices:
            self.window._slot_store.assign(item, item.slot)
        self.fixture.service.detector = lambda: devices
        for slot in range(1, 5):
            self.fixture.service.recheck(slot)
        self.window._show_devices(devices)
        for failure in (False, True):
            thread = ValidationSignals(self.window)

            def start():
                self.window._together_thread = thread
                thread.finished.connect(self.window._vhdx_together_finished)

            with patch.object(
                self.window, "_start_vhdx_together", side_effect=start
            ) as validation:
                self.page.validate_button.click()
                self.assertFalse(self.page.validate_button.isEnabled())
                self.assertIn("Running post-format", self.page.progress.text())
                self.window._start_host_validation()
                validation.assert_called_once()
                if failure:
                    thread.validation_failed.emit("Synthetic validation failure")
                else:
                    thread.validation_finished.emit(SimpleNamespace(status="READY"))
                thread.finished.emit()
            self.assertIn("failure" if failure else "READY", self.page.progress.text())
            self.assertTrue(self.page.validate_button.isEnabled())
            self.assertFalse(self.window._storage_job_active())
