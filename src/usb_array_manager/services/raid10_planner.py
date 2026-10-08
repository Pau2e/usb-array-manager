from __future__ import annotations

from usb_array_manager.models.raid_plan import (
    MirrorPairEstimate,
    PairingSuggestion,
    PlannerDrive,
    Raid10Estimate,
)


SLOW_WRITE_THRESHOLD_MBPS = 20.0
WRITE_MISMATCH_RATIO = 0.75
NEAR_WRITE_TOLERANCE_PERCENT = 5.0


class Raid10PlanError(ValueError):
    """Raised when a proposed RAID10 layout is incomplete or ambiguous."""


def estimate_raid10(
    pairs: list[tuple[PlannerDrive, PlannerDrive]],
) -> Raid10Estimate:
    if not pairs:
        raise Raid10PlanError("Select an even number of drives and define mirror pairs.")

    slots = [drive.slot for pair in pairs for drive in pair]
    if len(slots) != len(set(slots)):
        raise Raid10PlanError("Each selected slot must appear in exactly one mirror pair.")

    pair_estimates = tuple(
        _estimate_pair(_pair_label(index), first, second)
        for index, (first, second) in enumerate(pairs)
    )
    drives = [drive for pair in pairs for drive in pair]

    raw_capacity = _sum_if_known(drive.capacity_bytes for drive in drives)
    usable_capacity = _sum_if_known(
        pair.usable_capacity_bytes for pair in pair_estimates
    )
    efficiency = (
        usable_capacity / raw_capacity * 100
        if usable_capacity is not None and raw_capacity
        else None
    )

    warnings: list[str] = []
    for pair in pair_estimates:
        warnings.extend(pair.warnings)

    return Raid10Estimate(
        pairs=pair_estimates,
        total_raw_capacity_bytes=raw_capacity,
        total_usable_capacity_bytes=usable_capacity,
        capacity_efficiency_percent=efficiency,
        estimated_sustained_write_mbps=_sum_if_known(
            pair.estimated_write_mbps for pair in pair_estimates
        ),
        conservative_read_mbps=_sum_if_known(
            pair.conservative_read_mbps for pair in pair_estimates
        ),
        theoretical_max_read_mbps=_sum_if_known(
            pair.theoretical_max_read_mbps for pair in pair_estimates
        ),
        warnings=tuple(warnings),
    )


def suggest_mirror_pairs(
    drives: list[PlannerDrive],
) -> list[tuple[PlannerDrive, PlannerDrive]]:
    return list(suggest_mirror_pairing(drives).pairs)


def suggest_mirror_pairing(drives: list[PlannerDrive]) -> PairingSuggestion:
    if len(drives) < 2 or len(drives) % 2:
        raise Raid10PlanError("Select an even number of at least two drives.")
    if len({drive.slot for drive in drives}) != len(drives):
        raise Raid10PlanError("Selected slots must be unique.")

    ordered = sorted(drives, key=lambda drive: drive.slot)
    candidates = [
        _PairingCandidate(
            pairs=tuple(pairs),
            estimated_write_mbps=_pairing_write_estimate(pairs),
            capacity_waste_bytes=_pairing_capacity_waste(pairs),
        )
        for pairs in _all_pairings(ordered)
    ]
    selected, explanation = _select_candidate(candidates, ordered)
    return PairingSuggestion(
        pairs=selected.pairs,
        estimated_sustained_write_mbps=selected.estimated_write_mbps,
        capacity_waste_bytes=selected.capacity_waste_bytes,
        evaluated_layout_count=len(candidates),
        explanation=(
            f"Evaluated all {len(candidates)} valid pairing(s). {explanation}"
        ),
    )


