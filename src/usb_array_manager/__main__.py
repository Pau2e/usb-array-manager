from __future__ import annotations

import sys

from usb_array_manager.services.windows_cim import (
    DeviceDetectionError,
    detect_usb_storage_devices,
)


def format_capacity(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unknown"

    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024

    return "Unknown"


def display_value(value: str | None) -> str:
    return value or "Unavailable"


def main() -> int:
    try:
        devices = detect_usb_storage_devices()
    except DeviceDetectionError as error:
        print(f"Unable to detect USB storage devices: {error}", file=sys.stderr)
        return 1

    if not devices:
        print("No connected USB storage devices were detected.")
        return 0

    print(f"Detected {len(devices)} connected USB storage device(s):\n")

    for number, device in enumerate(devices, start=1):
        drive_letters = ", ".join(device.drive_letters) or "None"
        print(f"Device {number}")
        print(f"  Model:        {display_value(device.model)}")
        print(f"  Serial:       {display_value(device.serial_number)}")
        print(f"  Capacity:     {format_capacity(device.capacity_bytes)}")
        print(f"  Drive letters:{' ' if drive_letters else ''}{drive_letters}")
        print(f"  Device path:  {display_value(device.device_path)}")
        print(f"  USB VID:      {display_value(device.usb_vid)}")
        print(f"  USB PID:      {display_value(device.usb_pid)}")
        print(f"  Connected:    {'Yes' if device.is_connected else 'No'}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

