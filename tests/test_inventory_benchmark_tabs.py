import importlib.metadata
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from test_batch_operations import benchmark, device
from usb_array_manager import __version__
from usb_array_manager.services.benchmark_store import BenchmarkStore
from usb_array_manager.services.raid10_plan_store import Raid10PlanStore
from usb_array_manager.services.slot_store import SlotStore
from usb_array_manager.ui.main_window import MainWindow


class InventoryBenchmarkTabTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, store in (
            ("SlotStore", SlotStore(self.root / "slots.json")),
            ("BenchmarkStore", BenchmarkStore(self.root / "benchmarks.json")),
            ("Raid10PlanStore", Raid10PlanStore(self.root / "plan.json")),
        ):
            self.stack.enter_context(patch(f"usb_array_manager.ui.main_window.{name}", return_value=store))
        self.stack.enter_context(patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes))
        self.stack.enter_context(patch.object(QMessageBox, "information"))
        self.stack.enter_context(patch("usb_array_manager.ui.main_window.check_benchmark_target", return_value=20 * 1024**3))
        with patch("usb_array_manager.ui.main_window.QTimer.singleShot"):
            self.window = MainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window._reconciliation_timer.stop()
        self.devices = [device(slot) for slot in range(1, 5)]
        for item in self.devices:
            self.window._slot_store.assign(item, item.slot)
        self.window._show_devices(self.devices)
        self.addCleanup(self.window.close)

    def finish(self):
        deadline = time.monotonic() + 5
        while self.window._storage_job_active() and time.monotonic() < deadline:
            self.app.processEvents()
            threading.Event().wait(0.001)
        self.assertFalse(self.window._storage_job_active())
        self.app.processEvents()

    def test_separate_pages_own_original_panels_and_models(self):
        titles = [self.window._tabs.tabText(index) for index in range(self.window._tabs.count())]
        self.assertEqual(titles, ["Overview", "Benchmarks", "RAID10 Planner", "Failure Simulator", "Backend / Deployment", "Logs / Details"])
        self.assertNotIn("Overview / Benchmarks", titles)
        self.assertIs(self.window._tabs.widget(0), self.window._overview_tab)
        self.assertIs(self.window._tabs.widget(1), self.window._benchmarks_tab)
        self.assertIs(self.window._overview_tab.table.model(), self.window._model)
        self.assertIs(self.window._benchmarks_tab.table.model(), self.window._benchmark_model)

    def test_inventory_and_benchmark_results_render_on_their_tabs(self):
        self.window._benchmark_results = {slot: benchmark(slot) for slot in range(1, 5)}
        self.window._set_benchmark_results_on_models()
        self.window.show()
        self.app.processEvents()
        self.assertTrue(self.window._table.isVisible())
        self.assertFalse(self.window._benchmark_table.isVisible())
        self.window._tabs.setCurrentWidget(self.window._benchmarks_tab)
        self.app.processEvents()
        self.assertTrue(self.window._benchmark_table.isVisible())
        self.assertFalse(self.window._table.isVisible())
        self.assertEqual(self.window._model.rowCount(), 4)
        self.assertEqual(self.window._benchmark_model.rowCount(), 4)
        self.assertEqual(self.window._model.data(self.window._model.index(0, 0)), "Slot 1")
        self.assertEqual(self.window._benchmark_model.data(self.window._benchmark_model.index(0, 2)), "100.0")
        self.assertEqual(self.window._benchmark_model.data(self.window._benchmark_model.index(0, 5)), "GOOD")
        self.assertFalse(self.window._table.isSortingEnabled())
        self.assertFalse(self.window._benchmark_table.isSortingEnabled())

    def test_per_slot_button_calls_existing_benchmark_path(self):
        self.window._tabs.setCurrentWidget(self.window._benchmarks_tab)
        calls = []
        def execute(_root, slot, _letter, **_kwargs):
            calls.append(slot)
            return benchmark(slot)
        with patch("usb_array_manager.ui.main_window.run_benchmark", side_effect=execute):
            self.window._benchmark_buttons[1].click()
            self.finish()
        self.assertEqual(calls, [2])
        self.assertIn(2, self.window._benchmark_results)
        self.assertIs(self.window._tabs.currentWidget(), self.window._benchmarks_tab)

    def test_runs_selector_and_benchmark_all_reuse_sequential_batch(self):
        calls = []
        panel = self.window._benchmarks_tab
        self.assertEqual([panel.batch_runs.itemText(index) for index in range(panel.batch_runs.count())], ["1", "3", "5"])
        panel.batch_runs.setCurrentText("3")
        def execute(_root, slot, _letter, **_kwargs):
            calls.append(slot)
            return benchmark(slot)
        with patch("usb_array_manager.ui.main_window.run_benchmark", side_effect=execute):
            panel.benchmark_all_button.click()
            self.finish()
        self.assertEqual(calls, [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4])
        self.assertTrue(all(item.run_count == 3 for item in self.window._benchmark_results.values()))
        self.assertTrue(panel.batch_runs.isEnabled())
        self.assertTrue(panel.benchmark_all_button.isEnabled())

    def test_selection_and_assignment_are_still_connected(self):
        self.window._slot_store = SlotStore(self.root / "fresh_slots.json")
        self.window._show_devices(self.devices)
        self.window._table.selectRow(0)
        self.app.processEvents()
        self.assertTrue(self.window._assign_slot_button.isEnabled())
        with patch.object(QInputDialog, "getItem", return_value=("Slot 1", True)):
            self.window._assign_slot_button.click()
        self.assertEqual(self.window._slot_store.reconcile(self.devices)[0].slot, 1)
        self.assertIn("Saved as Slot 1", self.window._status.text())
        self.assertEqual(len(self.window._benchmark_buttons), 1)
        self.assertTrue(self.window._benchmark_buttons[0].isEnabled())

    def test_separate_tabs_resize_and_keep_controls_accessible(self):
        for width, height in ((1280, 720), (1366, 768), (1050, 650)):
            with self.subTest(width=width):
                self.window.resize(width, height)
                self.window.show()
                self.app.processEvents()
                self.window._tabs.setCurrentWidget(self.window._overview_tab)
                self.app.processEvents()
                self.assertTrue(self.window._assign_slot_button.isVisible())
                self.assertGreater(self.window._table.viewport().height(), 200)
                self.window._tabs.setCurrentWidget(self.window._benchmarks_tab)
                self.app.processEvents()
                self.assertTrue(self.window._benchmarks_tab.benchmark_all_button.isVisible())
                action_column = self.window._benchmark_model.benchmark_action_column()
                self.assertEqual(self.window._benchmark_table.horizontalHeader().visualIndex(action_column), action_column)
                self.assertGreater(self.window._benchmark_table.viewport().height(), 200)
        self.window._tabs.setCurrentWidget(self.window._overview_tab)
        self.window._table.selectRow(2)
        self.window.resize(1280, 720)
        self.app.processEvents()
        self.assertEqual(self.window._table.selectionModel().selectedRows()[0].row(), 2)
        self.assertTrue(self.window._assign_slot_button.isEnabled())

    def test_current_runtime_package_and_project_versions_match(self):
        self.assertEqual(__version__, "0.8.0")
        self.assertIn("USB Array Manager 0.8.0", self.window.windowTitle())
        self.assertEqual(importlib.metadata.version("usb-array-manager"), "0.8.0")
        project = Path(__file__).resolve().parents[1]
        self.assertEqual(tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"], "0.8.0")
        self.assertNotIn("0.7.5.2", self.window.windowTitle())

    def test_switching_tabs_preserves_results_slots_selection_and_models(self):
        self.window._benchmark_results = {slot: benchmark(slot) for slot in range(1, 5)}
        for result in self.window._benchmark_results.values():
            self.window._benchmark_store.save(result)
        self.window._set_benchmark_results_on_models()
        inventory_model = self.window._model
        benchmark_model = self.window._benchmark_model
        results = self.window._benchmark_results
        slots = tuple(item.slot for item in inventory_model.devices())
        persisted_slots = self.window._slot_store.path.read_bytes()
        self.window._table.selectRow(1)
        for _ in range(3):
            self.window._tabs.setCurrentWidget(self.window._benchmarks_tab)
            self.app.processEvents()
            self.assertIs(self.window._benchmark_model, benchmark_model)
            self.assertIs(self.window._benchmark_results, results)
            self.assertEqual(benchmark_model.data(benchmark_model.index(1, 2)), "100.0")
            self.window._tabs.setCurrentWidget(self.window._overview_tab)
            self.app.processEvents()
            self.assertIs(self.window._model, inventory_model)
            self.assertEqual(tuple(item.slot for item in inventory_model.devices()), slots)
            self.assertEqual(self.window._table.selectionModel().selectedRows()[0].row(), 1)
        self.assertEqual(self.window._benchmark_store.load_all(), results)
        self.assertEqual(self.window._slot_store.path.read_bytes(), persisted_slots)


if __name__ == "__main__":
    unittest.main()