def _estimate_pair(
    label: str,
    first: PlannerDrive,
    second: PlannerDrive,
) -> MirrorPairEstimate:
    warnings: list[str] = []
    connected = first.is_connected and second.is_connected
    benchmarked = _has_benchmark(first) and _has_benchmark(second)

    if not first.is_connected:
        warnings.append(f"{label}: Slot {first.slot} is disconnected.")
    if not second.is_connected:
        warnings.append(f"{label}: Slot {second.slot} is disconnected.")

    unbenchmarked = [
        drive.slot for drive in (first, second) if not _has_benchmark(drive)
    ]
    if unbenchmarked:
        slot_text = ", ".join(f"Slot {slot}" for slot in unbenchmarked)
        warnings.append(f"{label}: {slot_text} has no valid benchmark result.")

    usable_capacity: int | None = None
    if first.capacity_bytes is not None and second.capacity_bytes is not None:
        usable_capacity = min(first.capacity_bytes, second.capacity_bytes)
        if first.capacity_bytes != second.capacity_bytes:
            smaller = first if first.capacity_bytes < second.capacity_bytes else second
            wasted = abs(first.capacity_bytes - second.capacity_bytes)
            warnings.append(
                f"{label}: capacities differ; Slot {smaller.slot} limits the pair "
                f"and {_format_gib(wasted)} GiB of the larger drive is unusable."
            )
    else:
        warnings.append(f"{label}: capacity information is unavailable.")

    estimated_write: float | None = None
    conservative_read: float | None = None
    theoretical_read: float | None = None
    if connected and benchmarked:
        first_write = first.sustained_write_mbps
        second_write = second.sustained_write_mbps
        first_read = first.sequential_read_mbps
        second_read = second.sequential_read_mbps
        assert first_write is not None and second_write is not None
        assert first_read is not None and second_read is not None

        estimated_write = min(first_write, second_write)
        conservative_read = min(first_read, second_read)
        theoretical_read = first_read + second_read

        faster_write = max(first_write, second_write)
        slower_write = min(first_write, second_write)
        if faster_write > 0 and slower_write / faster_write < WRITE_MISMATCH_RATIO:
            slower = first if first_write < second_write else second
            warnings.append(
                f"{label}: write speeds differ significantly; Slot {slower.slot} "
                "will limit mirror writes."
            )
        if slower_write < SLOW_WRITE_THRESHOLD_MBPS:
            slower = first if first_write <= second_write else second
            warnings.append(
                f"{label}: Slot {slower.slot} is a slow write bottleneck "
                f"at {slower_write:.1f} MB/s."
            )

    return MirrorPairEstimate(
        label=label,
        first=first,
        second=second,
        usable_capacity_bytes=usable_capacity,
        estimated_write_mbps=estimated_write,
        conservative_read_mbps=conservative_read,
        theoretical_max_read_mbps=theoretical_read,
        bottleneck=_bottleneck(first, second),
        warnings=tuple(warnings),
    )


def _bottleneck(first: PlannerDrive, second: PlannerDrive) -> str:
    if not first.is_connected or not second.is_connected:
        return "Disconnected member"
    if not _has_benchmark(first) or not _has_benchmark(second):
        return "Unknown — benchmark required"

    assert first.sustained_write_mbps is not None
    assert second.sustained_write_mbps is not None
    if first.sustained_write_mbps < second.sustained_write_mbps:
        return f"Slot {first.slot} — slower write"
    if second.sustained_write_mbps < first.sustained_write_mbps:
        return f"Slot {second.slot} — slower write"

    if first.capacity_bytes is not None and second.capacity_bytes is not None:
        if first.capacity_bytes < second.capacity_bytes:
            return f"Slot {first.slot} — smaller capacity"
        if second.capacity_bytes < first.capacity_bytes:
            return f"Slot {second.slot} — smaller capacity"
    return "Balanced"


class _PairingCandidate:
    def __init__(
        self,
        *,
        pairs: tuple[tuple[PlannerDrive, PlannerDrive], ...],
        estimated_write_mbps: float | None,
        capacity_waste_bytes: int | None,
    ) -> None:
        self.pairs = pairs
        self.estimated_write_mbps = estimated_write_mbps
        self.capacity_waste_bytes = capacity_waste_bytes

    @property
    def slot_signature(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (min(first.slot, second.slot), max(first.slot, second.slot))
            for first, second in self.pairs
        )


