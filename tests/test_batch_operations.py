import tempfile
import threading
import time
import unittest
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHeaderView, QMessageBox

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.raid_backend import SUPPORTED, UNKNOWN
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import (
    VhdxBenchmarkComparison,
    VhdxDiskInfo,
    VhdxProbeResult,
    VhdxTogetherMember,
    VhdxTogetherResult,
)
from usb_array_manager.services.batch_results import (
    average_benchmark_results,
    average_vhdx_comparisons,
)
from usb_array_manager.services.benchmark import BenchmarkError, BenchmarkSettings, _WriteMetrics
from usb_array_manager.services.benchmark_store import BenchmarkStore
from usb_array_manager.services.raid10_plan_store import Raid10PlanStore
from usb_array_manager.services.slot_store import SlotStore
from usb_array_manager.services.vhdx_probe import VhdxProbeSettings, probe_vhdx_backend
from usb_array_manager.ui.main_window import MainWindow


def device(slot):
    return StorageDevice(
        model=f"USB {slot}", serial_number=f"SERIAL-{slot}",
        capacity_bytes=32 * 1024**3, drive_letters=(f"{chr(71 + slot)}:",),
        device_path=rf"\\.\PHYSICALDRIVE{slot + 4}",
        pnp_device_id=f"USBSTOR\\DEVICE-{slot}", usb_vid="1234", usb_pid="5678",
        is_connected=True, slot=slot, storage_unique_id=f"UNIQUE-{slot}",
        health_status="Healthy", bus_type="USB", is_removable=True,
        is_boot_disk=False, is_system_disk=False, is_read_only=False, is_offline=False,
        filesystem_types=("NTFS",),
    )


def benchmark(slot=1, value=100):
    return BenchmarkResult(
        slot=slot, tested_at="2026-10-13T10:00:00+00:00", drive_letter=f"{chr(71 + slot)}:",
        file_size_bytes=1024**3, sequential_read_mbps=value,
        sequential_write_mbps=value / 2, burst_write_mbps=value / 2,
        sustained_write_mbps=value / 4, write_stability_cv=0.1, qualification="GOOD",
    )


def probe_result(slot, comparison=None, **changes):
    return replace(VhdxProbeResult(
        slot=slot, status=SUPPORTED, vhdx_path=f"slot{slot}.vhdx",
        attach_status="Attached; detached", virtual_disk=VhdxDiskInfo(
            42, "VHDX", "File Backed Virtual", "Unspecified", True, "OK", "Healthy",
        ), cleanup_status="Detached and temporary file deleted", filesystem="NTFS",
        explanation="Success", source_identity=f"UNIQUE-{slot}", benchmark=comparison,
    ), **changes)


class BatchResultsTests(unittest.TestCase):
    def test_three_run_arithmetic_mean_and_round_trip(self):
        result = average_benchmark_results([benchmark(value=value) for value in (100, 200, 300)])
        self.assertEqual(result.sequential_read_mbps, 200)
        self.assertEqual(result.sequential_write_mbps, 100)
        self.assertEqual(result.sustained_write_mbps, 50)
        self.assertEqual(result.run_count, 3)
        self.assertEqual(len(result.individual_runs), 3)
        with tempfile.TemporaryDirectory() as directory:
            store = BenchmarkStore(Path(directory) / "results.json")
            store.save(result)
            self.assertEqual(store.load_all()[1], result)

    def test_old_unbuffered_result_defaults_to_one_run(self):
        data = benchmark().to_dict()
        data.pop("run_count")
        data.pop("individual_runs")
        loaded = BenchmarkResult.from_dict(data)
        self.assertEqual(loaded.run_count, 1)
        self.assertEqual(loaded.individual_runs, ())

    def test_vhdx_mean_includes_mean_of_per_run_overheads(self):
        items = [
            VhdxBenchmarkComparison(100, 50, 40, 90, 40, 20),
            VhdxBenchmarkComparison(200, 100, 80, 100, 90, 72),
            VhdxBenchmarkComparison(300, 150, 120, 240, 150, 120),
        ]
        result = average_vhdx_comparisons(items)
        self.assertEqual(result.direct_read_mbps, 200)
        self.assertEqual(result.direct_write_mbps, 100)
        self.assertEqual(result.direct_sustained_write_mbps, 80)
        self.assertAlmostEqual(result.vhdx_read_mbps, 430 / 3)
        self.assertAlmostEqual(result.vhdx_write_mbps, 280 / 3)
        self.assertAlmostEqual(result.vhdx_sustained_write_mbps, 212 / 3)
        self.assertAlmostEqual(result.read_overhead_percent, -80 / 3)
        self.assertAlmostEqual(result.write_overhead_percent, -10)
        self.assertAlmostEqual(result.sustained_write_overhead_percent, -20)


