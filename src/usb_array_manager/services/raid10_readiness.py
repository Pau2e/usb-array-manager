from __future__ import annotations

from datetime import datetime, timedelta, timezone

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.saved_raid10_plan import (
    Raid10Readiness,
    ReadinessIssue,
    SavedRaid10Plan,
)
from usb_array_manager.models.storage_device import StorageDevice


READY = "READY"
WARNING = "WARNING"
NOT_READY = "NOT READY"
DEFAULT_BENCHMARK_MAX_AGE_DAYS = 30
CAPACITY_MISMATCH_WARNING_RATIO = 0.10


def validate_raid10_readiness(
    plan: SavedRaid10Plan,
    devices: tuple[StorageDevice, ...] | list[StorageDevice],
    benchmarks: dict[int, BenchmarkResult],
    *,
    now: datetime | None = None,
    benchmark_max_age_days: int = DEFAULT_BENCHMARK_MAX_AGE_DAYS,
    ambiguous_slots: set[int] | frozenset[int] = frozenset(),
) -> Raid10Readiness:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    by_slot = {device.slot: device for device in devices if device.slot is not None}
    issues: list[ReadinessIssue] = []

    for slot in plan.slots:
        device = by_slot.get(slot)
        if device is None:
            _add(issues, NOT_READY, "missing_slot", f"Slot {slot} is not reserved.")
            continue
        if slot in ambiguous_slots:
            _add(
                issues,
                NOT_READY,
                "ambiguous_identity",
                f"Slot {slot} cannot be matched to exactly one connected device.",
            )
        if not device.is_connected:
            _add(issues, NOT_READY, "disconnected", f"Slot {slot} is disconnected.")

        health = (device.health_status or "").strip().casefold()
        if not health:
            _add(
                issues,
                WARNING,
                "unknown_health",
                f"Slot {slot} has no Windows health status.",
            )
        elif health not in {"healthy", "ok"}:
            _add(
                issues,
                NOT_READY,
                "unhealthy",
                f"Slot {slot} reports Windows health status {device.health_status}.",
            )

        result = benchmarks.get(slot)
        if result is None:
            _add(
                issues,
                NOT_READY,
                "missing_benchmark",
                f"Slot {slot} needs a current benchmark.",
            )
            continue
        tested_at = _parse_datetime(result.tested_at)
        if tested_at is None:
            _add(
                issues,
                NOT_READY,
                "invalid_benchmark_time",
                f"Slot {slot} has an invalid benchmark timestamp.",
            )
        elif current - tested_at > timedelta(days=benchmark_max_age_days):
            age_days = max(0, (current - tested_at).days)
            _add(
                issues,
                WARNING,
                "stale_benchmark",
                f"Slot {slot} benchmark is {age_days} days old (warning after {benchmark_max_age_days} days).",
            )

        qualification = result.qualification.strip().upper()
        if qualification == "UNSTABLE":
            _add(
                issues,
                NOT_READY,
                "unstable_member",
                f"Slot {slot} is qualified UNSTABLE.",
            )
        elif qualification in {"SLOW WRITE", "SLOW READ"}:
            _add(
                issues,
                WARNING,
                "slow_member",
                f"Slot {slot} is qualified {qualification}.",
            )

    for first_slot, second_slot in plan.pairs:
        first = by_slot.get(first_slot)
        second = by_slot.get(second_slot)
        if first is None or second is None:
            continue
        first_capacity = first.capacity_bytes
        second_capacity = second.capacity_bytes
        if first_capacity is None or second_capacity is None:
            _add(
                issues,
                WARNING,
                "unknown_capacity",
                f"Slots {first_slot} and {second_slot} cannot be checked for capacity mismatch.",
            )
            continue
        larger = max(first_capacity, second_capacity)
        if larger and abs(first_capacity - second_capacity) / larger >= CAPACITY_MISMATCH_WARNING_RATIO:
            _add(
                issues,
                WARNING,
                "capacity_mismatch",
                f"Slots {first_slot} and {second_slot} differ in capacity by at least 10%.",
            )

    if any(issue.severity == NOT_READY for issue in issues):
        status = NOT_READY
    elif issues:
        status = WARNING
    else:
        status = READY
    return Raid10Readiness(status, tuple(issues))


def readiness_summary(plan: SavedRaid10Plan, readiness: Raid10Readiness) -> str:
    lines = [
        "USB Array Manager — Read-only RAID10 plan",
        f"Saved: {plan.saved_at}",
        f"Readiness: {readiness.status}",
        "",
        "Mirror layout:",
    ]
    lines.extend(
        f"- Pair {index + 1}: Slot {first} + Slot {second}"
        for index, (first, second) in enumerate(plan.pairs)
    )
    lines.extend(("", "Readiness reasons:"))
    if readiness.issues:
        lines.extend(
            f"- {issue.severity}: {issue.message}" for issue in readiness.issues
        )
    else:
        lines.append("- All strict preflight checks passed.")
    lines.extend(
        (
            "",
            "This is a planning record only. It does not create, format, or modify an array or disk.",
        )
    )
    return "\n".join(lines) + "\n"


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _add(
    issues: list[ReadinessIssue], severity: str, code: str, message: str
) -> None:
    issues.append(ReadinessIssue(severity, code, message))

