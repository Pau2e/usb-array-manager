from __future__ import annotations

from usb_array_manager.models.raid_plan import (
    MirrorPairEstimate,
    PlannerDrive,
    Raid10Estimate,
)


SLOW_WRITE_THRESHOLD_MBPS = 20.0
WRITE_MISMATCH_RATIO = 0.75


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
    if len(drives) < 2 or len(drives) % 2:
        raise Raid10PlanError("Select an even number of at least two drives.")
    if len({drive.slot for drive in drives}) != len(drives):
        raise Raid10PlanError("Selected slots must be unique.")

    ordered = sorted(drives, key=lambda drive: drive.slot)
    _score, pairs = _best_pairing(ordered)
    return pairs


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


def _best_pairing(
    drives: list[PlannerDrive],
) -> tuple[float, list[tuple[PlannerDrive, PlannerDrive]]]:
    if not drives:
        return 0.0, []

    first = drives[0]
    best_score = float("-inf")
    best_pairs: list[tuple[PlannerDrive, PlannerDrive]] = []
    for index in range(1, len(drives)):
        second = drives[index]
        remaining = drives[1:index] + drives[index + 1 :]
        remaining_score, remaining_pairs = _best_pairing(remaining)
        score = _pairing_score(first, second) + remaining_score
        candidate = [(first, second), *remaining_pairs]
        if score > best_score:
            best_score = score
            best_pairs = candidate
    return best_score, best_pairs


def _pairing_score(first: PlannerDrive, second: PlannerDrive) -> float:
    capacity_similarity = _similarity(first.capacity_bytes, second.capacity_bytes)
    write_similarity = _similarity(
        first.sustained_write_mbps,
        second.sustained_write_mbps,
    )
    return capacity_similarity * 2.0 + write_similarity


def _similarity(first: float | int | None, second: float | int | None) -> float:
    if first is None and second is None:
        return 0.5
    if first is None or second is None:
        return 0.0
    larger = max(first, second)
    return min(first, second) / larger if larger > 0 else 1.0


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
