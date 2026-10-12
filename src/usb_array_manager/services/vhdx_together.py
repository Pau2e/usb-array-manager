from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import (
    VhdxDiskInfo, VhdxTogetherMember, VhdxTogetherResult,
)
from usb_array_manager.services.vhdx_probe import (
    ADMINISTRATOR_MESSAGE, ELIGIBILITY_PROBE_BYTES, TEST_FOLDER_NAME,
    DiskPartVhdxRunner, VhdxProbeError, VhdxProbeSettings,
    _device_blocker, _device_identity, _identity_token, _is_file_backed_virtual,
    _matches_verified_virtual_disk, _physical_disk_query_script, _ps_quote,
    _resolve_storage_match, _storage_physical_disks, windows_elevation_status,
)


def _image_query_script(paths: Sequence[Path]) -> str:
    values = ", ".join(_ps_quote(str(path)) for path in paths)
    return f"""
$images = @(foreach ($path in @({values})) {{
    try {{
        $image = Get-DiskImage -ImagePath $path -ErrorAction Stop
        [PSCustomObject]@{{
            requested_path = $path; image_path = [string]$image.ImagePath
            device_path = [string]$image.DevicePath; attached = $image.Attached
            storage_type = [string]$image.StorageType; error = ''
            size = $image.Size; number = $image.Number
        }}
    }} catch {{
        [PSCustomObject]@{{requested_path = $path; error = [string]$_}}
    }}
}})
"""


def _provider_query_script(provider: str, expression: str) -> str:
    query = _physical_disk_query_script(expression)
    return f"""
try {{
    $decoded = & {{ {query} }} | ConvertFrom-Json -ErrorAction Stop
    # PowerShell 5.1 emits a JSON array as one pipeline object. Enumerate its
    # members explicitly before nesting them in the shared snapshot object.
    $rows = @(foreach ($row in $decoded) {{ $row }})
    $queries += [PSCustomObject]@{{ provider = '{provider}'; rows = $rows; error = '' }}
}} catch {{
    $queries += [PSCustomObject]@{{ provider = '{provider}'; rows = @(); error = [string]$_ }}
}}
"""


def _snapshot_rows(query: Mapping) -> list[Mapping]:
    rows = query.get("rows")
    if not isinstance(rows, list) or any(
        not isinstance(row, Mapping) or "DeviceId" not in row or "BusType" not in row
        for row in rows
    ):
        raise VhdxProbeError(
            f"{query.get('provider', 'Storage provider')} returned malformed shared disk records.",
            details=json.dumps(rows),
        )
    return rows


