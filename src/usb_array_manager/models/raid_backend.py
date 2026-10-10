from __future__ import annotations

from dataclasses import dataclass


SUPPORTED = "SUPPORTED"
PARTIALLY_SUPPORTED = "PARTIALLY SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BackendDriveAssessment:
    slot: int
    pair: str
    physical_disk: str
    model: str
    identity: str
    capacity_bytes: int | None
    bus_type: str
    media_type: str
    removable: str
    can_pool: str
    partition_state: str
    health: str
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackendAssessment:
    backend_name: str
    status: str
    explanation: str
    requirements: tuple[str, ...]
    blockers: tuple[str, ...]
    drives: tuple[BackendDriveAssessment, ...]
    dry_run_text: str
