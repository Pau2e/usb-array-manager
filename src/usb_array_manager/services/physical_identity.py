"""Pure, auditable physical proof over the existing StorageDevice identity fields.

No enumeration, filesystem writes, slot reassignment or storage commands live here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from usb_array_manager.models.storage_device import StorageDevice

PROVEN = "PROVEN MATCH"
NO_MATCH = "NO MATCH"
AMBIGUOUS = "AMBIGUOUS MATCH"
STRONG_FIELDS = ("storage_unique_id", "container_id", "usb_device_id", "pnp_device_id")
PROOF_FIELDS = (
    *STRONG_FIELDS,
    "storage_unique_id_format",
    "serial_number",
    "model",
    "capacity_bytes",
)
POLICY_VERSION = 1


def normalize_field(field: str, value):
    """Trim/case only for identifiers; do not shorten or unescape instance IDs."""
    if field == "capacity_bytes":
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
            return number if number > 0 and str(value).strip() == str(number) else None
        except (ValueError, TypeError):
            return None
    if value is None or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if field == "container_id":
        try:
            parsed = uuid.UUID(text)
            return str(parsed).upper() if parsed.int else None
        except ValueError:
            return text.upper()  # preserve invalid evidence; never a proof anchor
    if field == "model":
        return " ".join(text.split()).upper()
    # Internal serial/instance-ID characters and whitespace are distinguishing.
    return text.upper()


def valid_container(value) -> bool:
    try:
        return bool(value and uuid.UUID(value).int)
    except ValueError:
        return False


def identity_values(device: StorageDevice) -> dict:
    return {
        field: normalize_field(field, getattr(device, field)) for field in PROOF_FIELDS
    }


@dataclass(frozen=True)
class CandidateComparison:
    device: StorageDevice
    saved: dict
    current: dict
    matched: tuple[str, ...]
    conflicts: tuple[str, ...]
    missing: tuple[str, ...]
    policy: str | None
    reasons: tuple[str, ...]

    @property
    def proven(self):
        return self.policy is not None and not self.reasons


def compare_physical(
    saved: StorageDevice, current: StorageDevice
) -> CandidateComparison:
    first, second = identity_values(saved), identity_values(current)
    matched = tuple(
        field
        for field in PROOF_FIELDS
        if first[field] is not None and first[field] == second[field]
    )
    conflicts = tuple(
        field
        for field in PROOF_FIELDS
        if first[field] is not None
        and second[field] is not None
        and first[field] != second[field]
    )
    missing = tuple(
        field
        for field in PROOF_FIELDS
        if first[field] is not None and second[field] is None
    )
    reasons = []
    if not current.is_connected:
        reasons.append("Device disconnected")
    if conflicts:
        reasons.append("Conflicting saved evidence: " + ", ".join(conflicts))
    if missing:
        reasons.append("Saved evidence unavailable now: " + ", ".join(missing))
    required = {"pnp_device_id", "serial_number", "model", "capacity_bytes"}
    if not required.issubset(matched):
        reasons.append(
            "Saved PnP + serial with model/capacity corroboration not fully matched"
        )
    policy = None
    if "storage_unique_id" in matched and required.issubset(matched):
        policy = "saved-storage-id+pnp+serial"
    elif (
        first["storage_unique_id"] is None
        and {"usb_device_id", "container_id"}.issubset(matched)
        and valid_container(first["container_id"])
        and required.issubset(matched)
    ):
        policy = "saved-usb-parent+container+pnp+serial"
    if policy is None:
        reasons.append("Neither complete saved physical proof chain is available")
    return CandidateComparison(
        current, first, second, matched, conflicts, missing, policy, tuple(reasons)
    )


@dataclass(frozen=True)
class IdentityReconciliation:
    slot: int
    result: str
    comparisons: tuple[CandidateComparison, ...]
    reason: str
    legacy: bool
    saved_record: dict

    @property
    def proven_candidates(self):
        return tuple(item for item in self.comparisons if item.proven)

    @property
    def target(self):
        return self.proven_candidates[0].device if self.result == PROVEN else None

    def describe(self) -> str:
        lines = [
            f"Slot {self.slot} identity reconciliation: {self.result}",
            "Saved slot schema: 1; application generation: not stamped in legacy data",
            f"Legacy/unversioned preparation proof: {'YES' if self.legacy else 'NO'}",
            "Saved identity record: " + json.dumps(self.saved_record, sort_keys=True),
            f"Current candidates examined: {len(self.comparisons)}; full-proof candidates: {len(self.proven_candidates)}",
            "Reason: " + self.reason,
            "Identity migration: not attempted by diagnostics; explicit record-proof action required.",
        ]
        for index, candidate in enumerate(self.comparisons, 1):
            lines += [
                f"Candidate {index}: serial={candidate.device.serial_number}; PnP={candidate.device.pnp_device_id}; "
                f"result={'FULL PHYSICAL PROOF' if candidate.proven else 'REJECTED'}",
                "  matched=" + ", ".join(candidate.matched),
                "  conflicts="
                + ", ".join(candidate.conflicts)
                + "; unavailable="
                + ", ".join(candidate.missing),
                "  policy="
                + str(candidate.policy)
                + "; "
                + "; ".join(candidate.reasons),
            ]
            for field in PROOF_FIELDS:
                a, b = candidate.saved[field], candidate.current[field]
                match = (
                    "MATCH"
                    if a is not None and a == b
                    else "ABSENT (not evidence)"
                    if a is None
                    else "DIFFERENT/UNAVAILABLE"
                )
                lines.append(f"  {field}: saved={a!r}; current={b!r}; {match}")
        return "\n".join(lines)


def reconcile_physical(
    slot: int,
    saved: StorageDevice,
    devices: list[StorageDevice],
    *,
    other_references: tuple[StorageDevice, ...] = (),
    legacy=True,
    saved_record: dict | None = None,
) -> IdentityReconciliation:
    comparisons = tuple(
        compare_physical(saved, current) for current in devices if current.is_connected
    )
    proven = [item for item in comparisons if item.proven]
    result, reason = (
        NO_MATCH,
        "Zero current devices satisfy a complete saved physical proof chain.",
    )
    if len(proven) > 1:
        result, reason = (
            AMBIGUOUS,
            "Multiple current devices satisfy the saved physical proof.",
        )
    elif len(proven) == 1:
        target = proven[0]
        overlaps = []
        for item in comparisons:
            if item is target:
                continue
            shared = [
                field
                for field in STRONG_FIELDS
                if target.current[field] is not None
                and target.current[field] == item.current[field]
            ]
            if shared:
                overlaps.append("another current device shares " + ", ".join(shared))
        for reference in other_references:
            other = identity_values(reference)
            shared = [
                field
                for field in STRONG_FIELDS
                if target.current[field] is not None
                and target.current[field] == other[field]
            ]
            if shared:
                overlaps.append(f"another reserved slot shares {', '.join(shared)}")
        if overlaps:
            result, reason = AMBIGUOUS, "; ".join(overlaps)
        else:
            result, reason = (
                PROVEN,
                "Exactly one physical member proven by "
                + target.policy
                + "; no conflicting/shared strong evidence.",
            )
    return IdentityReconciliation(
        slot,
        result,
        comparisons,
        reason,
        legacy,
        saved_record or identity_values(saved),
    )
