"""Read-only presentation of health provenance and the existing preparation gate."""

from __future__ import annotations

import json

from usb_array_manager.models.storage_device import StorageDevice

DIRTY_FORMAT_REVIEW = (
    "FILESYSTEM DIRTY — DESTRUCTIVE FORMAT MAY CONTINUE WITH EXTRA ACKNOWLEDGEMENT"
)


def dirty_format_exception(device: StorageDevice) -> bool:
    """Only a completely observed FAT32 dirty/repair warning can be reviewed.

    This is not a health upgrade or permission to format. Identity, exclusions,
    confirmation and final Windows checks remain the caller's responsibility.
    """
    if len(device.host_volumes) != 1:
        return False
    volume = device.host_volumes[0]
    disk = _evidence(device.health_details_json) or {}
    raw = _evidence(volume.health_details_json) or {}
    storage = raw.get("storage") or {}
    current_disk = disk.get("get_disk") or {}
    if not isinstance(storage, dict) or not isinstance(current_disk, dict):
        return False
    return (
        (device.health_status or "").upper() in {"HEALTHY", "OK"}
        and current_disk.get("health_value") == "Healthy"
        and type(current_disk.get("health_cim_raw")) is int
        and current_disk.get("health_cim_raw") == 0
        and current_disk.get("operational_value") == ["Online"]
        and current_disk.get("operational_cim_raw") == [53264]
        and not disk.get("get_disk_error")
        and volume.filesystem == "FAT32"
        and volume.health_status == "Warning"
        and raw.get("source") == "Get-Volume.HealthStatus"
        and raw.get("selected_raw") == "Warning"
        and raw.get("storage_match_count") == 1
        and not raw.get("storage_query_error")
        and raw.get("file_system") == "FAT32"
        and raw.get("automount") is True
        and raw.get("dirty_bit_set") is True
        and raw.get("logical_disk_dirty") is True
        and raw.get("win32_status") in (None, "OK")
        and storage.get("health_value") == "Warning"
        and type(storage.get("health_cim_raw")) is int
        and storage.get("health_cim_raw") == 1
        and storage.get("operational_value") == ["Full Repair Needed"]
        and storage.get("operational_cim_raw") == [53263]
    )


def physical_health_allows(device: StorageDevice) -> bool:
    if (device.health_status or "").upper() not in {"HEALTHY", "OK"}:
        return False
    if device.health_details_json is None:
        return True  # Legacy clean path; final Get-Disk still has to prove health.
    raw = _evidence(device.health_details_json)
    if raw is None:
        return False
    disk = raw.get("get_disk")
    return (
        isinstance(disk, dict)
        and disk.get("health_value") == "Healthy"
        and type(disk.get("health_cim_raw")) is int
        and disk.get("health_cim_raw") == 0
        and disk.get("operational_value") == ["Online"]
        and disk.get("operational_cim_raw") == [53264]
        and not raw.get("get_disk_error")
    )


def healthy_volume(device: StorageDevice) -> bool:
    if len(device.host_volumes) != 1:
        return False
    volume = device.host_volumes[0]
    if (volume.health_status or "").upper() not in {"HEALTHY", "OK"}:
        return False
    if volume.health_details_json is None:
        return True  # Legacy clean observations still require final Windows proof.
    raw = _evidence(volume.health_details_json)
    if not raw or not isinstance(raw.get("storage"), dict):
        return False
    storage = raw["storage"]
    return (
        raw.get("source") == "Get-Volume.HealthStatus"
        and raw.get("storage_match_count") == 1
        and not raw.get("storage_query_error")
        and raw.get("selected_raw") == "Healthy"
        and storage.get("health_value") == "Healthy"
        and type(storage.get("health_cim_raw")) is int
        and storage.get("health_cim_raw") == 0
        and storage.get("operational_value") == ["OK"]
        and storage.get("operational_cim_raw") == [2]
        and raw.get("dirty_bit_set") is False
        and raw.get("logical_disk_dirty") is False
    )


def health_review_key(device: StorageDevice) -> tuple:
    """Bind acknowledgement to the observed warning, not changing free space."""
    return (
        device.health_status,
        device.health_details_json,
        tuple((v.health_status, v.health_details_json) for v in device.host_volumes),
    )


def _evidence(value: str | None) -> dict | None:
    if value is None:
        return None
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else None
    except (TypeError, ValueError):
        return None


def _trace(label: str, normalized: str | None, details: str | None) -> list[str]:
    evidence = _evidence(details)
    allowed = (normalized or "").upper() in {"HEALTHY", "OK"}
    lines = [
        f"{label}: normalized={normalized!r}; health gate={'PASS' if allowed else 'BLOCKED'}",
        "Condition: (health_status or '').upper() must be HEALTHY or OK.",
    ]
    if evidence is None:
        return lines + [
            "Raw health provenance unavailable in this observation (legacy/missing diagnostics).",
            "No raw value, dirty bit or provider source is inferred from that absence.",
        ]
    lines += [
        f"Selected source: {evidence.get('source')}",
        f"Selected raw value: {evidence.get('selected_raw')!r} -> inventory normalization -> {normalized!r}",
        "Raw Windows evidence (diagnostic only):",
        json.dumps(evidence, indent=2, sort_keys=True),
    ]
    return lines


def describe_health(device: StorageDevice, preparation_status: str, reason: str) -> str:
    lines = [
        f"Slot {device.slot}: read-only health provenance / decision",
        f"Preparation result: {preparation_status}",
        f"Preparation reason: {reason}",
        "Health, operational state and dirty flags are distinct observations.",
        "These diagnostics do not scan, repair, format, clear a dirty bit or change eligibility.",
        "",
    ]
    lines += _trace("Physical disk", device.health_status, device.health_details_json)
    if len(device.host_volumes) != 1:
        return "\n".join(
            lines
            + ["Host-volume association is not unique; no volume health selected here."]
        )
    volume = device.host_volumes[0]
    lines += ["", f"Current host Volume GUID: {volume.identity}"]
    lines += _trace("Host volume", volume.health_status, volume.health_details_json)
    lines += [
        "",
        f"Physical-health decision from normalized + available raw evidence: {'PASS' if physical_health_allows(device) else 'BLOCKED'}",
        f"Clean-volume decision from normalized + available raw evidence: {'PASS' if healthy_volume(device) else 'BLOCKED'}",
        f"Dirty-filesystem exception: {DIRTY_FORMAT_REVIEW if dirty_format_exception(device) else 'NOT PROVEN; normal health gate applies'}",
        "Decision boundary: physical health must pass; only the fully evidenced FAT32 dirty/Full Repair Needed pattern permits destructive review with an additional acknowledgement.",
        "OperationalStatus and dirty flags are exposed as evidence; they are not remapped into health by this code.",
        "Unknown/missing numeric health values are retained as Unknown/None and block, never invented as Warning.",
        "The volume remains Warning. This exception is only for explicit destructive format; it never marks the member PREPARED or healthy.",
        "A dirty flag and Full Repair Needed describe filesystem state; these observations alone do not establish media failure.",
    ]
    return "\n".join(lines)
