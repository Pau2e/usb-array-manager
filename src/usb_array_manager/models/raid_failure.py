from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MirrorFailureState:
    label: str
    status: str
    member_slots: tuple[int, int]
    failed_slots: tuple[int, ...]
    working_slots: tuple[int, ...]
    conservative_read_mbps: float | None
    theoretical_max_read_mbps: float | None


@dataclass(frozen=True, slots=True)
class Raid10FailureSimulation:
    pair_states: tuple[MirrorFailureState, ...]
    array_status: str
    explanation: str
    failed_slots: tuple[int, ...]
    remaining_working_slots: tuple[int, ...]
    lost_redundancy_pairs: tuple[str, ...]
    at_risk_pairs: tuple[str, ...]
    additional_failure_tolerance: str
    conservative_read_mbps: float | None
    theoretical_max_read_mbps: float | None
