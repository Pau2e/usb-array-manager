from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from usb_array_manager.models.storage_device import StorageDevice


SLOT_COUNT = 4
SCHEMA_VERSION = 1
_IDENTITY_MATCH_ORDER = (
    "storage_unique_id",
    "container_id",
    "usb_device_id",
    "pnp_device_id",
    "fingerprint",
    "location_paths",
)


class SlotStoreError(RuntimeError):
    """Raised when slot configuration cannot be read or written safely."""


class SlotConflictError(SlotStoreError):
    """Raised when a requested slot is already reserved."""


def default_slot_config_path() -> Path:
    return Path.home() / "Documents" / "USBArrayManager" / "slots.json"


def legacy_slot_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base_directory = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return base_directory / "USBArrayManager" / "slots.json"


class SlotStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        legacy_path: Path | None = None,
    ) -> None:
        uses_default_path = path is None
        self.path = path or default_slot_config_path()
        self._legacy_path = (
            legacy_path
            if legacy_path is not None
            else legacy_slot_config_path() if uses_default_path else None
        )

    def reconcile(self, devices: list[StorageDevice]) -> list[StorageDevice]:
        assignments = self._load_assignments()
        identities = [_identity_from_device(device) for device in devices]
        matches = _match_assignments(identities, assignments)

        displayed: list[StorageDevice] = []
        matched_slots: set[int] = set()
        changed = False

        for index, device in enumerate(devices):
            slot = matches.get(index)
            displayed.append(replace(device, slot=slot))
            if slot is None:
                continue

            matched_slots.add(slot)
            updated_record = {
                "identity": identities[index],
                "last_known_device": _snapshot_from_device(device),
            }
            if assignments.get(slot) != updated_record:
                assignments[slot] = updated_record
                changed = True

        for slot, record in assignments.items():
            if slot in matched_slots:
                continue
            disconnected = _device_from_snapshot(record["last_known_device"])
            displayed.append(replace(disconnected, slot=slot, is_connected=False))

        if changed:
            self._save_assignments(assignments)

        return sorted(
            displayed,
            key=lambda device: (
                device.slot is None,
                device.slot if device.slot is not None else SLOT_COUNT + 1,
                device.device_path or "",
            ),
        )

    def assign(self, device: StorageDevice, slot: int | None) -> None:
        if slot is not None and not 1 <= slot <= SLOT_COUNT:
            raise ValueError(f"Slot must be between 1 and {SLOT_COUNT}.")

        assignments = self._load_assignments()
        current_slot = device.slot

        if slot is not None and slot in assignments and slot != current_slot:
            raise SlotConflictError(f"Slot {slot} is already reserved.")

        if current_slot is not None:
            assignments.pop(current_slot, None)

        if slot is not None:
            assignments[slot] = {
                "identity": _identity_from_device(device),
                "last_known_device": _snapshot_from_device(device),
            }

        self._save_assignments(assignments)

    def reserved_slots(self) -> set[int]:
        return set(self._load_assignments())

    def ambiguous_slots(self, devices: list[StorageDevice]) -> set[int]:
        """Return reserved slots that have identity evidence but no unique match."""
        assignments = self._load_assignments()
        identities = [_identity_from_device(device) for device in devices]
        matches = _match_assignments(identities, assignments)
        unmatched_devices = set(range(len(devices))) - set(matches)
        unmatched_slots = set(assignments) - set(matches.values())
        ambiguous: set[int] = set()
        for field in _IDENTITY_MATCH_ORDER:
            device_values = _group_identity_values(
                {index: identities[index] for index in unmatched_devices}, field
            )
            slot_values = _group_identity_values(
                {slot: assignments[slot]["identity"] for slot in unmatched_slots},
                field,
            )
            for value in device_values.keys() & slot_values.keys():
                if len(device_values[value]) != 1 or len(slot_values[value]) != 1:
                    ambiguous.update(slot_values[value])
        return ambiguous

    def _load_assignments(self) -> dict[int, dict[str, Any]]:
        if not self.path.exists():
            if self._legacy_path is not None and self._legacy_path.exists():
                assignments = self._read_assignments(self._legacy_path)
                self._save_assignments(assignments)
                return assignments
            return {}

        return self._read_assignments(self.path)

    def _read_assignments(self, path: Path) -> dict[int, dict[str, Any]]:
        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SlotStoreError(
                f"Could not read slot configuration: {path}"
            ) from error

        if not isinstance(raw_data, dict) or raw_data.get("schema_version") != 1:
            raise SlotStoreError("The slot configuration has an unsupported format.")

        raw_slots = raw_data.get("slots")
        if not isinstance(raw_slots, dict):
            raise SlotStoreError("The slot configuration is missing its slot records.")

        assignments: dict[int, dict[str, Any]] = {}
        for raw_slot, record in raw_slots.items():
            try:
                slot = int(raw_slot)
            except (TypeError, ValueError) as error:
                raise SlotStoreError("The slot configuration contains an invalid slot.") from error

            if not 1 <= slot <= SLOT_COUNT:
                raise SlotStoreError("The slot configuration contains an invalid slot.")
            if not isinstance(record, dict):
                raise SlotStoreError("The slot configuration contains an invalid record.")
            if not isinstance(record.get("identity"), dict) or not isinstance(
                record.get("last_known_device"), dict
            ):
                raise SlotStoreError("The slot configuration contains an invalid record.")
            assignments[slot] = record

        return assignments

    def _save_assignments(self, assignments: dict[int, dict[str, Any]]) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "slots": {
                str(slot): assignments[slot] for slot in sorted(assignments)
            },
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
            raise SlotStoreError(
                f"Could not save slot configuration: {self.path}"
            ) from error


