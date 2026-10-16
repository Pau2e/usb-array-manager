"""Single-member host preparation. Production array execution is absent.

Inventory and identity evidence come from the existing CIM detector/slot store.
The only storage mutation is Format-Volume on a freshly verified volume object.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.services.host_health import (
    DIRTY_FORMAT_REVIEW,
    dirty_format_exception,
    health_review_key,
    physical_health_allows,
    healthy_volume,
)
from usb_array_manager.services.physical_identity import (
    PROVEN,
    compare_physical,
    reconcile_physical,
)
from usb_array_manager.services.production_planning import (
    VOLUME_GUID,
    validation_fingerprint,
)
from usb_array_manager.services.slot_store import (
    SlotStore,
    _identity_from_device,
    _normalize,
)
from usb_array_manager.services.vhdx_probe import windows_elevation_status
from usb_array_manager.services.windows_cim import (
    _POWERSHELL_SCRIPT,
    detect_usb_storage_devices,
)


class PreparationError(RuntimeError):
    pass


STRONG_FIELDS = ("storage_unique_id", "container_id", "usb_device_id", "pnp_device_id")
IN_PROGRESS = {"FORMATTING", "REDISCOVERING", "REVALIDATING"}
_PROCESS_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def physical_matches(expected: StorageDevice, current: StorageDevice) -> bool:
    """Pairwise physical evidence only; callers still enforce global uniqueness."""
    return compare_physical(expected, current).proven


def host_key(device: StorageDevice) -> dict:
    """JSON-stable, letter-independent evidence for persistent preparation state."""
    return {
        "physical": _identity_from_device(device),
        "serial": _normalize(device.serial_number),
        "capacity": device.capacity_bytes,
        "host": json.loads(json.dumps(validation_fingerprint(device))),
    }


def protected_paths(store_path: Path) -> tuple[str, ...]:
    return tuple(
        str(Path(value).resolve())
        for value in (
            __file__,
            sys.executable,
            Path.cwd(),
            store_path.parent,
            tempfile.gettempdir(),
            os.environ.get("SystemRoot", r"C:\Windows"),
        )
    )


def check_target(
    slot: int,
    reference: StorageDevice,
    devices: list[StorageDevice],
    *,
    filesystems: tuple[str, ...] = ("FAT32",),
    protected: tuple[str, ...] = (),
) -> StorageDevice:
    if slot not in range(1, 5) or reference.slot != slot:
        raise PreparationError("An assigned logical slot is required.")
    proof = reconcile_physical(slot, reference, devices)
    if proof.result != PROVEN:
        raise PreparationError(f"{proof.result}: {proof.reason}")
    original_target = proof.target
    target = replace(original_target, slot=slot)
    identity = _identity_from_device(target)
    for item in devices:
        if item is original_target or not item.is_connected:
            continue
        other = _identity_from_device(item)
        if item.disk_number == target.disk_number or any(
            identity[field] and identity[field] == other[field]
            for field in STRONG_FIELDS
        ):
            raise PreparationError(
                "IDENTITY AMBIGUOUS: duplicate physical identity or disk mapping."
            )
    if (target.bus_type or "").upper() != "USB" or target.is_removable is not True:
        raise PreparationError("Only proven removable USB members may be prepared.")
    if target.is_boot_disk is not False or target.is_system_disk is not False:
        raise PreparationError(
            "System/boot disk or unknown disk protection status: BLOCKED."
        )
    if target.is_read_only is not False or target.is_offline is not False:
        raise PreparationError("Read-only/offline status must be proven safe.")
    if not physical_health_allows(target):
        raise PreparationError("Physical member health is unhealthy or unknown.")
    if (
        target.disk_number is None
        or not target.device_path
        or target.partition_count != 1
    ):
        raise PreparationError(
            "Exactly one existing partition on a known physical disk is required."
        )
    if len(target.host_volumes) != 1 or len(target.drive_letters) != 1:
        raise PreparationError(
            "Exactly one uniquely associated mounted host volume is required."
        )
    volume = target.host_volumes[0]
    if not VOLUME_GUID.fullmatch(volume.identity or ""):
        raise PreparationError("Authoritative host Volume GUID is missing or invalid.")
    if volume.is_system is not False or volume.is_boot is not False:
        raise PreparationError(
            "System/boot volume or unknown volume protection status: BLOCKED."
        )
    if not healthy_volume(target) and not (
        filesystems == ("FAT32",) and dirty_format_exception(target)
    ):
        raise PreparationError(
            f"Host volume health is {volume.health_status or 'UNKNOWN'}; healthy host required. Preparation BLOCKED."
        )
    if (volume.drive_letter or "").casefold() != target.drive_letters[0].casefold():
        raise PreparationError("Physical-to-volume association is inconsistent.")
    filesystem = (volume.filesystem or "").upper()
    if filesystem not in filesystems or {
        value.upper() for value in target.filesystem_types
    } != {filesystem}:
        raise PreparationError(
            "Unexpected or inconsistent host filesystem. Only FAT32 is migratable in v0.8.0."
        )
    if (
        not volume.capacity_bytes
        or not target.capacity_bytes
        or not (
            target.capacity_bytes * 9 // 10
            <= volume.capacity_bytes
            <= target.capacity_bytes
        )
        or volume.free_bytes is None
        or not 0 <= volume.free_bytes <= volume.capacity_bytes
    ):
        raise PreparationError(
            "Host capacity/free space is unknown or inconsistent with the physical member."
        )
    for item in devices:
        if item is original_target or not item.is_connected:
            continue
        if any(
            (other.identity or "").casefold() == volume.identity.casefold()
            for other in item.host_volumes
        ):
            raise PreparationError("IDENTITY AMBIGUOUS: duplicate host Volume GUID.")
    # Early UI gate; the Windows adapter also resolves physical backing disks for
    # these paths, including folder mount points, immediately before dispatch.
    if any(
        Path(value).drive.casefold() == target.drive_letters[0].casefold()
        for value in protected
    ):
        raise PreparationError(
            "The member contains the application, Windows, or preparation state."
        )
    return target


@dataclass(frozen=True)
class PreparationConfirmation:
    target: StorageDevice
    backup_acknowledged: bool
    phrase: str
    dirty_filesystem_acknowledged: bool = False

    def check(self) -> None:
        if (
            self.backup_acknowledged is not True
            or self.phrase != f"FORMAT SLOT {self.target.slot}"
        ):
            raise PreparationError(
                "Per-member backup acknowledgement and exact typed confirmation are required."
            )
        if (
            dirty_format_exception(self.target)
            and self.dirty_filesystem_acknowledged is not True
        ):
            raise PreparationError(
                "Separate dirty-filesystem acknowledgement is required: filesystem may be inconsistent, data damaged/unrecoverable, and formatting destroys it."
            )
        if (
            not physical_health_allows(self.target)
            or len(self.target.host_volumes) != 1
            or (
                not healthy_volume(self.target)
                and not dirty_format_exception(self.target)
            )
        ):
            raise PreparationError(
                "Health is unproven; destructive confirmation is blocked."
            )


class PreparationStore:
    """Atomic per-member stage records. In-flight stages never recover as success."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {
                "schema_version": 1,
                "generation": 0,
                "members": {},
                "validation": None,
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or type(data.get("generation")) is not int
                or data["generation"] < 0
                or not isinstance(data.get("members"), dict)
            ):
                raise ValueError("Unsupported preparation state")
            for slot, record in data["members"].items():
                if slot not in {"1", "2", "3", "4"} or not isinstance(record, dict):
                    raise ValueError("Invalid member record")
                if record.get("status") not in {
                    "READY FOR PREPARATION",
                    DIRTY_FORMAT_REVIEW,
                    "FORMATTING",
                    "REDISCOVERING",
                    "REVALIDATING",
                    "PREPARED",
                    "FAILED",
                    "NEEDS REVIEW",
                } or not isinstance(record.get("events"), list):
                    raise ValueError("Invalid stage record")
                if record["status"] == "PREPARED" and not isinstance(
                    record.get("key"), dict
                ):
                    raise ValueError("Missing prepared identity")
            return data
        except (OSError, ValueError, TypeError) as error:
            raise PreparationError(
                f"Preparation state cannot be trusted: {self.path}: {error}"
            ) from error

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(data, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise PreparationError(
                f"Could not persist preparation stage: {error}"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def exclusive(self):
        if not _PROCESS_LOCK.acquire(blocking=False):
            raise PreparationError("Another host preparation is active.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Windows releases the byte lock on process exit, unlike a sentinel.
            import msvcrt

            with self.path.with_suffix(".lock").open("a+b") as lock:
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise PreparationError(
                        "Another instance is preparing a host."
                    ) from error
                try:
                    try:
                        # A PowerShell child can outlive a forcibly closed GUI.
                        # Its exclusive sharing lock must clear before review or
                        # another operation can proceed in a new app instance.
                        with self.path.with_suffix(".formatter.lock").open("a+b"):
                            pass
                    except OSError as error:
                        raise PreparationError(
                            "A Windows preparation process is still active; wait before review."
                        ) from error
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            _PROCESS_LOCK.release()


def format_script(
    target: StorageDevice,
    protected: tuple[str, ...],
    operation_lock: Path | None = None,
    *,
    dirty_filesystem_acknowledged: bool = False,
) -> str:
    """Build a guarded one-volume operation; caller must validate confirmation."""
    payload = base64.b64encode(
        json.dumps(
            {
                "target": asdict(target),
                "protected": protected,
                "operation_lock": str(operation_lock) if operation_lock else None,
                "parent_pid": os.getpid(),
                "dirty_filesystem_acknowledged": dirty_filesystem_acknowledged is True
                and dirty_format_exception(target),
            }
        ).encode()
    ).decode()
    # Reuse the actual detector, rather than a second USB identification algorithm.
    return r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$operationLock = $null
if ($request.operation_lock) {
    $operationLock = [IO.File]::Open($request.operation_lock, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}
try {
if (-not (Get-Process -Id $request.parent_pid -ErrorAction SilentlyContinue)) { throw 'Confirming application has exited. Review again.' }
$expected = $request.target
function Read-ExistingInventory {
__INVENTORY__
}
function Normalize([object]$value) { ([string]$value).Trim().ToUpperInvariant() }
function Assert-ObservedHealth($current) {
    $hostVolume = @($current.host_volumes)[0]
    if ((Normalize $hostVolume.health_status) -ne (Normalize $expected.host_volumes[0].health_status)) { throw 'Health changed since confirmation. Review again.' }
    $d = $current.health_diagnostics
    $v = $hostVolume.health_diagnostics
    $s = $v.storage
    if ($d -and ($d.get_disk.health_value -ne 'Healthy' -or $null -eq $d.get_disk.health_cim_raw -or $d.get_disk.health_cim_raw -ne 0 -or
        (@($d.get_disk.operational_value) -join ',') -ne 'Online' -or (@($d.get_disk.operational_cim_raw) -join ',') -ne '53264' -or $d.get_disk_error)) { throw 'Observed physical health unproven.' }
    if ($hostVolume.health_status -in @(0,'Healthy','OK')) {
        if ($v -and ($v.source -ne 'Get-Volume.HealthStatus' -or $v.storage_match_count -ne 1 -or $v.storage_query_error -or
            $v.selected_raw -ne 'Healthy' -or $s.health_value -ne 'Healthy' -or $null -eq $s.health_cim_raw -or $s.health_cim_raw -ne 0 -or
            (@($s.operational_value) -join ',') -ne 'OK' -or (@($s.operational_cim_raw) -join ',') -ne '2' -or
            $null -eq $v.dirty_bit_set -or $v.dirty_bit_set -ne $false -or $null -eq $v.logical_disk_dirty -or $v.logical_disk_dirty -ne $false)) { throw 'Observed volume health inconsistent or unknown.' }
        return
    }
    if ($request.dirty_filesystem_acknowledged -ne $true -or
        $hostVolume.health_status -ne 'Warning' -or $hostVolume.filesystem -ne 'FAT32' -or
        $d.get_disk.health_value -ne 'Healthy' -or $null -eq $d.get_disk.health_cim_raw -or $d.get_disk.health_cim_raw -ne 0 -or
        (@($d.get_disk.operational_value) -join ',') -ne 'Online' -or
        (@($d.get_disk.operational_cim_raw) -join ',') -ne '53264' -or $d.get_disk_error -or
        $v.source -ne 'Get-Volume.HealthStatus' -or $v.selected_raw -ne 'Warning' -or
        $v.storage_match_count -ne 1 -or $v.storage_query_error -or $v.file_system -ne 'FAT32' -or
        $v.automount -ne $true -or $v.dirty_bit_set -ne $true -or $v.logical_disk_dirty -ne $true -or
        ($null -ne $v.win32_status -and $v.win32_status -ne 'OK') -or
        $s.health_value -ne 'Warning' -or $s.health_cim_raw -ne 1 -or
        (@($s.operational_value) -join ',') -ne 'Full Repair Needed' -or
        (@($s.operational_cim_raw) -join ',') -ne '53263') { throw 'Dirty-filesystem exception unproven or not acknowledged.' }
}
function Assert-VolumeHealth($volume) {
    if ($request.dirty_filesystem_acknowledged -eq $true) {
        if ($volume.HealthStatus -ne 'Warning' -or
            (@($volume.OperationalStatus) -join ',') -ne 'Full Repair Needed') { throw 'Dirty volume health changed. Review again.' }
    } elseif ($volume.HealthStatus -ne 'Healthy' -or (@($volume.OperationalStatus) -join ',') -ne 'OK') { throw 'Volume health unproven.' }
}
function Assert-Target {
    $all = @(Read-ExistingInventory | ConvertFrom-Json | ForEach-Object { $_ })
    $matches = @($all | Where-Object { (Normalize $_.pnp_device_id) -eq (Normalize $expected.pnp_device_id) })
    if ($matches.Count -ne 1) { throw 'Physical identity is no longer unique. Review again.' }
    $current = $matches[0]
    foreach ($field in @('pnp_device_id','storage_unique_id','storage_unique_id_format','serial_number',
                         'container_id','usb_device_id','model','capacity_bytes','disk_number','device_path')) {
        if ((Normalize $current.$field) -ne (Normalize $expected.$field)) { throw "Target changed: $field. Review again." }
    }
    foreach ($field in @('pnp_device_id','storage_unique_id','container_id','usb_device_id')) {
        if ((Normalize $current.$field) -and @($all | Where-Object { (Normalize $_.$field) -eq (Normalize $current.$field) }).Count -ne 1) {
            throw "Duplicate physical identity: $field"
        }
    }
    if ($current.bus_type -notin @(7, 'USB') -or $current.is_removable -ne $true -or
        $null -eq $current.is_boot_disk -or $current.is_boot_disk -ne $false -or
        $null -eq $current.is_system_disk -or $current.is_system_disk -ne $false -or
        $null -eq $current.is_read_only -or $current.is_read_only -ne $false -or
        $null -eq $current.is_offline -or $current.is_offline -ne $false -or
        $current.partition_count -ne 1 -or $current.health_status -notin @(0,'Healthy','OK')) { throw 'Physical safety gate failed.' }
    $hosts = @($current.host_volumes)
    if ($hosts.Count -ne 1) { throw 'Host volume association is ambiguous.' }
    $hostVolume = $hosts[0]
    if ($hostVolume.identity -ine $expected.host_volumes[0].identity -or
        $hostVolume.filesystem -ine 'FAT32' -or
        $hostVolume.capacity_bytes -ne $expected.host_volumes[0].capacity_bytes -or
        $null -eq $hostVolume.is_system -or $hostVolume.is_system -ne $false -or
        $null -eq $hostVolume.is_boot -or $hostVolume.is_boot -ne $false -or
        (@($current.drive_letters) -join ',') -ine (@($expected.drive_letters) -join ',')) { throw 'Host mapping changed. Review again.' }
    $duplicates = @($all | ForEach-Object { $_.host_volumes } | Where-Object { $_.identity -ieq $hostVolume.identity })
    if ($duplicates.Count -ne 1) { throw 'Duplicate host volume.' }
    Assert-ObservedHealth $current
    return $current
}
$current = Assert-Target
$disk = @(Get-Disk -Number $current.disk_number -ErrorAction Stop)
if ($disk.Count -ne 1 -or $disk[0].IsSystem -ne $false -or $disk[0].IsBoot -ne $false -or
    $disk[0].HealthStatus -ne 'Healthy' -or (@($disk[0].OperationalStatus) -join ',') -ne 'Online' -or
    $disk[0].BusType -ne 'USB' -or $disk[0].IsReadOnly -ne $false -or $disk[0].IsOffline -ne $false) { throw 'Storage disk safety gate failed.' }
$parts = @($disk[0] | Get-Partition -ErrorAction Stop)
if ($parts.Count -ne 1 -or $parts[0].IsSystem -ne $false -or $parts[0].IsBoot -ne $false) { throw 'Expected one non-system partition.' }
$volumes = @($parts[0] | Get-Volume -ErrorAction Stop)
if ($volumes.Count -ne 1) { throw 'Expected one storage volume object.' }
$volume = $volumes[0]
if ($volume.Path -ine $expected.host_volumes[0].identity -or $volume.FileSystem -ine 'FAT32' -or
    $volume.Size -ne $expected.host_volumes[0].capacity_bytes -or
    -not $volume.ObjectId) { throw 'Verified volume object does not match confirmation.' }
Assert-VolumeHealth $volume
foreach ($path in $request.protected) {
    $protectedVolumes = @(Get-Volume -FilePath $path -ErrorAction Stop)
    if ($protectedVolumes.Count -ne 1) { throw 'Cannot resolve a protected application/state path.' }
    $protectedDisks = @($protectedVolumes[0] | Get-Partition -ErrorAction Stop | Get-Disk -ErrorAction Stop)
    if ($protectedDisks.Count -ne 1 -or $protectedDisks[0].Number -eq $disk[0].Number) { throw 'Target contains protected application/Windows/state data.' }
}
# Final relationship and hardware check; the mutation receives this exact CIM
# volume object, never a drive-letter or disk-number format argument.
$null = Assert-Target
if (-not (Get-Process -Id $request.parent_pid -ErrorAction SilentlyContinue)) { throw 'Confirming application exited before dispatch.' }
$last = @($parts[0] | Get-Volume -ErrorAction Stop)
if ($last.Count -ne 1 -or $last[0].ObjectId -ne $volume.ObjectId -or $last[0].Path -ine $volume.Path -or
    $last[0].FileSystem -ine 'FAT32') { throw 'Volume object changed at dispatch.' }
Assert-VolumeHealth $last[0]
$finalDisk = @(Get-Disk -Number $current.disk_number -ErrorAction Stop)
if ($finalDisk.Count -ne 1 -or $finalDisk[0].HealthStatus -ne 'Healthy' -or
    (@($finalDisk[0].OperationalStatus) -join ',') -ne 'Online' -or
    $finalDisk[0].IsSystem -ne $false -or $finalDisk[0].IsBoot -ne $false -or
    $finalDisk[0].IsOffline -ne $false -or $finalDisk[0].IsReadOnly -ne $false -or
    $finalDisk[0].BusType -ne 'USB') { throw 'Final physical disk safety gate failed.' }
$formatted = Format-Volume -InputObject $last[0] -FileSystem NTFS -NewFileSystemLabel ('USBArraySlot' + $expected.slot) -Confirm:$false -ErrorAction Stop
if (@($formatted).Count -ne 1 -or $formatted.FileSystem -ine 'NTFS' -or $formatted.HealthStatus -ne 'Healthy') {
    throw 'Format result unverified. Do not retry formatting automatically.'
}
[PSCustomObject]@{ formatted = $true; healthy = $true } | ConvertTo-Json -Compress
} finally {
    if ($operationLock) { $operationLock.Dispose() }
}
""".replace("__PAYLOAD__", payload).replace("__INVENTORY__", _POWERSHELL_SCRIPT)


class WindowsHostFormatter:
    def format(
        self,
        target: StorageDevice,
        protected: tuple[str, ...],
        *,
        confirmation: PreparationConfirmation,
        operation_lock: Path,
    ) -> None:
        confirmation.check()
        if not compare_physical(target, target).proven:
            raise PreparationError(
                "Physical identity is unproven; format adapter blocked."
            )
        if (
            host_key(confirmation.target) != host_key(target)
            or confirmation.target.disk_number != target.disk_number
            or confirmation.target.drive_letters != target.drive_letters
        ):
            raise PreparationError(
                "Adapter target does not match the confirmed member."
            )
        if windows_elevation_status() is not True:
            raise PreparationError(
                "Run USB Array Manager as administrator to prepare a host."
            )
        if health_review_key(confirmation.target) != health_review_key(target):
            raise PreparationError(
                "Health evidence changed since confirmation. Review again."
            )
        script = format_script(
            target,
            protected,
            operation_lock,
            dirty_filesystem_acknowledged=confirmation.dirty_filesystem_acknowledged,
        )
        # No timeout/kill: retain the worker and operation lock until Windows
        # finishes. Process loss is persisted as NEEDS REVIEW on the next launch.
        with tempfile.TemporaryDirectory(prefix="usb-host-preparation-") as folder:
            script_path = Path(folder) / "prepare.ps1"
            script_path.write_text(script, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                ],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        if result.returncode:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
            raise PreparationError(
                "Windows format failed or is unverified; manual review required: "
                + detail
            )
        try:
            receipt = json.loads(result.stdout.decode("utf-8-sig"))
            if receipt != {"formatted": True, "healthy": True}:
                raise ValueError("Unexpected receipt")
        except (ValueError, UnicodeError) as error:
            raise PreparationError(
                "Format result could not be verified. Do not format again automatically."
            ) from error


class HostPreparationService:
    def __init__(
        self,
        slots: SlotStore,
        store: PreparationStore,
        *,
        detector=None,
        formatter=None,
        elevated=None,
        protected=None,
        sleep=time.sleep,
    ):
        self.slots, self.store = slots, store
        self.detector = detector or detect_usb_storage_devices
        self.formatter = formatter or WindowsHostFormatter()
        self.elevated = elevated or windows_elevation_status
        self.protected = (
            protected if protected is not None else protected_paths(store.path)
        )
        self.sleep = sleep

    def inspect(
        self, slot: int, devices: list[StorageDevice], *, ntfs=False
    ) -> StorageDevice:
        proof = self.identity_reconciliation(slot, devices)
        if proof.result != PROVEN:
            raise PreparationError(f"{proof.result}: {proof.reason}")
        reference = self.slots.preparation_reference(slot)
        return check_target(
            slot,
            reference,
            devices,
            filesystems=("NTFS",) if ntfs else ("FAT32",),
            protected=self.protected,
        )

    def identity_reconciliation(self, slot: int, devices: list[StorageDevice]):
        record = self.slots.preparation_record(slot)
        references = tuple(
            self.slots.preparation_reference(other)
            for other in self.slots.reserved_slots() - {slot}
        )
        saved = {
            "identity": record.get("identity"),
            "reference_source": "explicit recorded proof"
            if record.get("preparation_identity")
            else "preserved snapshot / legacy",
            "reference": {
                field: getattr(self.slots.preparation_reference(slot), field)
                for field in (
                    *STRONG_FIELDS,
                    "serial_number",
                    "model",
                    "capacity_bytes",
                )
            },
        }
        return reconcile_physical(
            slot,
            self.slots.preparation_reference(slot),
            devices,
            other_references=references,
            legacy=not bool(record.get("preparation_identity")),
            saved_record=saved,
        )

    def exclusion_diagnostic(self, proof) -> str:
        if proof.result != PROVEN:
            return "Application/Windows/state exclusion: NOT EVALUATED — physical identity is not proven."
        letters = {value.casefold() for value in proof.target.drive_letters}
        drives = [Path(value).drive.casefold() for value in self.protected]
        early = (
            "BLOCKED — target hosts a protected path"
            if letters.intersection(drives)
            else "UNPROVEN — a protected path has no local drive mapping"
            if any(not value for value in drives)
            else "INITIAL PATH CHECK PASS"
        )
        return (
            f"Application/Windows/state exclusion: {early}.\n"
            "Protected paths: " + json.dumps(self.protected) + "\n"
            "Final Windows backing-volume/disk exclusion: NOT RUN by diagnostics; required immediately before formatting. "
            "System/boot=False alone cannot establish this exclusion."
        )

    def record_identity(self, slot: int, log=lambda message: None) -> StorageDevice:
        """User-requested, non-destructive metadata upgrade; never implicit in scans."""
        with self.store.exclusive():
            devices = self.detector()
            result = self.identity_reconciliation(slot, devices)
            log(result.describe())
            if result.result != PROVEN:
                raise PreparationError(
                    f"Legacy identity upgrade refused: {result.result}"
                )
            if not result.legacy:
                log(
                    f"Slot {slot}: current physical proof already recorded; no identity upgrade attempted."
                )
                return replace(result.target, slot=slot)
            log(
                f"Slot {slot}: explicit legacy metadata upgrade attempted after unique PROVEN MATCH; no USB mutation."
            )
            try:
                proof = self.slots.record_preparation_identity(slot, devices)
            except Exception as error:
                log(
                    f"Slot {slot}: legacy identity upgrade FAILED: {error}; no USB mutation."
                )
                raise
            log(
                f"Slot {slot} legacy identity reconciliation: PROVEN MATCH; explicit metadata upgrade succeeded. "
                f"Evidence: {', '.join(proof['evidence_fields'])}; "
                + (
                    "New persistent-storage identity recorded."
                    if result.target.storage_unique_id
                    else "Persistent-storage identity remains unavailable; no identifier fabricated. Original reference preserved."
                )
            )
            return replace(result.target, slot=slot)

    def status(self, slot: int, devices: list[StorageDevice]) -> tuple[str, str]:
        try:
            data = self.store.load()
            record = data["members"].get(str(slot), {})
            if record.get("status") in IN_PROGRESS | {"FAILED", "NEEDS REVIEW"}:
                return (
                    "NEEDS REVIEW",
                    "Previous operation unfinished/failed. Use read-only Recheck; never auto-format.",
                )
            try:
                target = self.inspect(slot, devices, ntfs=True)
            except PreparationError:
                target = self.inspect(slot, devices)
                if dirty_format_exception(target):
                    return (
                        DIRTY_FORMAT_REVIEW,
                        "FAT32 dirty/Full Repair Needed only; separate filesystem-risk acknowledgement, backup acknowledgement and typed confirmation required.",
                    )
                return (
                    "READY FOR PREPARATION",
                    "FAT32; backup acknowledgement and typed confirmation required.",
                )
            if record.get("status") == "PREPARED" and record.get("key") == host_key(
                target
            ):
                return (
                    "PREPARED",
                    "NTFS — physical and host identity revalidated against current inventory.",
                )
            return (
                "REVALIDATION REQUIRED",
                "NTFS host requires a read-only Recheck to record current identity.",
            )
        except Exception as error:
            return "BLOCKED", str(error)

    def _record(
        self, slot, status, detail, target=None, *, invalidate=False, confirmation=None
    ):
        data = self.store.load()
        record = data["members"].setdefault(str(slot), {"events": []})
        if invalidate:
            data["generation"] += 1
            data["validation"] = None
        record.update(status=status, detail=detail, updated_at=now())
        if target is not None:
            record.update(key=host_key(target), observation=asdict(target))
        if confirmation is not None:
            record["confirmation"] = {
                "slot": slot,
                "phrase": confirmation.phrase,
                "backup_acknowledged": True,
                "dirty_filesystem_acknowledged": confirmation.dirty_filesystem_acknowledged
                is True,
                "dirty_filesystem_exception": dirty_format_exception(
                    confirmation.target
                ),
                "accepted_at": now(),
            }
        record["events"].append({"at": now(), "status": status, "detail": detail})
        self.store.save(data)

    def recheck(self, slot: int, log=lambda message: None) -> StorageDevice:
        """Explicit read-only review after failure or discovery of an NTFS host."""
        with self.store.exclusive():
            devices = self.detector()
            try:
                target = self.inspect(slot, devices, ntfs=True)
                status = "PREPARED"
            except PreparationError:
                target = self.inspect(slot, devices)
                status = (
                    DIRTY_FORMAT_REVIEW
                    if dirty_format_exception(target)
                    else "READY FOR PREPARATION"
                )
            self._record(
                slot,
                status,
                "Read-only physical/volume/capacity recheck completed.",
                target,
                invalidate=True,
            )
            log(
                f"Slot {slot}: {status}; volume={target.host_volumes[0].identity}; capacity/free={target.host_volumes[0].capacity_bytes}/{target.host_volumes[0].free_bytes}."
            )
            return target

    def prepare(
        self, confirmation: PreparationConfirmation, log=lambda message: None
    ) -> StorageDevice:
        confirmation.check()
        slot = confirmation.target.slot
        with self.store.exclusive():
            if self.elevated() is not True:
                raise PreparationError(
                    "Administrator rights are required before host preparation can start."
                )
            records = self.store.load()["members"]
            if any(record.get("status") in IN_PROGRESS for record in records.values()):
                raise PreparationError(
                    "An interrupted preparation needs read-only review before any new format."
                )
            if records.get(str(slot), {}).get("status") in {"FAILED", "NEEDS REVIEW"}:
                raise PreparationError(
                    "Recheck this member before reviewing a new confirmation."
                )
            current = self.inspect(slot, self.detector())
            expected = confirmation.target
            if (
                not physical_matches(expected, current)
                or host_key(current) != host_key(expected)
                or current.disk_number != expected.disk_number
                or current.device_path != expected.device_path
                or current.drive_letters != expected.drive_letters
                or health_review_key(current) != health_review_key(expected)
            ):
                raise PreparationError(
                    "Target changed since confirmation. Stop and review again."
                )
            log(
                f"Slot {slot}: PRECHECK passed; physical={_identity_from_device(current)}; old filesystem={current.host_volumes[0].filesystem}; old GUID={current.host_volumes[0].identity}."
            )
            log(
                f"Slot {slot}: confirmation accepted; backup acknowledged (not verified)."
            )
            if dirty_format_exception(current):
                log(
                    f"Slot {slot}: filesystem dirty exception proven; separate acknowledgement accepted: existing filesystem may be inconsistent, data damaged/unrecoverable, format destroys it. No repair or dirty-bit clearing."
                )
            self._record(
                slot,
                "FORMATTING",
                "Confirmed destructive NTFS format started; prior validation invalidated.",
                current,
                invalidate=True,
                confirmation=confirmation,
            )
            log(f"Slot {slot}: FORMATTING; earlier four-member validation invalidated.")
            try:
                self.formatter.format(
                    current,
                    self.protected,
                    confirmation=confirmation,
                    operation_lock=self.store.path.with_suffix(".formatter.lock"),
                )
            except Exception as error:
                self._record(slot, "FAILED", str(error))
                log(
                    f"Slot {slot}: FAILED; {error}; rescan and manual review required. No automatic retry."
                )
                raise
            self._record(
                slot,
                "REDISCOVERING",
                "Windows confirmed healthy NTFS format. Old GUID is stale.",
            )
            log(
                f"Slot {slot}: Windows format returned healthy NTFS; REDISCOVERING from physical identity, old GUID discarded."
            )
            last_error = None
            for attempt in range(5):
                try:
                    prepared = self.inspect(slot, self.detector(), ntfs=True)
                    self._record(
                        slot,
                        "REVALIDATING",
                        "Current physical/NTFS association rediscovered.",
                        prepared,
                    )
                    log(
                        f"Slot {slot}: REVALIDATING; freshly observed GUID={prepared.host_volumes[0].identity}; filesystem={prepared.host_volumes[0].filesystem}."
                    )
                    old = current.host_volumes[0]
                    volume = prepared.host_volumes[0]
                    if abs(volume.capacity_bytes - old.capacity_bytes) > 256 * 1024**2:
                        raise PreparationError(
                            "Post-format capacity changed unexpectedly; manual review required."
                        )
                    self._record(
                        slot,
                        "PREPARED",
                        "NTFS host identity and measured capacity/free space revalidated.",
                        prepared,
                    )
                    log(
                        f"Slot {slot}: PREPARED; slot retained by physical identity; new authoritative GUID={volume.identity}; filesystem={volume.filesystem}; capacity/free={volume.capacity_bytes}/{volume.free_bytes}; production sizing must be recalculated."
                    )
                    return prepared
                except Exception as error:
                    last_error = error
                    if attempt < 4:
                        self.sleep(1)
            self._record(
                slot,
                "NEEDS REVIEW",
                f"Format completed but rediscovery/revalidation failed: {last_error}",
            )
            log(
                f"Slot {slot}: NEEDS REVIEW; {last_error}; no guessed GUID and no reformat."
            )
            raise PreparationError(
                f"Format completed, but rediscovery requires manual review: {last_error}"
            )

    def record_validation(self, devices: list[StorageDevice], result) -> None:
        with self.store.exclusive():
            data = self.store.load()
            ready = all(
                self.status(slot, devices)[0] == "PREPARED" for slot in range(1, 5)
            )
            clean = (
                result.status == "READY"
                and result.simultaneous_verified
                and len(result.members) == 4
                and {member.slot for member in result.members} == {1, 2, 3, 4}
                and all(
                    member.identity_verified
                    and member.status == "READY"
                    and member.disk
                    and member.disk.bus_type.casefold()
                    .replace(" ", "")
                    .replace("-", "")
                    == "filebackedvirtual"
                    and member.disk.can_pool is True
                    and member.cleanup_status == "Detached and temporary file deleted"
                    and not member.leftover_path
                    and not member.may_remain_attached
                    for member in result.members
                )
            )
            data["validation"] = (
                {
                    "at": now(),
                    "generation": data["generation"],
                    "hosts": [
                        host_key(self.inspect(slot, devices, ntfs=True))
                        for slot in range(1, 5)
                    ],
                }
                if ready and clean
                else None
            )
            self.store.save(data)
