from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SavedRaid10Plan:
    """A durable, read-only RAID10 layout expressed only in logical slots."""

    pairs: tuple[tuple[int, int], ...]
    saved_at: str

    @property
    def slots(self) -> tuple[int, ...]:
        return tuple(slot for pair in self.pairs for slot in pair)


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    severity: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Raid10Readiness:
    status: str
    issues: tuple[ReadinessIssue, ...]

