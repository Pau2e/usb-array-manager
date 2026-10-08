from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan


SCHEMA_VERSION = 1


class Raid10PlanStoreError(RuntimeError):
    """Raised when a saved RAID10 plan cannot be read or written safely."""


def default_raid10_plan_path() -> Path:
    return Path.home() / "Documents" / "USBArrayManager" / "raid10_plan.json"


class Raid10PlanStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_raid10_plan_path()

    def load(self) -> SavedRaid10Plan | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return _plan_from_data(data)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise Raid10PlanStoreError(
                f"Could not read RAID10 plan: {self.path}"
            ) from error

    def save(
        self,
        pairs: tuple[tuple[int, int], ...] | list[tuple[int, int]],
        *,
        saved_at: str | None = None,
    ) -> SavedRaid10Plan:
        plan = SavedRaid10Plan(
            pairs=tuple((int(first), int(second)) for first, second in pairs),
            saved_at=saved_at or datetime.now(timezone.utc).isoformat(),
        )
        _validate_plan(plan)
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": plan.saved_at,
            "pairs": [[first, second] for first, second in plan.pairs],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(".tmp")
            with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise Raid10PlanStoreError(
                f"Could not save RAID10 plan: {self.path}"
            ) from error
        return plan


def _plan_from_data(data: Any) -> SavedRaid10Plan:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema")
    raw_pairs = data.get("pairs")
    saved_at = data.get("saved_at")
    if not isinstance(raw_pairs, list) or not isinstance(saved_at, str):
        raise ValueError("missing plan fields")
    pairs: list[tuple[int, int]] = []
    for raw_pair in raw_pairs:
        if not isinstance(raw_pair, list) or len(raw_pair) != 2:
            raise ValueError("invalid mirror pair")
        if any(isinstance(slot, bool) or not isinstance(slot, int) for slot in raw_pair):
            raise ValueError("invalid slot")
        pairs.append((raw_pair[0], raw_pair[1]))
    plan = SavedRaid10Plan(tuple(pairs), saved_at)
    _validate_plan(plan)
    return plan


def _validate_plan(plan: SavedRaid10Plan) -> None:
    if not plan.pairs:
        raise ValueError("A RAID10 plan must contain at least one mirror pair.")
    slots = plan.slots
    if any(slot < 1 or slot > 4 for slot in slots):
        raise ValueError("RAID10 plan slots must be between 1 and 4.")
    if len(slots) != len(set(slots)):
        raise ValueError("Each logical slot may appear only once in a RAID10 plan.")
    try:
        datetime.fromisoformat(plan.saved_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("The plan saved time is invalid.") from error

