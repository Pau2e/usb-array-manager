from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Mapping
from typing import Any

from usb_array_manager.models.storage_device import StorageDevice


class DeviceDetectionError(RuntimeError):
    """Raised when Windows device inventory cannot be queried or parsed."""


_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$canReadPnpProperties = $null -ne (
    Get-Command -Name Get-PnpDeviceProperty -ErrorAction SilentlyContinue
)

$devices = @(
    Get-CimInstance -ClassName Win32_DiskDrive |
        Where-Object {
            $_.InterfaceType -eq 'USB' -or $_.PNPDeviceID -like 'USB*'
        } |
        ForEach-Object {
            $disk = $_
            $usbDeviceId = $null

            if ($canReadPnpProperties) {
                try {
                    $parentProperty = Get-PnpDeviceProperty `
                        -InstanceId $disk.PNPDeviceID `
                        -KeyName 'DEVPKEY_Device_Parent' `
                        -ErrorAction Stop
                    $usbDeviceId = $parentProperty.Data
                }
                catch {
                    # Some USB bridges do not expose a readable parent property.
                }
            }

            $letters = @(
                Get-CimAssociatedInstance -InputObject $disk `
                    -Association Win32_DiskDriveToDiskPartition `
                    -ResultClassName Win32_DiskPartition |
                    ForEach-Object {
                        Get-CimAssociatedInstance -InputObject $_ `
                            -Association Win32_LogicalDiskToPartition `
                            -ResultClassName Win32_LogicalDisk
                    } |
                    Where-Object { $_.DeviceID } |
                    Select-Object -ExpandProperty DeviceID -Unique |
                    Sort-Object
            )

            [PSCustomObject]@{
                model          = $disk.Model
                serial_number  = $disk.SerialNumber
                capacity_bytes = $disk.Size
                drive_letters  = $letters
                device_path    = $disk.DeviceID
                pnp_device_id  = $disk.PNPDeviceID
                usb_device_id  = $usbDeviceId
            }
        }
)

ConvertTo-Json -InputObject $devices -Depth 4 -Compress
"""

_VID_PID_PATTERN = re.compile(
    r"VID_([0-9A-F]{4}).*?PID_([0-9A-F]{4})",
    flags=re.IGNORECASE,
)


def detect_usb_storage_devices(timeout_seconds: int = 20) -> list[StorageDevice]:
    """Return the USB storage disks currently reported by Windows."""
    encoded_script = base64.b64encode(
        _POWERSHELL_SCRIPT.encode("utf-16-le")
    ).decode("ascii")

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as error:
        raise DeviceDetectionError(
            "Windows PowerShell was not found. This detector requires Windows 10 "
            "with powershell.exe available."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DeviceDetectionError(
            f"The Windows device query did not finish within {timeout_seconds} seconds."
        ) from error
    except OSError as error:
        raise DeviceDetectionError(f"PowerShell could not be started: {error}") from error

    stdout = _decode_process_output(result.stdout)
    stderr = _decode_process_output(result.stderr).strip()

    if result.returncode != 0:
        detail = stderr or f"PowerShell exited with code {result.returncode}."
        raise DeviceDetectionError(detail)

    if not stdout.strip():
        raise DeviceDetectionError("Windows returned an empty device-inventory response.")

    try:
        records = json.loads(stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as error:
        raise DeviceDetectionError(
            "Windows returned device information in an unexpected format."
        ) from error

    if not isinstance(records, list):
        raise DeviceDetectionError("Windows returned an invalid device list.")

    devices: list[StorageDevice] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise DeviceDetectionError("Windows returned an invalid device record.")
        devices.append(_record_to_device(record))

    return devices


def _decode_process_output(output: bytes) -> str:
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        # Older Windows PowerShell configurations can still use the active code page.
        return output.decode(errors="replace")


def _record_to_device(record: Mapping[str, Any]) -> StorageDevice:
    pnp_device_id = _clean_text(record.get("pnp_device_id"))
    usb_device_id = _clean_text(record.get("usb_device_id"))
    vid, pid = _extract_vid_pid(usb_device_id or pnp_device_id)

    raw_letters = record.get("drive_letters")
    if isinstance(raw_letters, str):
        raw_letters = [raw_letters]
    if not isinstance(raw_letters, list):
        raw_letters = []

    capacity = record.get("capacity_bytes")
    try:
        capacity_bytes = int(capacity) if capacity is not None else None
    except (TypeError, ValueError):
        capacity_bytes = None

    return StorageDevice(
        model=_clean_text(record.get("model")),
        serial_number=_clean_text(record.get("serial_number")),
        capacity_bytes=capacity_bytes,
        drive_letters=tuple(
            letter
            for value in raw_letters
            if (letter := _clean_text(value)) is not None
        ),
        device_path=_clean_text(record.get("device_path")),
        pnp_device_id=pnp_device_id,
        usb_vid=vid,
        usb_pid=pid,
        is_connected=True,
    )


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = "".join(character for character in str(value) if character.isprintable())
    cleaned = cleaned.strip()
    return cleaned or None


def _extract_vid_pid(pnp_device_id: str | None) -> tuple[str | None, str | None]:
    if not pnp_device_id:
        return None, None

    match = _VID_PID_PATTERN.search(pnp_device_id)
    if not match:
        return None, None

    return match.group(1).upper(), match.group(2).upper()