class TogetherDiskPartRunner(DiskPartVhdxRunner):
    """Existing lifecycle plus a shared, read-only four-disk capability snapshot."""

    def inspect_together(self, expected: Mapping[Path, VhdxDiskInfo]) -> dict[Path, VhdxDiskInfo]:
        inventory = self._enumerate_disks()
        expressions = (
            ("Get-PhysicalDisk", "@(Get-PhysicalDisk -ErrorAction Stop)"),
            ("Primordial Storage Pool", "@(Get-StoragePool -IsPrimordial $true -ErrorAction Stop | Get-PhysicalDisk -ErrorAction Stop)"),
            ("MSFT_PhysicalDisk CIM", "@(Get-CimInstance -Namespace 'root/Microsoft/Windows/Storage' -ClassName MSFT_PhysicalDisk -ErrorAction Stop)"),
        )
        script = _image_query_script(tuple(expected)) + "$queries = @()\n"
        for provider, expression in expressions:
            script += _provider_query_script(provider, expression)
        script += "[PSCustomObject]@{ images = $images; queries = $queries } | ConvertTo-Json -Depth 8 -Compress"
        payload = json.loads(self._run_powershell(script))
        self._verify_image_links(expected, inventory, payload["images"])
        candidates = []
        for query in payload["queries"]:
            provider = query["provider"]
            if query.get("error"):
                self._record_diagnostic(f"Simultaneous {provider}: FAILED — {query['error']}")
                continue
            try:
                rows = _storage_physical_disks(json.dumps(_snapshot_rows(query)), provider)
            except VhdxProbeError as error:
                self._record_diagnostic(
                    f"Simultaneous {provider}: FAILED — {error}\n{error.details}"
                )
                continue
            self._record_diagnostic(f"Simultaneous {provider}: READY — {len(rows)} object(s)")
            candidates.extend(rows)
            for candidate in rows:
                # Include only this validation's virtual-disk candidates.
                if any(candidate.device_id.strip() == str(disk.disk_number) or
                       (disk.storage_unique_id and _identity_token(candidate.unique_id) == _identity_token(disk.storage_unique_id))
                       for disk in expected.values()):
                    self._record_diagnostic(
                        f"Simultaneous {provider} candidate: DeviceId={candidate.device_id!r}; "
                        f"UniqueId={candidate.unique_id!r}; BusType={candidate.bus_type!r}; "
                        f"Size={candidate.size_bytes}; CanPool={candidate.can_pool}; "
                        f"CannotPoolReason={candidate.cannot_pool_reasons!r}"
                    )

        resolved: dict[Path, VhdxDiskInfo] = {}
        for path, disk in expected.items():
            matches = [item for item in candidates if _matches_verified_virtual_disk(disk, item)]
            result, reason = _resolve_storage_match(replace(disk, can_pool=None), matches)
            if "ambiguous" in reason or "conflicting" in reason:
                raise VhdxProbeError(f"{path.name}: Storage Spaces identity/capability mapping is ambiguous: {reason}")
            for candidate in matches:
                if candidate.device_id and candidate.device_id.strip() != str(disk.disk_number):
                    raise VhdxProbeError(f"{path.name}: Storage DeviceId conflicts with the verified path/disk mapping.")
                if candidate.friendly_name and "virtual" not in candidate.friendly_name.casefold():
                    raise VhdxProbeError(f"{path.name}: unexpected virtual-disk model {candidate.friendly_name!r}.")
                if disk.storage_object_id and candidate.object_id and _identity_token(disk.storage_object_id) != _identity_token(candidate.object_id):
                    raise VhdxProbeError(f"{path.name}: Storage ObjectId changed.")
            self._record_diagnostic(
                f"Simultaneous path={path}; Disk={result.disk_number}; identity={result.identity}; "
                f"UniqueId={result.storage_unique_id}; DeviceId={result.storage_device_id}; "
                f"ObjectId={result.storage_object_id}; model={result.friendly_name}; "
                f"size={result.size_bytes}; CanPool={result.can_pool}; "
                f"CannotPoolReason={result.pool_rejection_details}; resolution={reason}"
            )
            resolved[path] = result
        # The complete attachment set must survive the shared capability-query interval.
        script = _image_query_script(tuple(expected)) + "$images | ConvertTo-Json -Depth 4 -Compress"
        images = json.loads(self._run_powershell(script))
        self._verify_image_links(expected, self._enumerate_disks(), images)
        _reject_duplicate_disks(tuple(resolved.values()))
        return resolved

    def _verify_image_links(self, expected, inventory, images) -> None:
        if not isinstance(images, list) or len(images) != len(expected):
            raise VhdxProbeError("The shared snapshot did not expose exactly four image-path mappings.")
        for path, disk in expected.items():
            matches = [row for row in images if _path_token(row.get("requested_path", "")) == _path_token(str(path))]
            if len(matches) != 1:
                raise VhdxProbeError(f"{path.name}: image-path mapping is ambiguous.")
            image = matches[0]
            current = inventory.get(disk.disk_number)
            if image.get("error"):
                raise VhdxProbeError(f"{path.name}: read-only image inspection failed: {image['error']}")
            number = re.fullmatch(r"\\\\\.\\PhysicalDrive(\d+)", str(image.get("device_path", "")), re.IGNORECASE)
            if (
                image.get("attached") is not True
                or _path_token(image.get("image_path", "")) != _path_token(str(path))
                or str(image.get("storage_type", "")).casefold() not in {"vhdx", "3"}
                or number is None or int(number.group(1)) != disk.disk_number
                or current is None or not _is_file_backed_virtual(current.bus_type)
            ):
                raise VhdxProbeError(f"{path.name}: the VHDX path, attached state, type or disk inventory did not agree.")
            if image.get("size") is not None and image["size"] != ELIGIBILITY_PROBE_BYTES:
                raise VhdxProbeError(f"{path.name}: image virtual size is not 8 GiB.")
            if image.get("number") is not None and image["number"] != disk.disk_number:
                raise VhdxProbeError(f"{path.name}: image disk number conflicts with DevicePath.")
            if current.size_is_exact and current.size_bytes != disk.size_bytes:
                raise VhdxProbeError(f"{path.name}: virtual size changed during validation.")
            if current.storage_unique_id and disk.storage_unique_id and _identity_token(current.storage_unique_id) != _identity_token(disk.storage_unique_id):
                raise VhdxProbeError(f"{path.name}: virtual UniqueId changed during validation.")
            self._record_diagnostic(f"Image path verified: {path} -> Disk {disk.disk_number}; File Backed Virtual; VHDX.")


