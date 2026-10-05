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