def _identity_from_device(device: StorageDevice) -> dict[str, Any]:
    storage_unique_id = _normalize(device.storage_unique_id)
    unique_id_format = _normalize(device.storage_unique_id_format)
    if storage_unique_id and unique_id_format:
        storage_unique_id = f"{unique_id_format}:{storage_unique_id}"

    fingerprint_parts = {
        "model": _normalize(device.model),
        "capacity_bytes": device.capacity_bytes,
        "usb_vid": _normalize(device.usb_vid),
        "usb_pid": _normalize(device.usb_pid),
        "serial_number": _normalize(device.serial_number),
    }
    fingerprint = None
    if fingerprint_parts["model"] and fingerprint_parts["capacity_bytes"] is not None:
        encoded = json.dumps(
            fingerprint_parts, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()

    return {
        "storage_unique_id": storage_unique_id,
        "container_id": _normalize(device.container_id),
        "usb_device_id": _normalize(device.usb_device_id),
        "pnp_device_id": _normalize(device.pnp_device_id),
        "fingerprint": fingerprint,
        "location_paths": sorted(
            location
            for path in device.location_paths
            if (location := _normalize(path)) is not None
        ),
    }


def _match_assignments(
    device_identities: list[dict[str, Any]],
    assignments: dict[int, dict[str, Any]],
) -> dict[int, int]:
    unmatched_devices = set(range(len(device_identities)))
    unmatched_slots = set(assignments)
    matches: dict[int, int] = {}

    for field in _IDENTITY_MATCH_ORDER:
        device_values = _group_identity_values(
            {index: device_identities[index] for index in unmatched_devices}, field
        )
        slot_values = _group_identity_values(
            {
                slot: assignments[slot]["identity"]
                for slot in unmatched_slots
            },
            field,
        )

        candidate_pairs: set[tuple[int, int]] = set()
        for value in device_values.keys() & slot_values.keys():
            if len(device_values[value]) == 1 and len(slot_values[value]) == 1:
                candidate_pairs.add(
                    (device_values[value][0], slot_values[value][0])
                )

        device_candidates: dict[int, set[int]] = {}
        slot_candidates: dict[int, set[int]] = {}
        for device_index, slot in candidate_pairs:
            device_candidates.setdefault(device_index, set()).add(slot)
            slot_candidates.setdefault(slot, set()).add(device_index)

        for device_index, slots in device_candidates.items():
            if len(slots) != 1:
                continue
            slot = next(iter(slots))
            if len(slot_candidates.get(slot, set())) != 1:
                continue
            matches[device_index] = slot

        unmatched_devices.difference_update(matches)
        unmatched_slots.difference_update(matches.values())

    return matches


def _group_identity_values(
    identities: dict[int, dict[str, Any]], field: str
) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for identifier, identity in identities.items():
        raw_value = identity.get(field)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            if isinstance(value, str) and value:
                grouped.setdefault(value, []).append(identifier)
    return grouped


def _snapshot_from_device(device: StorageDevice) -> dict[str, Any]:
    snapshot = asdict(device)
    snapshot.pop("slot", None)
    return snapshot


def _device_from_snapshot(snapshot: dict[str, Any]) -> StorageDevice:
    return StorageDevice(
        model=_optional_text(snapshot.get("model")),
        serial_number=_optional_text(snapshot.get("serial_number")),
        capacity_bytes=_optional_int(snapshot.get("capacity_bytes")),
        drive_letters=_text_tuple(snapshot.get("drive_letters")),
        device_path=_optional_text(snapshot.get("device_path")),
        pnp_device_id=_optional_text(snapshot.get("pnp_device_id")),
        usb_vid=_optional_text(snapshot.get("usb_vid")),
        usb_pid=_optional_text(snapshot.get("usb_pid")),
        is_connected=False,
        bus_type=_optional_text(snapshot.get("bus_type")),
        media_type=_optional_text(snapshot.get("media_type")),
        can_pool=snapshot.get("can_pool") if isinstance(snapshot.get("can_pool"), bool) else None,
        health_status=_optional_text(snapshot.get("health_status")),
        usb_device_id=_optional_text(snapshot.get("usb_device_id")),
        storage_unique_id=_optional_text(snapshot.get("storage_unique_id")),
        storage_unique_id_format=_optional_text(
            snapshot.get("storage_unique_id_format")
        ),
        container_id=_optional_text(snapshot.get("container_id")),
        location_paths=_text_tuple(snapshot.get("location_paths")),
        disk_number=_optional_int(snapshot.get("disk_number")),
        is_removable=_optional_bool(snapshot.get("is_removable")),
        is_boot_disk=_optional_bool(snapshot.get("is_boot_disk")),
        is_system_disk=_optional_bool(snapshot.get("is_system_disk")),
        is_read_only=_optional_bool(snapshot.get("is_read_only")),
        is_offline=_optional_bool(snapshot.get("is_offline")),
        partition_count=_optional_int(snapshot.get("partition_count")),
        partition_style=_optional_text(snapshot.get("partition_style")),
        filesystem_types=_text_tuple(snapshot.get("filesystem_types")),
        operational_status=_optional_text(snapshot.get("operational_status")),
    )


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).upper()
    return normalized or None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()
