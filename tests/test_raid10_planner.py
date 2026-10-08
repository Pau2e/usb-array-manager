import unittest

from usb_array_manager.models.raid_plan import PlannerDrive
from usb_array_manager.services.raid10_planner import (
    Raid10PlanError,
    estimate_raid10,
    suggest_mirror_pairing,
    suggest_mirror_pairs,
)


GIB = 1024**3


def drive(
    slot: int,
    capacity_gib: int,
    read: float | None,
    write: float | None,
    *,
    connected: bool = True,
) -> PlannerDrive:
    return PlannerDrive(
        slot=slot,
        model=f"Drive {slot}",
        capacity_bytes=capacity_gib * GIB,
        sequential_read_mbps=read,
        sustained_write_mbps=write,
        is_connected=connected,
    )


class Raid10PlannerTests(unittest.TestCase):
    def test_four_drive_estimate_uses_mirror_limits_and_stripe_sums(self) -> None:
        slot1 = drive(1, 32, 150, 60)
        slot2 = drive(2, 30, 140, 50)
        slot3 = drive(3, 64, 160, 45)
        slot4 = drive(4, 60, 130, 40)

        estimate = estimate_raid10([(slot1, slot2), (slot3, slot4)])

        self.assertEqual(estimate.total_raw_capacity_bytes, 186 * GIB)
        self.assertEqual(estimate.total_usable_capacity_bytes, 90 * GIB)
        self.assertAlmostEqual(estimate.capacity_efficiency_percent, 48.387, places=3)
        self.assertEqual(estimate.estimated_sustained_write_mbps, 90)
        self.assertEqual(estimate.conservative_read_mbps, 270)
        self.assertEqual(estimate.theoretical_max_read_mbps, 580)
        self.assertEqual(estimate.pairs[0].bottleneck, "Slot 2 — slower write")

    def test_warnings_cover_capacity_speed_benchmark_and_connection(self) -> None:
        slot1 = drive(1, 64, 150, 60)
        slot2 = drive(2, 32, 100, 10)
        slot3 = drive(3, 32, None, None)
        slot4 = drive(4, 32, 150, 50, connected=False)

        estimate = estimate_raid10([(slot1, slot2), (slot3, slot4)])
        warning_text = " ".join(estimate.warnings)

        self.assertIn("capacities differ", warning_text)
        self.assertIn("write speeds differ significantly", warning_text)
        self.assertIn("slow write bottleneck", warning_text)
        self.assertIn("no valid benchmark", warning_text)
        self.assertIn("disconnected", warning_text)
        self.assertIsNone(estimate.estimated_sustained_write_mbps)
        self.assertIsNone(estimate.conservative_read_mbps)

    def test_suggestion_pairs_similar_capacity_and_write_speed(self) -> None:
        drives = [
            drive(1, 32, 150, 100),
            drive(2, 64, 160, 50),
            drive(3, 32, 145, 95),
            drive(4, 64, 155, 48),
        ]

        suggested = suggest_mirror_pairs(drives)
        slot_pairs = {frozenset((first.slot, second.slot)) for first, second in suggested}

        self.assertEqual(
            slot_pairs,
            {frozenset((1, 3)), frozenset((2, 4))},
        )

    def test_fast_fast_and_slow_slow_maximizes_write_throughput(self) -> None:
        drives = [
            drive(1, 32, 150, 100),
            drive(2, 32, 150, 95),
            drive(3, 32, 100, 20),
            drive(4, 32, 100, 10),
        ]

        suggestion = suggest_mirror_pairing(drives)
        slot_pairs = {
            frozenset((first.slot, second.slot))
            for first, second in suggestion.pairs
        }

        self.assertEqual(
            slot_pairs,
            {frozenset((1, 2)), frozenset((3, 4))},
        )
        self.assertEqual(suggestion.estimated_sustained_write_mbps, 105)
        self.assertEqual(suggestion.evaluated_layout_count, 3)
        self.assertIn("highest estimated sustained write", suggestion.explanation)

    def test_fast_slow_capacity_pairs_do_not_override_large_write_loss(self) -> None:
        drives = [
            drive(1, 32, 150, 100),
            drive(2, 64, 150, 100),
            drive(3, 32, 100, 10),
            drive(4, 64, 100, 10),
        ]

        suggestion = suggest_mirror_pairing(drives)
        slot_pairs = {
            frozenset((first.slot, second.slot))
            for first, second in suggestion.pairs
        }

        self.assertEqual(
            slot_pairs,
            {frozenset((1, 2)), frozenset((3, 4))},
        )
        self.assertEqual(suggestion.estimated_sustained_write_mbps, 110)
        self.assertEqual(suggestion.capacity_waste_bytes, 64 * GIB)

    def test_equal_speeds_use_capacity_waste_as_tiebreaker(self) -> None:
        drives = [
            drive(1, 32, 150, 50),
            drive(2, 64, 150, 50),
            drive(3, 32, 150, 50),
            drive(4, 64, 150, 50),
        ]

        suggestion = suggest_mirror_pairing(drives)
        slot_pairs = {
            frozenset((first.slot, second.slot))
            for first, second in suggestion.pairs
        }

        self.assertEqual(
            slot_pairs,
            {frozenset((1, 3)), frozenset((2, 4))},
        )
        self.assertEqual(suggestion.capacity_waste_bytes, 0)

    def test_close_write_estimates_prefer_lower_capacity_waste(self) -> None:
        drives = [
            drive(1, 32, 150, 100),
            drive(2, 64, 150, 99),
            drive(3, 32, 150, 98),
            drive(4, 64, 150, 97),
        ]

        suggestion = suggest_mirror_pairing(drives)
        slot_pairs = {
            frozenset((first.slot, second.slot))
            for first, second in suggestion.pairs
        }

        self.assertEqual(
            slot_pairs,
            {frozenset((1, 3)), frozenset((2, 4))},
        )
        self.assertEqual(suggestion.estimated_sustained_write_mbps, 195)
        self.assertEqual(suggestion.capacity_waste_bytes, 0)
        self.assertIn("within 5%", suggestion.explanation)

    def test_missing_benchmark_data_falls_back_to_capacity_waste(self) -> None:
        drives = [
            drive(1, 32, None, None),
            drive(2, 64, 150, 60),
            drive(3, 32, 150, 55),
            drive(4, 64, 150, 50),
        ]

        suggestion = suggest_mirror_pairing(drives)
        slot_pairs = {
            frozenset((first.slot, second.slot))
            for first, second in suggestion.pairs
        }

        self.assertEqual(
            slot_pairs,
            {frozenset((1, 3)), frozenset((2, 4))},
        )
        self.assertIsNone(suggestion.estimated_sustained_write_mbps)
        self.assertEqual(suggestion.capacity_waste_bytes, 0)
        self.assertIn("Slot 1 has no benchmark", suggestion.explanation)

    def test_duplicate_slot_is_rejected(self) -> None:
        slot1 = drive(1, 32, 150, 60)
        slot2 = drive(2, 32, 150, 60)

        with self.assertRaises(Raid10PlanError):
            estimate_raid10([(slot1, slot2), (slot1, slot2)])


if __name__ == "__main__":
    unittest.main()