def _all_pairings(
    drives: list[PlannerDrive],
) -> list[list[tuple[PlannerDrive, PlannerDrive]]]:
    if not drives:
        return [[]]

    first = drives[0]
    layouts: list[list[tuple[PlannerDrive, PlannerDrive]]] = []
    for index in range(1, len(drives)):
        second = drives[index]
        remaining = drives[1:index] + drives[index + 1 :]
        for remaining_pairs in _all_pairings(remaining):
            layouts.append([(first, second), *remaining_pairs])
    return layouts


def _select_candidate(
    candidates: list[_PairingCandidate],
    drives: list[PlannerDrive],
) -> tuple[_PairingCandidate, str]:
    complete_write_data = all(
        drive.sustained_write_mbps is not None for drive in drives
    )
    if not complete_write_data:
        selected = min(candidates, key=_capacity_then_signature)
        missing = ", ".join(
            f"Slot {drive.slot}"
            for drive in drives
            if drive.sustained_write_mbps is None
        )
        return selected, (
            f"Sustained-write comparison is unavailable because {missing} has no "
            f"benchmark. Selected the layout with {_waste_text(selected)} capacity waste."
        )

    best_write = max(
        candidate.estimated_write_mbps or 0.0 for candidate in candidates
    )
    tolerance = max(1.0, best_write * NEAR_WRITE_TOLERANCE_PERCENT / 100)
    near_best = [
        candidate
        for candidate in candidates
        if best_write - (candidate.estimated_write_mbps or 0.0) <= tolerance
    ]
    selected = min(
        near_best,
        key=lambda candidate: (
            _waste_sort_value(candidate),
            -(candidate.estimated_write_mbps or 0.0),
            candidate.slot_signature,
        ),
    )
    selected_write = selected.estimated_write_mbps or 0.0
    if abs(selected_write - best_write) < 1e-9:
        return selected, (
            "Suggested because this pairing gives the highest estimated sustained "
            f"write throughput ({selected_write:.1f} MB/s) with "
            f"{_waste_text(selected)} capacity waste."
        )
    return selected, (
        f"Write estimates are within {NEAR_WRITE_TOLERANCE_PERCENT:.0f}% of the best. "
        f"Selected {selected_write:.1f} MB/s instead of {best_write:.1f} MB/s to "
        f"reduce capacity waste to {_waste_text(selected)}."
    )


def _pairing_write_estimate(
    pairs: list[tuple[PlannerDrive, PlannerDrive]],
) -> float | None:
    speeds = [
        drive.sustained_write_mbps
        for pair in pairs
        for drive in pair
    ]
    if any(speed is None for speed in speeds):
        return None
    return sum(
        min(first.sustained_write_mbps, second.sustained_write_mbps)
        for first, second in pairs
        if first.sustained_write_mbps is not None
        and second.sustained_write_mbps is not None
    )


def _pairing_capacity_waste(
    pairs: list[tuple[PlannerDrive, PlannerDrive]],
) -> int | None:
    capacities = [drive.capacity_bytes for pair in pairs for drive in pair]
    if any(capacity is None for capacity in capacities):
        return None
    return sum(
        abs(first.capacity_bytes - second.capacity_bytes)
        for first, second in pairs
        if first.capacity_bytes is not None and second.capacity_bytes is not None
    )


def _capacity_then_signature(candidate: _PairingCandidate):
    return (_waste_sort_value(candidate), candidate.slot_signature)


def _waste_sort_value(candidate: _PairingCandidate) -> float:
    return (
        float(candidate.capacity_waste_bytes)
        if candidate.capacity_waste_bytes is not None
        else float("inf")
    )


def _waste_text(candidate: _PairingCandidate) -> str:
    if candidate.capacity_waste_bytes is None:
        return "unknown"
    return f"{candidate.capacity_waste_bytes / 1024**3:.1f} GiB"


def _has_benchmark(drive: PlannerDrive) -> bool:
    return (
        drive.sequential_read_mbps is not None
        and drive.sustained_write_mbps is not None
    )


def _sum_if_known(values) -> float | int | None:
    collected = list(values)
    if any(value is None for value in collected):
        return None
    return sum(collected)


def _pair_label(index: int) -> str:
    if index < 26:
        return f"Pair {chr(ord('A') + index)}"
    return f"Pair {index + 1}"


def _format_gib(byte_count: int) -> str:
    return f"{byte_count / 1024**3:.1f}"
