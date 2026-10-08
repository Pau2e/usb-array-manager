import unittest

from usb_array_manager.models.raid_plan import PlannerDrive
from usb_array_manager.services.raid10_failure_simulator import (
    ARRAY_DEGRADED,
    ARRAY_FAILED,
    ARRAY_HEALTHY,
    PAIR_DEGRADED,
    PAIR_FAILED,
    PAIR_HEALTHY,
    simulate_failures,
)
from usb_array_manager.services.raid10_planner import estimate_raid10


def drive(slot: int, read: float) -> PlannerDrive:
    return PlannerDrive(
        slot=slot,
        model=f"Drive {slot}",
        capacity_bytes=32 * 1024**3,
        sequential_read_mbps=read,
        sustained_write_mbps=50.0,
        is_connected=True,
    )


class Raid10FailureSimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.estimate = estimate_raid10(
            [
                (drive(1, 150), drive(2, 140)),
                (drive(3, 100), drive(4, 90)),
            ]
        )

    def test_no_failures_is_healthy(self) -> None:
        simulation = simulate_failures(self.estimate, set())

        self.assertEqual(simulation.array_status, ARRAY_HEALTHY)
        self.assertEqual(
            [state.status for state in simulation.pair_states],
            [PAIR_HEALTHY, PAIR_HEALTHY],
        )
        self.assertEqual(simulation.remaining_working_slots, (1, 2, 3, 4))
        self.assertEqual(simulation.conservative_read_mbps, 230)
        self.assertEqual(simulation.theoretical_max_read_mbps, 480)

    def test_one_failed_drive_degrades_one_pair(self) -> None:
        simulation = simulate_failures(self.estimate, {1})

        self.assertEqual(simulation.array_status, ARRAY_DEGRADED)
        self.assertEqual(simulation.pair_states[0].status, PAIR_DEGRADED)
        self.assertEqual(simulation.remaining_working_slots, (2, 3, 4))
        self.assertEqual(simulation.at_risk_pairs, ("Pair A",))
        self.assertEqual(simulation.conservative_read_mbps, 230)
        self.assertEqual(simulation.theoretical_max_read_mbps, 330)
        self.assertIn("Conditional", simulation.additional_failure_tolerance)

    def test_two_failures_in_different_pairs_remain_degraded(self) -> None:
        simulation = simulate_failures(self.estimate, {1, 3})

        self.assertEqual(simulation.array_status, ARRAY_DEGRADED)
        self.assertEqual(
            [state.status for state in simulation.pair_states],
            [PAIR_DEGRADED, PAIR_DEGRADED],
        )
        self.assertEqual(simulation.remaining_working_slots, (2, 4))
        self.assertEqual(simulation.conservative_read_mbps, 230)
        self.assertEqual(simulation.theoretical_max_read_mbps, 230)
        self.assertTrue(simulation.additional_failure_tolerance.startswith("No"))

    def test_two_failures_in_same_pair_fail_array(self) -> None:
        simulation = simulate_failures(self.estimate, {1, 2})

        self.assertEqual(simulation.array_status, ARRAY_FAILED)
        self.assertEqual(simulation.pair_states[0].status, PAIR_FAILED)
        self.assertIsNone(simulation.conservative_read_mbps)
        self.assertIsNone(simulation.theoretical_max_read_mbps)
        self.assertIn("Pair A", simulation.explanation)

    def test_three_failures_fail_array(self) -> None:
        simulation = simulate_failures(self.estimate, {1, 2, 3})

        self.assertEqual(simulation.array_status, ARRAY_FAILED)
        self.assertEqual(simulation.remaining_working_slots, (4,))
        self.assertEqual(simulation.lost_redundancy_pairs, ("Pair A", "Pair B"))


if __name__ == "__main__":
    unittest.main()
