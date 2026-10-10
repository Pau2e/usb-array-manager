from __future__ import annotations

import base64
import ctypes
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from usb_array_manager.models.raid_backend import (
    NEEDS_ADMINISTRATOR,
    PROBE_FAILED,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
)
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import (
    VhdxBenchmarkComparison,
    VhdxDiskInfo,
    VhdxProbeResult,
)
from usb_array_manager.services.benchmark import BenchmarkSettings, run_benchmark

MEBIBYTE = 1024 * 1024
TEST_FOLDER_NAME = ".usb-array-manager-vhdx-test"
ADMINISTRATOR_MESSAGE = (
    "VHDX testing requires Administrator privileges. Restart USB Array Manager "
    "as Administrator, then run the capability probe again."
)


class VhdxProbeError(RuntimeError):
    """Raised when a temporary VHDX operation cannot complete safely."""

    def __init__(
        self,
        message: str,
        *,
        details: str = "",
        unavailable: bool = False,
        needs_administrator: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = details
        self.unavailable = unavailable
        self.needs_administrator = needs_administrator


@dataclass(frozen=True, slots=True)
class VhdxProbeSettings:
    vhdx_size_bytes: int = 512 * MEBIBYTE
    free_space_reserve_bytes: int = 512 * MEBIBYTE
    benchmark_settings: BenchmarkSettings = field(
        default_factory=lambda: BenchmarkSettings(
            file_size_bytes=128 * MEBIBYTE,
            block_size_bytes=4 * MEBIBYTE,
            sample_size_bytes=32 * MEBIBYTE,
            burst_size_bytes=16 * MEBIBYTE,
            free_space_reserve_bytes=64 * MEBIBYTE,
            random_4k_operations=0,
        )
    )


class DiskPartVhdxRunner:
    """Manage one temporary VHDX with built-in DiskPart and inspect it via PowerShell."""

    def __init__(self, timeout_seconds: int = 60) -> None:
        self.timeout_seconds = timeout_seconds
        self._attached_disk_numbers: dict[str, int] = {}

    def create(self, path: Path, size_bytes: int) -> None:
        size_mebibytes = max(1, (size_bytes + MEBIBYTE - 1) // MEBIBYTE)
        output = self._run_diskpart(
            (
                f"create vdisk file={_diskpart_quote(str(path))} "
                f"maximum={size_mebibytes} type=expandable",
            )
        )
        if not path.exists():
            raise VhdxProbeError(
                "DiskPart did not create the temporary VHDX.",
                details=output,
            )

    def attach(self, path: Path, *, prepare_filesystem: bool) -> VhdxDiskInfo:
        before = self._enumerate_disks()
        output = self._run_diskpart(
            (
                f"select vdisk file={_diskpart_quote(str(path))}",
                "attach vdisk",
            )
        )
        after = self._enumerate_disks()
        new_numbers = sorted(set(after) - set(before))
        if len(new_numbers) != 1:
            raise VhdxProbeError(
                "Windows could not identify exactly one new disk after attaching the VHDX.",
                details=(
                    f"DiskPart output:\n{output}\n"
                    f"Before disk numbers: {sorted(before)}\n"
                    f"After disk numbers: {sorted(after)}"
                ),
            )
        disk = after[new_numbers[0]]
        self._attached_disk_numbers[str(path).casefold()] = disk.disk_number
        if disk.bus_type.casefold() != "file backed virtual":
            raise VhdxProbeError(
                "The newly attached disk was not verified as File Backed Virtual.",
                details=(
                    f"Detected Disk {disk.disk_number} with BusType={disk.bus_type}, "
                    f"identity={disk.identity}."
                ),
            )
        if prepare_filesystem:
            volume_root = self._prepare_filesystem(disk)
            disk = VhdxDiskInfo(
                disk.disk_number,
                disk.identity,
                disk.bus_type,
                disk.media_type,
                disk.can_pool,
                disk.operational_status,
                disk.health_status,
                volume_root,
            )
        return disk

    def detach(self, path: Path) -> None:
        output = self._run_diskpart(
            (
                f"select vdisk file={_diskpart_quote(str(path))}",
                "detach vdisk",
            )
        )
        expected = self._attached_disk_numbers.pop(str(path).casefold(), None)
        if expected is not None and expected in self._enumerate_disks():
            raise VhdxProbeError(
                f"DiskPart reported detach, but Disk {expected} is still present.",
                details=output,
            )

    def _run_diskpart(self, commands: tuple[str, ...]) -> str:
        executable = shutil.which("diskpart.exe") or shutil.which("diskpart")
        if executable is None:
            raise VhdxProbeError(
                "DiskPart is unavailable on this Windows installation.",
                unavailable=True,
            )
        descriptor, script_name = tempfile.mkstemp(prefix="uam-vhdx-", suffix=".txt")
        script_path = Path(script_name)
        try:
            os.close(descriptor)
            script_path.write_text("\n".join(commands) + "\n", encoding="utf-16")
            result = subprocess.run(
                [executable, "/s", str(script_path)],
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            if _requires_elevation(error):
                raise VhdxProbeError(
                    ADMINISTRATOR_MESSAGE,
                    details=repr(error),
                    needs_administrator=True,
                ) from error
            raise VhdxProbeError(
                "DiskPart could not complete the temporary VHDX operation.",
                details=str(error),
            ) from error
        finally:
            script_path.unlink(missing_ok=True)
        output = _clean_process_output(result.stdout, result.stderr)
        if result.returncode != 0:
            if _requires_elevation(output):
                raise VhdxProbeError(
                    ADMINISTRATOR_MESSAGE,
                    details=output,
                    needs_administrator=True,
                )
            raise VhdxProbeError(
                "DiskPart reported an error during the temporary VHDX operation.",
                details=output,
            )
        return output

    def _enumerate_disks(self) -> dict[int, VhdxDiskInfo]:
        script = r"""
$physicalDisks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue)
@(
    Get-Disk -ErrorAction Stop | ForEach-Object {
        $disk = $_
        $physical = @($physicalDisks | Where-Object {
            ([string]$_.DeviceId -eq [string]$disk.Number) -or
            ($disk.UniqueId -and ([string]$_.UniqueId -eq [string]$disk.UniqueId))
        }) | Select-Object -First 1
        [PSCustomObject]@{
            disk_number = $disk.Number
            identity = $disk.UniqueId
            bus_type = [string]$disk.BusType
            media_type = if ($physical) { [string]$physical.MediaType } else { 'Unspecified' }
            can_pool = if ($physical) { $physical.CanPool } else { $null }
            operational_status = if ($physical) { [string]$physical.OperationalStatus } else { [string]$disk.OperationalStatus }
            health_status = if ($physical) { [string]$physical.HealthStatus } else { 'Unknown' }
        }
    }
) | ConvertTo-Json -Depth 3 -Compress
"""
        return _disk_records(self._run_powershell(script))

    def _prepare_filesystem(self, disk: VhdxDiskInfo) -> str:
        if not disk.identity or disk.identity == "Unknown":
            raise VhdxProbeError(
                "The temporary VHDX identity could not be verified before initialization."
            )
        script = f"""
$disk = Get-Disk -Number {disk.disk_number} -ErrorAction Stop
if ([string]$disk.BusType -ne 'File Backed Virtual') {{
    throw 'Refusing to initialize a disk that is not File Backed Virtual.'
}}
if ([string]$disk.UniqueId -ne {_ps_quote(disk.identity)}) {{
    throw 'The verified temporary VHDX identity changed before initialization.'
}}
$partition = Initialize-Disk -Number $disk.Number -PartitionStyle GPT -PassThru -ErrorAction Stop |
    New-Partition -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
$partition | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'UAM_VHDX_TEST' -Confirm:$false -ErrorAction Stop | Out-Null
([string]$partition.DriveLetter) + ':\'
"""
        volume_root = self._run_powershell(script).strip()
        if not volume_root:
            raise VhdxProbeError(
                "The temporary VHDX filesystem did not receive a drive letter."
            )
        return volume_root

    def _run_powershell(self, script: str) -> str:
        script = (
            "$ErrorActionPreference='Stop'\n"
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)\n"
            "try {\n"
            + script
            + "\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            if _requires_elevation(error):
                raise VhdxProbeError(
                    ADMINISTRATOR_MESSAGE,
                    details=repr(error),
                    needs_administrator=True,
                ) from error
            raise VhdxProbeError(
                "Windows storage inspection could not be started.",
                details=str(error),
            ) from error
        stdout = result.stdout.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
        details = _clean_process_output(result.stdout, result.stderr)
        if result.returncode != 0:
            if _requires_elevation(details):
                raise VhdxProbeError(
                    ADMINISTRATOR_MESSAGE,
                    details=details,
                    needs_administrator=True,
                )
            raise VhdxProbeError(
                "Windows storage inspection failed.",
                details=details or f"PowerShell exited with code {result.returncode}.",
            )
        return stdout


def probe_vhdx_backend(
    device: StorageDevice,
    *,
    performance: bool = False,
    root: Path | None = None,
    settings: VhdxProbeSettings | None = None,
    runner: DiskPartVhdxRunner | None = None,
    name_factory: Callable[[], str] | None = None,
    cancel_event: threading.Event | None = None,
    elevation_checker: Callable[[], bool | None] | None = None,
) -> VhdxProbeResult:
    active_settings = settings or VhdxProbeSettings()
    active_runner = runner or DiskPartVhdxRunner()
    filesystem = ", ".join(device.filesystem_types) or "Unknown"
    source_identity = _device_identity(device)
    slot = device.slot or 0
    initial_blocker = _device_blocker(device)
    if initial_blocker:
        return _failure(
            slot,
            source_identity,
            "",
            filesystem,
            initial_blocker,
            "Not started",
            "Not needed",
            status=UNSUPPORTED,
        )
    elevated = (elevation_checker or windows_elevation_status)()
    if elevated is not True:
        if elevated is False:
            return _failure(
                slot,
                source_identity,
                "",
                filesystem,
                ADMINISTRATOR_MESSAGE,
                "Not started",
                "Not needed",
                status=NEEDS_ADMINISTRATOR,
                details="Windows elevation preflight returned non-elevated.",
            )
        return _failure(
            slot,
            source_identity,
            "",
            filesystem,
            "Windows could not determine whether the application has Administrator privileges. The VHDX probe was not started.",
            "Not started",
            "Not needed",
            status=UNKNOWN,
            details="Windows elevation preflight was unavailable or failed.",
        )
    drive_root = root or Path(f"{device.drive_letters[0]}\\")
    if not drive_root.exists() or not drive_root.is_dir():
        return _failure(slot, source_identity, "", filesystem, f"Mounted drive is unavailable: {drive_root}", "Not started", "Not needed")
    required = active_settings.vhdx_size_bytes + active_settings.free_space_reserve_bytes
    if performance:
        required += active_settings.benchmark_settings.file_size_bytes * 2
    try:
        free = shutil.disk_usage(drive_root).free
    except OSError as error:
        return _failure(slot, source_identity, "", filesystem, f"Could not read free space: {error}", "Not started", "Not needed")
    if free < required:
        return _failure(
            slot,
            source_identity,
            "",
            filesystem,
            f"Insufficient free space: {required / MEBIBYTE:.0f} MiB required; {free / MEBIBYTE:.0f} MiB available.",
            "Not started",
            "Not needed",
        )

    folder = drive_root / TEST_FOLDER_NAME
    created_folder = False
    token = (name_factory or (lambda: uuid.uuid4().hex))()
    path = folder / f"probe-{token}.vhdx"
    attach_attempted = False
    attach_status = "Not attached"
    cleanup_status = "Pending"
    disk_info: VhdxDiskInfo | None = None
    comparison: VhdxBenchmarkComparison | None = None
    owns_path = False
    creation_attempted = False
    operation_error: str | None = None
    operation_details = ""
    failure_status = PROBE_FAILED
    detach_error: str | None = None
    delete_error: str | None = None
    try:
        folder_preexisted = folder.exists()
        if folder_preexisted and (
            folder.is_symlink()
            or (hasattr(folder, "is_junction") and folder.is_junction())
        ):
            raise VhdxProbeError(
                f"Refusing redirected VHDX test folder: {folder}"
            )
        folder.mkdir(exist_ok=True)
        created_folder = not folder_preexisted
        if path.exists():
            raise VhdxProbeError(f"Refusing to overwrite existing test file: {path}")
        creation_attempted = True
        active_runner.create(path, active_settings.vhdx_size_bytes)
        if not path.exists():
            raise VhdxProbeError("VHDX creation command completed but the file was not found.")
        owns_path = True
        if cancel_event is not None and cancel_event.is_set():
            raise VhdxProbeError("VHDX probe cancelled before attach.")
        attach_attempted = True
        disk_info = active_runner.attach(path, prepare_filesystem=performance)
        attach_status = f"Attached as Disk {disk_info.disk_number}"
        if disk_info.bus_type.casefold() != "file backed virtual":
            raise VhdxProbeError(
                f"Attached disk reported unexpected BusType {disk_info.bus_type!r}."
            )
        if performance:
            if not disk_info.volume_root:
                raise VhdxProbeError("The temporary VHDX filesystem did not receive a drive letter.")
            comparison = _benchmark_comparison(
                device,
                drive_root,
                Path(disk_info.volume_root),
                active_settings.benchmark_settings,
                cancel_event,
            )
    except VhdxProbeError as error:
        operation_error = str(error)
        operation_details = error.details
        failure_status = (
            NEEDS_ADMINISTRATOR
            if error.needs_administrator
            else UNKNOWN if error.unavailable
            else PROBE_FAILED
        )
    except Exception as error:
        operation_error = "The temporary VHDX probe failed unexpectedly."
        operation_details = repr(error)
    finally:
        if attach_attempted:
            try:
                active_runner.detach(path)
                attach_status += "; detached"
            except Exception as error:
                detach_error = "The temporary VHDX could not be detached."
                operation_details = "\n".join(
                    item
                    for item in (
                        operation_details,
                        getattr(error, "details", "") or str(error),
                    )
                    if item
                )
                failure_status = PROBE_FAILED
        if owns_path and detach_error is None and path.exists():
            try:
                path.unlink()
            except OSError as error:
                delete_error = str(error)
        try:
            if created_folder and folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass

    ambiguous_creation_residue = creation_attempted and not owns_path and path.exists()
    leftover = str(path) if (owns_path and path.exists()) or ambiguous_creation_residue else None
    if detach_error:
        cleanup_status = f"FAILED to detach: {detach_error}"
    elif delete_error or leftover:
        cleanup_status = f"FAILED to delete: {delete_error or 'file remains'}"
    elif owns_path:
        cleanup_status = "Detached and temporary file deleted"
    else:
        cleanup_status = "No temporary file created"
    blockers = [item for item in (operation_error, detach_error, delete_error) if item]
    if leftover:
        blockers.append(
            "Manual cleanup required because the temporary path still exists: "
            f"{leftover}"
        )
    if blockers:
        return VhdxProbeResult(
            slot=slot,
            status=failure_status,
            vhdx_path=str(path),
            attach_status=attach_status,
            virtual_disk=disk_info,
            cleanup_status=cleanup_status,
            filesystem=filesystem,
            explanation=blockers[0],
            blockers=tuple(blockers),
            leftover_path=leftover,
            benchmark=comparison,
            source_identity=source_identity,
            diagnostic_details=operation_details,
        )
    if disk_info is None:
        return _failure(slot, source_identity, str(path), filesystem, "No virtual disk information was returned.", attach_status, cleanup_status)
    if disk_info.can_pool is not True:
        status = UNKNOWN if disk_info.can_pool is None else UNSUPPORTED
        explanation = (
            "Windows did not report Storage Spaces CanPool for the temporary virtual disk."
            if disk_info.can_pool is None
            else "Windows Storage Spaces reported CanPool=False for the temporary virtual disk."
        )
    else:
        status = SUPPORTED
        explanation = "Temporary VHDX creation, attach, Storage Spaces detection, detach, and cleanup succeeded."
    return VhdxProbeResult(
        slot=slot,
        status=status,
        vhdx_path=str(path),
        attach_status=attach_status,
        virtual_disk=disk_info,
        cleanup_status=cleanup_status,
        filesystem=filesystem,
        explanation=explanation,
        benchmark=comparison,
        source_identity=source_identity,
    )


def _device_blocker(device: StorageDevice) -> str | None:
    if device.is_system_disk is True or device.is_boot_disk is True:
        return "The selected USB disk is a Windows system or boot disk and is forbidden."
    if device.is_system_disk is None or device.is_boot_disk is None:
        return "Windows could not prove that the selected USB disk is not a system or boot disk."
    if not device.is_connected:
        return "The selected USB disk is disconnected."
    if not device.drive_letters:
        return "The selected USB disk has no mounted filesystem."
    if device.is_read_only is True:
        return "The selected USB disk is read-only."
    return None


def windows_elevation_status() -> bool | None:
    """Return the Windows Administrator-token state without changing it."""
    if os.name != "nt":
        return None
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return None


def _device_identity(device: StorageDevice) -> str:
    return (
        device.storage_unique_id
        or device.container_id
        or device.pnp_device_id
        or device.device_path
        or ""
    )


def _benchmark_comparison(
    device: StorageDevice,
    direct_root: Path,
    vhdx_root: Path,
    settings: BenchmarkSettings,
    cancel_event: threading.Event | None,
) -> VhdxBenchmarkComparison:
    slot = device.slot or 0
    direct = run_benchmark(
        direct_root,
        slot,
        str(direct_root),
        settings=settings,
        cancel_event=cancel_event,
    )
    virtual = run_benchmark(
        vhdx_root,
        slot,
        str(vhdx_root),
        settings=settings,
        cancel_event=cancel_event,
    )
    return VhdxBenchmarkComparison(
        direct.sequential_read_mbps,
        direct.sequential_write_mbps,
        direct.sustained_write_mbps,
        virtual.sequential_read_mbps,
        virtual.sequential_write_mbps,
        virtual.sustained_write_mbps,
    )


def _failure(
    slot: int,
    source_identity: str,
    path: str,
    filesystem: str,
    message: str,
    attach_status: str,
    cleanup_status: str,
    *,
    status: str = PROBE_FAILED,
    details: str = "",
) -> VhdxProbeResult:
    return VhdxProbeResult(
        slot,
        status,
        path,
        attach_status,
        None,
        cleanup_status,
        filesystem,
        message,
        (message,),
        source_identity=source_identity,
        diagnostic_details=details,
    )


def _disk_records(raw: str) -> dict[int, VhdxDiskInfo]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise VhdxProbeError(
            "Windows returned invalid disk inventory data.",
            details=raw,
        ) from error
    records = payload if isinstance(payload, list) else [payload]
    disks: dict[int, VhdxDiskInfo] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise VhdxProbeError("Windows returned an invalid disk inventory record.")
        disk = _disk_info_from_record(record)
        disks[disk.disk_number] = disk
    return disks


def _disk_info_from_record(record: Mapping[str, Any]) -> VhdxDiskInfo:
    try:
        number = int(record["disk_number"])
    except (KeyError, TypeError, ValueError) as error:
        raise VhdxProbeError("The attached VHDX disk number was unavailable.") from error
    return VhdxDiskInfo(
        disk_number=number,
        identity=str(record.get("identity") or "Unknown"),
        bus_type=str(record.get("bus_type") or "Unknown"),
        media_type=str(record.get("media_type") or "Unknown"),
        can_pool=_optional_bool(record.get("can_pool")),
        operational_status=str(record.get("operational_status") or "Unknown"),
        health_status=str(record.get("health_status") or "Unknown"),
        volume_root=str(record["volume_root"]) if record.get("volume_root") else None,
    )


def _clean_process_output(stdout: bytes, stderr: bytes) -> str:
    parts = []
    for raw in (stdout, stderr):
        text = _decode_process_output(raw)
        if text:
            parts.append(_clean_clixml(text))
    return "\n".join(item for item in parts if item).strip()


def _decode_process_output(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:32]:
        return raw.decode("utf-16", errors="replace").lstrip("\ufeff").strip()
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()


def _clean_clixml(text: str) -> str:
    marker = "#< CLIXML"
    if marker not in text:
        return text
    xml_text = text.split(marker, 1)[1].strip()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "PowerShell returned encoded diagnostic output."
    messages: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "S" and element.text:
            decoded = re.sub(
                r"_x([0-9A-Fa-f]{4})_",
                lambda match: chr(int(match.group(1), 16)),
                html.unescape(element.text),
            ).strip()
            if decoded and decoded not in messages:
                messages.append(decoded)
    return "\n".join(messages) or "PowerShell returned encoded diagnostic output."


def _requires_elevation(value: object) -> bool:
    if getattr(value, "winerror", None) == 740:
        return True
    text = str(value).casefold()
    return any(
        marker in text
        for marker in (
            "winerror 740",
            "requires elevation",
            "requested operation requires elevation",
        )
    )


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    return None


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _diskpart_quote(value: str) -> str:
    if any(character in value for character in ('"', "\r", "\n")):
        raise VhdxProbeError("The temporary VHDX path contains unsupported characters.")
    return f'"{value}"'
