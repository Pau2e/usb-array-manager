from __future__ import annotations

from usb_array_manager.models.raid_failure import (
    MirrorFailureState,
    Raid10FailureSimulation,
)
from usb_array_manager.models.raid_plan import MirrorPairEstimate, Raid10Estimate


PAIR_HEALTHY = "HEALTHY"
PAIR_DEGRADED = "DEGRADED"
PAIR_FAILED = "FAILED"
ARRAY_HEALTHY = "HEALTHY"
ARRAY_DEGRADED = "DEGRADED — data still available"
ARRAY_FAILED = "FAILED — data unavailable"


class FailureSimulationError(ValueError):
    """Raised when a simulated failure is not part of the current RAID10 plan."""


def simulate_failures(
    estimate: Raid10Estimate,
    failed_slots: set[int],
) -> Raid10FailureSimulation:
    selected_slots = {
        drive.slot
        for pair in estimate.pairs
        for drive in (pair.first, pair.second)
    }
    unknown_slots = failed_slots - selected_slots
    if unknown_slots:
        slot_text = ", ".join(f"Slot {slot}" for slot in sorted(unknown_slots))
        raise FailureSimulationError(
            f"Simulated failures are not part of this RAID10 plan: {slot_text}."
        )

    pair_states = tuple(
        _simulate_pair(pair, failed_slots) for pair in estimate.pairs
    )
    remaining = tuple(sorted(selected_slots - failed_slots))
    failed = tuple(sorted(failed_slots))
    failed_pairs = [state for state in pair_states if state.status == PAIR_FAILED]
    degraded_pairs = [state for state in pair_states if state.status == PAIR_DEGRADED]
    healthy_pairs = [state for state in pair_states if state.status == PAIR_HEALTHY]

    if failed_pairs:
        array_status = ARRAY_FAILED
        names = ", ".join(state.label for state in failed_pairs)
        explanation = (
            f"{names} lost both mirror members. Striped data from that mirror pair "
            "has no surviving copy, so the complete RAID10 array is unavailable."
        )
        conservative_read = None
        theoretical_read = None
        tolerance = "No — the array is already failed and data is unavailable."
    elif degraded_pairs:
        array_status = ARRAY_DEGRADED
        names = ", ".join(state.label for state in degraded_pairs)
        explanation = (
            f"{names} has one surviving member. Data remains available, but "
            "redundancy is lost for every degraded pair."
        )
        conservative_read = _sum_if_known(
            state.conservative_read_mbps for state in pair_states
        )
        theoretical_read = _sum_if_known(
            state.theoretical_max_read_mbps for state in pair_states
        )
        if healthy_pairs:
            at_risk = ", ".join(state.label for state in degraded_pairs)
            tolerance = (
                "Conditional — another failure is safe only in a still-healthy pair. "
                f"A failure of the remaining member in {at_risk} would fail the array."
            )
        else:
            tolerance = (
                "No — every mirror pair has only one surviving member; any additional "
                "failure would fail the array."
            )
    else:
        array_status = ARRAY_HEALTHY
        explanation = (
            "All mirror pairs have both members available. The array retains full "
            "mirror redundancy."
        )
        conservative_read = _sum_if_known(
            state.conservative_read_mbps for state in pair_states
        )
        theoretical_read = _sum_if_known(
            state.theoretical_max_read_mbps for state in pair_states
        )
        tolerance = (
            "Yes — one drive may fail in any mirror pair without losing array data."
        )

    return Raid10FailureSimulation(
        pair_states=pair_states,
        array_status=array_status,
        explanation=explanation,
        failed_slots=failed,
        remaining_working_slots=remaining,
        lost_redundancy_pairs=tuple(
            state.label for state in pair_states if state.status != PAIR_HEALTHY
        ),
        at_risk_pairs=tuple(
            state.label for state in pair_states if state.status == PAIR_DEGRADED
        ),
        additional_failure_tolerance=tolerance,
        conservative_read_mbps=conservative_read,
        theoretical_max_read_mbps=theoretical_read,
    )


def _simulate_pair(
    pair: MirrorPairEstimate,
    failed_slots: set[int],
) -> MirrorFailureState:
    drives = (pair.first, pair.second)
    failed = tuple(drive.slot for drive in drives if drive.slot in failed_slots)
    working = tuple(drive.slot for drive in drives if drive.slot not in failed_slots)

    if len(failed) == 2:
        status = PAIR_FAILED
        conservative_read = None
        theoretical_read = None
    elif len(failed) == 1:
        status = PAIR_DEGRADED
        survivor = next(drive for drive in drives if drive.slot in working)
        conservative_read = survivor.sequential_read_mbps
        theoretical_read = survivor.sequential_read_mbps
    else:
        status = PAIR_HEALTHY
        read_speeds = [drive.sequential_read_mbps for drive in drives]
        if any(speed is None for speed in read_speeds):
            conservative_read = None
            theoretical_read = None
        else:
            known_speeds = [speed for speed in read_speeds if speed is not None]
            conservative_read = min(known_speeds)
            theoretical_read = sum(known_speeds)

    return MirrorFailureState(
        label=pair.label,
        status=status,
        member_slots=(pair.first.slot, pair.second.slot),
        failed_slots=tuple(sorted(failed)),
        working_slots=tuple(sorted(working)),
        conservative_read_mbps=conservative_read,
        theoretical_max_read_mbps=theoretical_read,
    )


def _sum_if_known(values) -> float | None:
    collected = list(values)
    if any(value is None for value in collected):
        return None
    return float(sum(collected))
