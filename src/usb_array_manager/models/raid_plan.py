from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlannerDrive:
    slot: int
    model: str
    capacity_bytes: int | None
    sequential_read_mbps: float | None
    sustained_write_mbps: float | None
    is_connected: bool


@dataclass(frozen=True, slots=True)
class MirrorPairEstimate:
    label: str
    first: PlannerDrive
    second: PlannerDrive
    usable_capacity_bytes: int | None
    estimated_write_mbps: float | None
    conservative_read_mbps: float | None
    theoretical_max_read_mbps: float | None
    bottleneck: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Raid10Estimate:
    pairs: tuple[MirrorPairEstimate, ...]
    total_raw_capacity_bytes: int | None
    total_usable_capacity_bytes: int | None
    capacity_efficiency_percent: float | None
    estimated_sustained_write_mbps: float | None
    conservative_read_mbps: float | None
    theoretical_max_read_mbps: float | None
    warnings: tuple[str, ...]