def _path_token(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)).casefold()


def _reject_duplicate_disks(disks: Sequence[VhdxDiskInfo]) -> None:
    for attribute in ("disk_number", "identity", "storage_unique_id", "storage_object_id"):
        values = [str(getattr(disk, attribute)).strip().casefold() for disk in disks if getattr(disk, attribute) not in (None, "", "Unknown")]
        if len(values) != len(set(values)):
            raise VhdxProbeError(f"Duplicate virtual-disk mapping: {attribute} is shared by multiple members.")


@dataclass(slots=True)
class _OwnedMember:
    device: StorageDevice
    root: Path
    parent: Path
    folder: Path
    path: Path
    parent_created: bool = False
    folder_created: bool = False
    owned: bool = False
    file_key: tuple[int, int] | None = None
    attach_attempted: bool = False
    disk: VhdxDiskInfo | None = None
    verified: bool = False
    status: str = "NOT TESTED"
    reason: str = ""
    cleanup: str = "Not needed"
    leftover: str | None = None
    may_remain_attached: bool = False


def _assert_owned_file(member: _OwnedMember) -> None:
    info = member.path.lstat()
    if not member.owned or member.path.is_symlink() or (info.st_dev, info.st_ino) != member.file_key:
        raise VhdxProbeError("Temporary file identity changed; lifecycle operation refused.")


