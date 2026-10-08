from datetime import datetime, timezone
import unittest

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.raid10_readiness import (
    NOT_READY,
    READY,
    WARNING,
    validate_raid10_readiness,
)


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
PLAN = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-08T00:00:00+00:00")


def device(slot: int, **changes) -> StorageDevice:
    values = dict(
        model=f"Drive {slot}",
        serial_number=f"SERIAL-{slot}",
        capacity_bytes=32 * 1024**3,
        drive_letters=(f"{chr(71 + slot)}:",),
        device_path=rf"\\.\PHYSICALDRIVE{slot}",
        pnp_device_id=f"USBSTOR\\DEVICE-{slot}",
        usb_vid="1234",
        usb_pid="5678",
        is_connected=True,
        health_status="Healthy",
        slot=slot,
    )
    values.update(changes)
    return StorageDevice(**values)


def benchmark(slot: int, **changes) -> BenchmarkResult:
    values = dict(
        slot=slot,
        tested_at="2026-10-01T00:00:00+00:00",
        drive_letter=f"{chr(71 + slot)}:",
        file_size_bytes=1024**3,
        sequential_read_mbps=120.0,
        sequential_write_mbps=60.0,
        burst_write_mbps=65.0,
        sustained_write_mbps=55.0,
        write_stability_cv=0.05,
        qualification="GOOD",
    )
    values.update(changes)
    return BenchmarkResult(**values)


class Raid10ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.devices = [device(slot) for slot in range(1, 5)]
        self.benchmarks = {slot: benchmark(slot) for slot in range(1, 5)}

    def test_complete_current_plan_is_ready(self) -> None:
        readiness = validate_raid10_readiness(
            PLAN, self.devices, self.benchmarks, now=NOW
        )
        self.assertEqual(readiness.status, READY)
        self.assertEqual(readiness.issues, ())

    def test_slot_change_and_missing_benchmark_are_not_ready(self) -> None:
        devices = [item for item in self.devices if item.slot != 4]
        benchmarks = dict(self.benchmarks)
        benchmarks.pop(2)
        readiness = validate_raid10_readiness(
            PLAN, devices, benchmarks, now=NOW
        )
        self.assertEqual(readiness.status, NOT_READY)
        self.assertIn("missing_slot", {issue.code for issue in readiness.issues})
        self.assertIn("missing_benchmark", {issue.code for issue in readiness.issues})

    def test_disconnected_ambiguous_and_unhealthy_members_are_not_ready(self) -> None:
        devices = list(self.devices)
        devices[0] = device(1, is_connected=False)
        devices[1] = device(2, health_status="Warning")
        readiness = validate_raid10_readiness(
            PLAN,
            devices,
            self.benchmarks,
            now=NOW,
            ambiguous_slots={3},
        )
        self.assertEqual(readiness.status, NOT_READY)
        codes = {issue.code for issue in readiness.issues}
        self.assertTrue({"disconnected", "unhealthy", "ambiguous_identity"} <= codes)

    def test_stale_benchmark_capacity_mismatch_and_slow_member_warn(self) -> None:
        devices = list(self.devices)
        devices[1] = device(2, capacity_bytes=16 * 1024**3)
        benchmarks = dict(self.benchmarks)
        benchmarks[1] = benchmark(
            1,
            tested_at="2026-08-01T00:00:00+00:00",
            qualification="SLOW WRITE",
        )
        readiness = validate_raid10_readiness(
            PLAN, devices, benchmarks, now=NOW
        )
        self.assertEqual(readiness.status, WARNING)
        codes = {issue.code for issue in readiness.issues}
        self.assertTrue(
            {"stale_benchmark", "capacity_mismatch", "slow_member"} <= codes
        )

    def test_unstable_member_is_not_ready(self) -> None:
        benchmarks = dict(self.benchmarks)
        benchmarks[4] = benchmark(4, qualification="UNSTABLE")
        readiness = validate_raid10_readiness(
            PLAN, self.devices, benchmarks, now=NOW
        )
        self.assertEqual(readiness.status, NOT_READY)


if __name__ == "__main__":
    unittest.main()

