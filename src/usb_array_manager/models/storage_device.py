from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StorageDevice:
    model: str | None
    serial_number: str | None
    capacity_bytes: int | None
    drive_letters: tuple[str, ...]
    device_path: str | None
    pnp_device_id: str | None
    usb_vid: str | None
    usb_pid: str | None
    is_connected: bool
    bus_type: str | None = None
    media_type: str | None = None
    can_pool: bool | None = None
    health_status: str | None = None
    usb_device_id: str | None = None
    storage_unique_id: str | None = None
    storage_unique_id_format: str | None = None
    container_id: str | None = None
    location_paths: tuple[str, ...] = ()
    slot: int | None = None
    disk_number: int | None = None
    is_removable: bool | None = None
    is_boot_disk: bool | None = None
    is_system_disk: bool | None = None
    is_read_only: bool | None = None
    is_offline: bool | None = None
    partition_count: int | None = None
    partition_style: str | None = None
    filesystem_types: tuple[str, ...] = ()
    operational_status: str | None = None