class BatchOperationTests(unittest.TestCase):
    def setUp(self):
        self.application = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        stores = {
            "SlotStore": SlotStore(self.root / "slots.json"),
            "BenchmarkStore": BenchmarkStore(self.root / "benchmarks.json"),
            "Raid10PlanStore": Raid10PlanStore(self.root / "plan.json"),
        }
        for name, store in stores.items():
            self.stack.enter_context(patch(f"usb_array_manager.ui.main_window.{name}", return_value=store))
        self.stack.enter_context(patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes))
        self.information = self.stack.enter_context(patch.object(QMessageBox, "information"))
        self.stack.enter_context(patch.object(QMessageBox, "warning"))
        self.stack.enter_context(patch.object(QMessageBox, "critical"))
        self.stack.enter_context(patch("usb_array_manager.ui.main_window.check_benchmark_target", return_value=20 * 1024**3))
        with patch("usb_array_manager.ui.main_window.QTimer.singleShot"):
            self.window = MainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window._reconciliation_timer.stop()
        self.devices = [device(slot) for slot in range(1, 5)]
        for item in self.devices:
            stores["SlotStore"].assign(item, item.slot)
        self.window._show_devices(self.devices)

    def tearDown(self):
        if self.window._storage_job_active():
            self.window.close()
            self.wait_until(lambda: not self.window._storage_job_active())
        self.window.close()
        self.application.processEvents()
        self.stack.close()
        self.temp.cleanup()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.application.processEvents()
            threading.Event().wait(0.001)
        self.assertTrue(predicate(), "Batch did not finish: " + self.window._logs_tab.output.toPlainText())

    def finish(self):
        self.wait_until(lambda: not self.window._storage_job_active())

    def qualify_all(self):
        for item in self.devices:
            result = probe_result(item.slot)
            self.window._vhdx_backend.record_probe(result)
            self.window._backend_tab.set_probe_result(result)

    def together_result(self):
        members = tuple(VhdxTogetherMember(
            slot=item.slot, vhdx_path=f"T:/temporary/slot{item.slot}.vhdx",
            disk=replace(probe_result(item.slot).virtual_disk, disk_number=item.slot + 7),
            identity_verified=True, status="READY", reason="",
            cleanup_status="Detached and temporary file deleted",
        ) for item in self.devices)
        return VhdxTogetherResult("READY", "4/4 VHDX members READY simultaneously", members, True)

    def test_four_member_validation_control_uses_shared_busy_state(self):
        result = self.together_result()
        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", return_value=result) as validate, patch("usb_array_manager.ui.main_window.run_benchmark") as benchmark_run:
            self.window._backend_tab.together_button.click()
            self.assertFalse(self.window._benchmarks_tab.benchmark_all_button.isEnabled())
            self.assertFalse(self.window._backend_tab.together_button.isEnabled())
            self.assertFalse(self.window._backend_tab.probe_all_button.isEnabled())
            self.window._start_benchmark_batch(1)
            self.finish()
        self.assertEqual(validate.call_count, 1)
        benchmark_run.assert_not_called()
        self.assertEqual(self.window._backend_tab.together_table.rowCount(), 4)
        self.assertEqual(self.window._backend_tab.together_table.item(0, 6).text(), "READY")
        self.assertIn("4/4 VHDX members READY simultaneously", self.window._backend_tab.batch_summary.text())
        self.assertTrue(self.window._backend_tab.together_button.isEnabled())
        self.assertFalse(self.window._backend_tab.performance_all_button.isEnabled())

    def test_four_member_cleanup_failure_blocks_new_vhdx_operations(self):
        result = self.together_result()
        member = replace(result.members[1], status="CLEANUP FAILED", leftover_path=result.members[1].vhdx_path, may_remain_attached=True, cleanup_status="Detach failed")
        result = replace(result, status="FAILED", members=(result.members[0], member, *result.members[2:]))
        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", return_value=result), patch("usb_array_manager.ui.main_window.probe_vhdx_backend") as probe:
            self.window._start_vhdx_together()
            self.finish()
            self.window._start_vhdx_probe(1)
            self.window._start_vhdx_batch(performance=False, runs=1)
        probe.assert_not_called()
        self.assertIn(member.leftover_path, self.window._together_cleanup_blocker)
        self.assertIn("Slot 2", self.window._backend_tab.batch_summary.text())

    def test_simultaneous_ready_does_not_override_fat32_production_blockers(self):
        self.devices = [replace(item, filesystem_types=("FAT32",)) for item in self.devices]
        self.window._show_devices(self.devices)
        self.qualify_all()
        self.window._saved_raid10_plan = self.window._raid10_plan_store.save(((1, 2), (3, 4)))
        self.window._rebuild_raid_workspace()
        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", return_value=self.together_result()):
            self.window._start_vhdx_together()
            self.finish()
        self.assertIn("READY simultaneously", self.window._backend_tab.batch_summary.text())
        self.assertEqual(self.window._backend_tab.status.text(), "SUPPORTED")
        self.assertIn("BLOCKED", self.window._backend_tab.production_readiness.status.text())
        self.assertIn("FAT32", self.window._backend_tab.requirements.toPlainText())

    def test_four_member_success_updates_planning_requirement_and_later_failure_restores_it(self):
        self.devices = [replace(item, filesystem_types=("FAT32",)) for item in self.devices]
        self.window._show_devices(self.devices)
        self.window._saved_raid10_plan = self.window._raid10_plan_store.save(((1, 2), (3, 4)))
        self.window._rebuild_raid_workspace()
        tab = self.window._backend_tab
        tab.backend_selector.setCurrentText("VHDX-backed Storage Spaces")
        self.assertIn("Probe one selected USB slot successfully", tab.requirements.toPlainText())
        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", return_value=self.together_result()):
            self.window._start_vhdx_together()
            self.finish()
        self.assertNotIn("Probe one selected USB slot successfully", tab.requirements.toPlainText())
        assessment = self.window._vhdx_backend.assess(self.window._saved_raid10_plan, self.window._model.devices())
        self.assertEqual(len(assessment.blockers), 4)
        self.assertTrue(all("FAT32" in blocker for blocker in assessment.blockers))
        self.assertEqual(tab.status.text(), "SUPPORTED")
        self.assertIn("BLOCKED", tab.production_readiness.status.text())
        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", return_value=replace(self.together_result(), status="FAILED")):
            self.window._start_vhdx_together()
            self.finish()
        self.assertIn("Probe one selected USB slot successfully", tab.requirements.toPlainText())

    def test_unexpected_four_member_failure_invalidates_previous_planning_evidence(self):
        self.window._saved_raid10_plan = self.window._raid10_plan_store.save(((1, 2), (3, 4)))
        self.window._rebuild_raid_workspace()
        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", return_value=self.together_result()):
            self.window._start_vhdx_together()
            self.finish()
        self.window._vhdx_together_failed("Injected worker failure")
        self.assertIn("Probe one selected USB slot successfully", self.window._backend_tab.requirements.toPlainText())

    def test_window_close_waits_for_four_member_worker_cleanup(self):
        started = threading.Event()
        release = threading.Event()
        cancelled = []

        def validate(_devices, **kwargs):
            started.set()
            release.wait(2)
            cancelled.append(kwargs["cancel_event"].is_set())
            return replace(self.together_result(), status="CANCELLED")

        with patch("usb_array_manager.ui.vhdx_together_worker.validate_vhdxs_together", side_effect=validate):
            self.window._start_vhdx_together()
            self.wait_until(started.is_set)
            self.window.close()
            self.assertTrue(self.window._storage_job_active())
            release.set()
            self.finish()
        self.assertEqual(cancelled, [True])

    def test_benchmark_all_four_slots_is_sequential_and_saves_means(self):
        calls = []
        counts = defaultdict(int)
        active = 0
        peak = 0
        lock = threading.Lock()

        def execute(_root, slot, _letter, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                counts[slot] += 1
                calls.append((slot, counts[slot]))
            kwargs["progress"](50, "Testing")
            threading.Event().wait(0.003)
            result = benchmark(slot, counts[slot] * 100)
            with lock:
                active -= 1
            return result

        with patch("usb_array_manager.ui.main_window.run_benchmark", side_effect=execute):
            self.window._start_benchmark_batch(3)
            self.assertFalse(self.window._benchmarks_tab.benchmark_all_button.isEnabled())
            self.assertFalse(self.window._backend_tab.probe_button.isEnabled())
            self.assertTrue(all(not button.isEnabled() for button in self.window._benchmark_buttons))
            self.assertFalse(self.window._raid10_workspace.planner_page.isEnabled())
            self.finish()
        self.assertEqual(calls, [(slot, run) for slot in range(1, 5) for run in range(1, 4)])
        self.assertEqual(peak, 1)
        results = self.window._benchmark_store.load_all()
        self.assertEqual(set(results), {1, 2, 3, 4})
        for result in results.values():
            self.assertEqual(result.sequential_read_mbps, 200)
            self.assertEqual(result.run_count, 3)
        self.assertEqual(self.window._benchmark_model.data(self.window._benchmark_model.index(0, 7)), "3")
        self.assertTrue(self.window._benchmarks_tab.benchmark_all_button.isEnabled())
        self.assertTrue(all(button.isEnabled() for button in self.window._benchmark_buttons))
        self.assertIn("run 3/3", self.window._logs_tab.output.toPlainText())

    def test_benchmark_failed_slot_continues_to_remaining_slots(self):
        calls = []

        def execute(_root, slot, _letter, **_kwargs):
            calls.append(slot)
            if slot == 2:
                raise BenchmarkError("Drive unavailable")
            return benchmark(slot)

        with patch("usb_array_manager.ui.main_window.run_benchmark", side_effect=execute):
            self.window._start_benchmark_batch(1)
            self.finish()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual(set(self.window._benchmark_results), {1, 3, 4})
        self.assertIn("Slot 2: FAILED", self.information.call_args.args[2])

    def test_benchmark_cleanup_failure_stops_batch(self):
        calls = []

        def execute(_root, slot, _letter, **_kwargs):
            calls.append(slot)
            raise BenchmarkError("Temporary file cleanup also failed")

        with patch("usb_array_manager.ui.main_window.run_benchmark", side_effect=execute):
            self.window._start_benchmark_batch(3)
            self.finish()
        self.assertEqual(calls, [1])
        self.assertIn("batch stopped", self.window._logs_tab.output.toPlainText())

    def test_benchmark_batch_cancel_waits_for_current_run(self):
        calls = []

        def execute(_root, slot, _letter, **kwargs):
            calls.append(slot)
            self.assertFalse(kwargs["cancel_event"].is_set())
            return benchmark(slot)

        with patch("usb_array_manager.ui.main_window.run_benchmark", side_effect=execute):
            self.window._start_benchmark_batch(3)
            self.window._cancel_benchmark()
            self.finish()
        self.assertEqual(calls, [1])
        self.assertTrue(self.window._benchmarks_tab.benchmark_all_button.isEnabled())
        self.assertEqual(self.window._benchmark_store.load_all(), {})

    def test_probe_all_four_reuses_probe_and_enables_performance(self):
        calls = []

        def execute(item, **kwargs):
            calls.append(item.slot)
            self.assertFalse(kwargs["performance"])
            return probe_result(item.slot)

        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=execute):
            self.window._backend_tab.probe_all_button.click()
            self.assertFalse(self.window._benchmarks_tab.benchmark_all_button.isEnabled())
            self.assertFalse(self.window._backend_tab.probe_all_button.isEnabled())
            self.finish()
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertIn("Overall: 4/4 SUPPORTED", self.window._backend_tab.batch_summary.text())
        self.assertTrue(self.window._backend_tab.performance_all_button.isEnabled())

    def test_probe_unknown_slot_continues_and_blocks_performance_all(self):
        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=lambda item, **_kwargs: probe_result(item.slot, status=UNKNOWN if item.slot == 2 else SUPPORTED)) as probe:
            self.window._start_vhdx_batch(performance=False, runs=1)
            self.finish()
        self.assertEqual(probe.call_count, 4)
        self.assertFalse(self.window._backend_tab.performance_all_button.isEnabled())
        self.assertIn("Overall: 3/4 SUPPORTED", self.window._backend_tab.batch_summary.text())

    def test_performance_all_three_runs_averages_each_slot(self):
        self.qualify_all()
        counts = defaultdict(int)
        calls = []

        def execute(item, **kwargs):
            self.assertTrue(kwargs["performance"])
            counts[item.slot] += 1
            calls.append((item.slot, counts[item.slot]))
            value = counts[item.slot] * 100
            return probe_result(item.slot, VhdxBenchmarkComparison(value, value / 2, value / 4, value * .9, value * .4, value * .2))

        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=execute):
            self.window._start_vhdx_batch(performance=True, runs=3)
            self.finish()
        self.assertEqual(calls, [(slot, run) for slot in range(1, 5) for run in range(1, 4)])
        table = self.window._backend_tab.performance_table
        self.assertEqual(table.rowCount(), 4)
        self.assertEqual(table.item(0, 1).text(), "3")
        self.assertEqual(table.item(0, 2).text(), "200.0 / 100.0 / 50.0")
        self.assertEqual(table.item(0, 3).text(), "180.0 / 80.0 / 40.0")
        self.assertEqual(table.item(0, 4).text(), "-10.0% / -20.0% / -20.0%")
        self.assertIn("VHDX batch overhead", self.window._logs_tab.output.toPlainText())

    def test_performance_failed_slot_retains_individual_logs_and_continues(self):
        self.qualify_all()
        counts = defaultdict(int)

        def execute(item, **_kwargs):
            counts[item.slot] += 1
            if item.slot == 2 and counts[2] == 2:
                return probe_result(2, status=UNKNOWN, explanation="Read failed")
            return probe_result(item.slot, VhdxBenchmarkComparison(100, 50, 40, 90, 45, 36))

        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=execute):
            self.window._start_vhdx_batch(performance=True, runs=3)
            self.finish()
        self.assertEqual(dict(counts), {1: 3, 2: 2, 3: 3, 4: 3})
        table = self.window._backend_tab.performance_table
        self.assertEqual([table.item(row, 0).text() for row in range(table.rowCount())], ["Slot 1", "Slot 3", "Slot 4"])
        self.assertIn("Slot 2: FAILED", self.window._backend_tab.batch_summary.text())
        self.assertIn("Slot 2, run 1/3", self.window._logs_tab.output.toPlainText())

    def test_completed_benchmark_slot_is_saved_when_cancelled_between_slots(self):
        with patch("usb_array_manager.ui.main_window.run_benchmark", return_value=benchmark()) as run:
            self.window._start_benchmark_batch(1)
            self.window._cancel_benchmark()
            self.finish()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.window._benchmark_store.load_all()[1].run_count, 1)

    def test_single_slot_buttons_still_use_existing_implementations(self):
        with patch("usb_array_manager.ui.main_window.run_benchmark", return_value=benchmark(2)) as run:
            self.window._start_benchmark(self.devices[1])
            self.finish()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.window._benchmark_store.load_all()[2].run_count, 1)
        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", return_value=probe_result(2)) as probe:
            self.window._start_vhdx_probe(2)
            self.finish()
        self.assertEqual(probe.call_count, 1)
        self.assertTrue(self.window._vhdx_performance_ready(self.devices[1]))

    def test_repeated_vhdx_runs_complete_cleanup_before_next_create(self):
        self.qualify_all()
        events = []
        root = self.root / "probe"
        root.mkdir()
        benchmark_paths = []

        class Runner:
            def create(self, path, _size):
                # The previous call must have removed its VHDX and benchmark files.
                self_path_files = list(root.rglob("*.vhdx"))
                if self_path_files:
                    raise AssertionError(f"Previous files remain: {self_path_files}")
                events.append("create")
                path.write_bytes(b"temporary test fixture")

            def attach(self, _path, *, prepare_filesystem):
                events.append("attach")
                return VhdxDiskInfo(42, "TEST-VHDX", "File Backed Virtual", "SSD", True, "OK", "Healthy", str(root), size_bytes=1024)

            def detach(self, _path):
                events.append("detach")

        def execute(item, **kwargs):
            result = probe_vhdx_backend(
                item, performance=kwargs["performance"], root=root, runner=Runner(),
                settings=VhdxProbeSettings(
                    vhdx_size_bytes=1024, free_space_reserve_bytes=0,
                    benchmark_settings=BenchmarkSettings(
                        file_size_bytes=65536, block_size_bytes=4096,
                        sample_size_bytes=16384, burst_size_bytes=16384,
                        free_space_reserve_bytes=0,
                    ),
                ),
                elevation_checker=lambda: True,
            )
            self.assertEqual(list(root.iterdir()), [])
            events.append("deleted")
            return result

        def write_fixture(path, *_args):
            benchmark_paths.append(path)
            path.write_bytes(b"temporary benchmark fixture")
            return _WriteMetrics(50, 60, 40, .1)

        def read_fixture(path, *_args):
            self.assertEqual(path.read_bytes(), b"temporary benchmark fixture")
            return 100

        # Exercise both real cleanup paths while replacing only physical I/O measurements.
        with patch("usb_array_manager.services.benchmark._validate_alignment"), patch("usb_array_manager.services.benchmark._sequential_write", side_effect=write_fixture), patch("usb_array_manager.services.benchmark._sequential_read", side_effect=read_fixture), patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=execute):
            self.window._start_vhdx_batch(performance=True, runs=3)
            self.finish()
        self.assertEqual(events, ["create", "attach", "detach", "deleted"] * 12)
        self.assertEqual(len(benchmark_paths), 24)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in benchmark_paths))

    def test_probe_cleanup_failure_prevents_any_more_vhdx_operations(self):
        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", return_value=probe_result(1, status=UNKNOWN, cleanup_status="FAILED to detach", leftover_path="leftover.vhdx")) as probe:
            self.window._start_vhdx_batch(performance=False, runs=1)
            self.finish()
        self.assertEqual(probe.call_count, 1)
        self.assertIn("cleanup failed; batch stopped", self.window._backend_tab.batch_summary.text())

    def test_conflicting_single_or_batch_job_cannot_start(self):
        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=lambda item, **_kwargs: probe_result(item.slot)) as probe, patch("usb_array_manager.ui.main_window.run_benchmark", return_value=benchmark()) as run:
            self.window._start_vhdx_batch(performance=False, runs=1)
            self.window._start_benchmark_batch(3)
            self.window._start_benchmark(self.devices[0])
            self.window._start_vhdx_probe(2)
            self.window._start_vhdx_batch(performance=True, runs=3)
            self.finish()
        self.assertEqual(probe.call_count, 4)
        run.assert_not_called()

    def test_vhdx_cancel_finishes_lifecycle_and_restores_controls(self):
        def execute(item, **kwargs):
            self.assertFalse(kwargs["cancel_event"].is_set())
            return probe_result(item.slot)

        with patch("usb_array_manager.ui.main_window.probe_vhdx_backend", side_effect=execute) as probe:
            self.window._start_vhdx_batch(performance=False, runs=1)
            self.window._cancel_vhdx_batch()
            self.finish()
        self.assertEqual(probe.call_count, 1)
        self.assertTrue(self.window._backend_tab.probe_all_button.isEnabled())
        self.assertIn("Cancelled", self.window._backend_tab.batch_summary.text())

    def test_changed_slot_identity_is_rejected_before_a_batch_run(self):
        expected = self.devices[0]
        changed = replace(expected, storage_unique_id="REPLACED")
        self.window._model.set_devices([changed, *self.devices[1:]])
        with self.assertRaisesRegex(ValueError, "identity"):
            self.window._batch_device(expected)

    def test_backend_workspace_fits_small_windows_with_balanced_result_columns(self):
        tab = self.window._backend_tab
        tab.backend_selector.setCurrentText("VHDX-backed Storage Spaces")
        comparison = VhdxBenchmarkComparison(100, 50, 40, 90, 45, 36)
        tab.set_performance_batch_results({slot: (3, comparison) for slot in range(1, 5)})
        self.window._tabs.setCurrentWidget(tab)
        self.window.show()
        for width, height in ((1280, 720), (1366, 768)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.application.processEvents()
                self.assertEqual(self.window.width(), width)
                self.assertEqual(self.window.height(), height)
                self.assertLessEqual(tab.vhdx_panel.geometry().right(), tab.result_splitter.geometry().left())
                self.assertGreater(tab.probe_all_button.width(), 0)
                self.assertGreater(tab.performance_all_button.width(), 0)
                self.assertLessEqual(tab.minimumSizeHint().height(), self.window.height())
                header = tab.performance_table.horizontalHeader()
                for column in (2, 3, 4):
                    self.assertEqual(header.sectionResizeMode(column), QHeaderView.ResizeMode.Stretch)
                self.assertLessEqual(abs(header.sectionSize(4) - header.sectionSize(2)), 2)
                self.assertLess(header.sectionSize(4), tab.performance_table.viewport().width() / 2)


if __name__ == "__main__":
    unittest.main()