def validate_vhdxs_together(
    devices: Sequence[StorageDevice],
    *,
    runner: TogetherDiskPartRunner | None = None,
    roots: Mapping[int, Path] | None = None,
    elevation_checker: Callable[[], bool | None] | None = None,
    name_factory: Callable[[], str] | None = None,
    progress: Callable[[str], None] | None = None,
    diagnostic: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> VhdxTogetherResult:
    """Temporary capability validation only. No filesystem preparation or benchmark."""
    messages: list[str] = []

    def log(message: str) -> None:
        messages.append(message)
        if diagnostic:
            try:
                diagnostic(message)
            except Exception as error:
                messages.append(f"Diagnostic delivery failed: {error}")

    def report(message: str) -> None:
        log(message)
        if progress:
            try:
                progress(message)
            except Exception as error:
                log(f"Progress delivery failed: {error}")

    active = runner or TogetherDiskPartRunner(diagnostic_callback=log)
    members: list[_OwnedMember] = []
    failure = ""
    stage = "Preflight"
    simultaneous = False
    cancelled = False
    preflight_status = "FAILED"
    try:
        if len(devices) != 4 or {item.slot for item in devices} != {1, 2, 3, 4}:
            raise VhdxProbeError("Exactly one connected assigned device for each logical slot 1–4 is required.")
        elevation = (elevation_checker or windows_elevation_status)()
        if elevation is not True:
            preflight_status = "NEEDS ADMINISTRATOR" if elevation is False else "UNKNOWN"
            raise VhdxProbeError(ADMINISTRATOR_MESSAGE if elevation is False else "Windows elevation state could not be determined.")
        token = (name_factory or (lambda: uuid.uuid4().hex))()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token):
            raise VhdxProbeError("Invalid temporary-test token.")
        identities = set()
        physical_paths = set()
        root_paths = set()
        for device in sorted(devices, key=lambda item: item.slot or 0):
            stage = f"Slot {device.slot} preflight"
            blocker = _device_blocker(device)
            if blocker:
                raise VhdxProbeError(blocker)
            identity = _device_identity(device)
            physical = (device.device_path or "").casefold()
            if not identity or not physical or _identity_token(identity) in identities or physical in physical_paths:
                raise VhdxProbeError("Physical device identity is missing, ambiguous or duplicated.")
            identities.add(_identity_token(identity))
            physical_paths.add(physical)
            root = (roots[device.slot] if roots is not None else Path(f"{device.drive_letters[0]}\\")).resolve()
            if not root.is_dir() or not os.access(root, os.W_OK) or device.is_offline is True:
                raise VhdxProbeError(f"Mounted writable filesystem is unavailable: {root}")
            if _path_token(str(root)) in root_paths:
                raise VhdxProbeError("Multiple slots map to the same mounted filesystem.")
            root_paths.add(_path_token(str(root)))
            required = ELIGIBILITY_PROBE_BYTES + VhdxProbeSettings().free_space_reserve_bytes
            if shutil.disk_usage(root).free < required:
                raise VhdxProbeError(f"Insufficient free space on Slot {device.slot}: 8 GiB plus reserve required.")
            parent = root / TEST_FOLDER_NAME
            if parent.exists() and (parent.is_symlink() or parent.is_junction() or not parent.is_dir()):
                raise VhdxProbeError("The dedicated test folder is redirected or invalid.")
            folder = parent / f"together-{token}"
            path = folder / f"slot{device.slot}.vhdx"
            if folder.exists():
                raise VhdxProbeError(f"Refusing a pre-existing test directory/file: {folder}")
            members.append(_OwnedMember(device, root, parent, folder, path))
        # Inspect before writing any VHDX; all four physical members passed preflight.
        active._enumerate_disks()
        for index, member in enumerate(members, 1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                raise VhdxProbeError("Cancellation requested before the next stage.")
            stage = f"Slot {member.device.slot} creation"
            report(f"Creating VHDX {index}/4 — Slot {member.device.slot}; {member.path}")
            if member.parent.exists() and (member.parent.is_symlink() or member.parent.is_junction()):
                raise VhdxProbeError("Test folder became redirected.")
            member.parent_created = not member.parent.exists()
            member.parent.mkdir(exist_ok=True)
            member.folder.mkdir(exist_ok=False)
            member.folder_created = True
            if member.path.exists():
                raise VhdxProbeError("Refusing to overwrite a pre-existing VHDX.")
            try:
                active.create(member.path, ELIGIBILITY_PROBE_BYTES)
            finally:
                # Only a path inside this invocation's exclusively created directory.
                member.owned = member.path.is_file() and not member.path.is_symlink()
                if member.owned:
                    info = member.path.stat()
                    member.file_key = (info.st_dev, info.st_ino)
            if not member.owned:
                raise VhdxProbeError("No temporary VHDX was created.")
        for index, member in enumerate(members, 1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                raise VhdxProbeError("Cancellation requested before the next stage.")
            stage = f"Slot {member.device.slot} attachment"
            report(f"Attaching VHDX {index}/4 — Slot {member.device.slot}")
            _assert_owned_file(member)
            member.attach_attempted = True
            member.disk = replace(
                active.attach(member.path, prepare_filesystem=False),
                can_pool=None, cannot_pool_reasons=(), other_cannot_pool_reason_description="",
            )
            disk = member.disk
            if not _is_file_backed_virtual(disk.bus_type) or not disk.size_is_exact or disk.size_bytes != ELIGIBILITY_PROBE_BYTES:
                raise VhdxProbeError("Attached virtual type/8 GiB size could not be verified.")
            _reject_duplicate_disks([item.disk for item in members if item.disk is not None])
            log(f"Slot {member.device.slot}: {member.path} attached as Disk {disk.disk_number}; identity={disk.identity}.")
        stage = "Simultaneous identity and Storage Spaces validation"
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            raise VhdxProbeError("Cancellation requested before simultaneous inspection.")
        report("Validating Storage Spaces eligibility 4/4 — all four VHDXs attached")
        expected = {member.path: member.disk for member in members}
        inspected = active.inspect_together(expected)
        if set(inspected) != set(expected):
            raise VhdxProbeError("The shared inspection did not map all four members.")
        _reject_duplicate_disks(tuple(inspected.values()))
        for member in members:
            disk = inspected[member.path]
            if disk.disk_number != member.disk.disk_number or not _is_file_backed_virtual(disk.bus_type) or not disk.size_is_exact or disk.size_bytes != ELIGIBILITY_PROBE_BYTES:
                raise VhdxProbeError(f"Slot {member.device.slot}: shared snapshot disagrees with the verified attachment.")
            for field_name in ("storage_unique_id", "storage_device_id", "storage_object_id"):
                before_value = getattr(member.disk, field_name)
                after_value = getattr(disk, field_name)
                if before_value and after_value and _identity_token(before_value) != _identity_token(after_value):
                    raise VhdxProbeError(f"Slot {member.device.slot}: {field_name} changed in the shared snapshot.")
            member.disk = disk
            member.verified = True
            member.status = "READY" if disk.can_pool is True else "NOT READY" if disk.can_pool is False else "UNKNOWN"
            member.reason = "" if disk.can_pool is True else disk.pool_rejection_details if disk.can_pool is False else "Windows Storage providers could not expose CanPool."
            log(f"Slot {member.device.slot}: identity verified; Disk {disk.disk_number}; CanPool={disk.can_pool}; CannotPoolReason={member.reason or '—'}.")
        simultaneous = True
    except Exception as error:
        failure = f"{stage}: {error}"
        log(failure + (f"\n{error.details}" if isinstance(error, VhdxProbeError) and error.details else ""))
        for member in members:
            if stage.startswith(f"Slot {member.device.slot} ") or f"Slot {member.device.slot}:" in failure or member.path.name in failure:
                member.status = "FAILED"
                member.reason = failure
            elif member.disk is not None and not member.verified:
                member.status = "NOT VALIDATED"
                member.reason = "Simultaneous validation aborted: " + failure
    finally:
        for index, member in enumerate(reversed(members), 1):
            report(f"Cleaning up {index}/4 — Slot {member.device.slot}; {member.path}")
            errors = []
            if member.attach_attempted:
                try:
                    _assert_owned_file(member)
                    active.detach(member.path)
                except Exception as error:
                    member.may_remain_attached = True
                    errors.append(f"Detach failed: {error}")
            if member.owned and not member.may_remain_attached:
                try:
                    if not member.root.is_dir():
                        raise VhdxProbeError("Host filesystem is unavailable; temporary-file deletion could not be verified.")
                    if member.path.exists():
                        _assert_owned_file(member)
                        member.path.unlink()
                except Exception as error:
                    errors.append(f"Delete failed: {error}")
            if member.owned and (member.path.exists() or member.may_remain_attached or errors):
                member.leftover = str(member.path)
            member.cleanup = (
                "; ".join(errors) if errors else
                "Detached and temporary file deleted" if member.owned and member.attach_attempted else
                "Temporary file deleted; never attached" if member.owned else
                "No owned temporary file"
            )
            if errors:
                member.status = "CLEANUP FAILED"
            # Remove only empty directories created by this invocation.
            for folder, created in ((member.folder, member.folder_created), (member.parent, member.parent_created)):
                if created:
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
            log(f"Slot {member.device.slot}: cleanup={member.cleanup}; leftover={member.leftover or 'none'}; may remain attached={member.may_remain_attached}.")
    results = tuple(VhdxTogetherMember(
        slot=member.device.slot, vhdx_path=str(member.path), disk=member.disk,
        identity_verified=member.verified, status=member.status, reason=member.reason,
        cleanup_status=member.cleanup, leftover_path=member.leftover,
        may_remain_attached=member.may_remain_attached,
    ) for member in members)
    missing = {item.slot for item in devices if item.slot in (1, 2, 3, 4)} - {item.slot for item in results}
    results += tuple(VhdxTogetherMember(
        slot=slot, vhdx_path="", disk=None, identity_verified=False,
        status="FAILED" if stage.startswith(f"Slot {slot} ") else "NOT TESTED",
        reason=failure if stage.startswith(f"Slot {slot} ") else "Validation stopped before this member.",
        cleanup_status="No temporary file created",
    ) for slot in sorted(missing))
    results = tuple(sorted(results, key=lambda item: item.slot))
    ready = sum(member.status == "READY" for member in results)
    if len(results) == 4 and simultaneous and ready == 4 and not failure:
        status = "READY"
        explanation = "4/4 VHDX members READY simultaneously. All temporary files detached and deleted."
    else:
        status = "CANCELLED" if cancelled else "PARTIAL" if simultaneous and ready else preflight_status
        explanation = failure or f"{ready}/4 VHDX members READY simultaneously; see per-slot reasons and cleanup."
        if any(member.status == "CLEANUP FAILED" for member in results):
            status = "FAILED"
            explanation += " Cleanup failed; review the exact leftover paths before any further VHDX test."
    details = "\n".join(messages)
    return VhdxTogetherResult(status, explanation, results, simultaneous, details)
