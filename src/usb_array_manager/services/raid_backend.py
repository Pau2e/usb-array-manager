from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
import sys

from usb_array_manager import __version__
from usb_array_manager.models.raid_backend import (
    NEEDS_ADMINISTRATOR,
    PARTIALLY_SUPPORTED,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
    BackendAssessment,
    BackendDriveAssessment,
)
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import VhdxProbeResult, VhdxTogetherResult
from usb_array_manager.services.production_planning import (
    ProductionMember, ProductionPlan, plan_production, validation_fingerprint,
)


class RaidBackend(ABC):
    """Read-only capability and planning interface; execution is intentionally absent."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def assess(
        self,
        plan: SavedRaid10Plan | None,
        devices: tuple[StorageDevice, ...] | list[StorageDevice],
        *,
        ambiguous_slots: set[int] | frozenset[int] = frozenset(),
    ) -> BackendAssessment:
        raise NotImplementedError


class WindowsStorageSpacesBackend(RaidBackend):
    """Analyze Windows Storage Spaces eligibility without modifying storage."""

    @property
    def name(self) -> str:
        return "Windows Storage Spaces"

    def assess(
        self,
        plan: SavedRaid10Plan | None,
        devices: tuple[StorageDevice, ...] | list[StorageDevice],
        *,
        ambiguous_slots: set[int] | frozenset[int] = frozenset(),
    ) -> BackendAssessment:
        if plan is None:
            return BackendAssessment(
                backend_name=self.name,
                status=UNKNOWN,
                explanation="Save a RAID10 plan before evaluating deployment capability.",
                requirements=("A saved logical-slot RAID10 plan is required.",),
                blockers=(),
                drives=(),
                dry_run_text=_empty_preview(),
            )

        blockers: list[str] = []
        requirements: list[str] = []
        unknowns: list[str] = []
        selected: list[tuple[int, str, StorageDevice]] = []
        by_slot: dict[int, list[StorageDevice]] = {}
        for device in devices:
            if device.slot is not None:
                by_slot.setdefault(device.slot, []).append(device)

        for pair_index, pair in enumerate(plan.pairs):
            pair_name = f"Pair {chr(ord('A') + pair_index)}"
            for slot in pair:
                matches = by_slot.get(slot, [])
                if len(matches) != 1:
                    blockers.append(
                        f"Slot {slot} does not map to exactly one current physical disk."
                    )
                    continue
                device = matches[0]
                selected.append((slot, pair_name, device))
                if not device.is_connected:
                    blockers.append(f"Slot {slot} is disconnected.")
                if slot in ambiguous_slots:
                    blockers.append(f"Slot {slot} has an ambiguous physical identity.")
                if device.is_boot_disk is True or device.is_system_disk is True:
                    blockers.append(
                        f"Slot {slot} is reported as a Windows system or boot disk and is forbidden."
                    )

        identities = [_physical_identity(device) for _slot, _pair, device in selected]
        counts = Counter(identity for identity in identities if identity is not None)
        duplicate_identities = {identity for identity, count in counts.items() if count > 1}
        for slot, _pair, device in selected:
            identity = _physical_identity(device)
            if identity is None:
                blockers.append(f"Slot {slot} has no unambiguous physical-disk identity.")
            elif identity in duplicate_identities:
                blockers.append(
                    f"Slot {slot} resolves to a physical disk already used by another plan member."
                )

            health = (device.health_status or "").strip().casefold()
            if health and health not in {"healthy", "ok"}:
                blockers.append(
                    f"Slot {slot} reports health status {device.health_status}."
                )
            elif not health:
                unknowns.append(f"Slot {slot} health status is unavailable.")

            if device.is_removable is True:
                blockers.append(
                    f"Slot {slot} is removable media; Storage Spaces requires eligible fixed media."
                )
            elif device.is_removable is None:
                unknowns.append(f"Slot {slot} removable/fixed-media state is unavailable.")

            if device.can_pool is False:
                requirements.append(
                    f"Slot {slot} currently reports CanPool=False; Windows eligibility must be resolved."
                )
            elif device.can_pool is None:
                unknowns.append(f"Slot {slot} Storage Spaces CanPool state is unavailable.")

            if device.partition_count is None:
                unknowns.append(f"Slot {slot} partition state is unavailable.")
            elif device.partition_count > 0:
                filesystems = ", ".join(device.filesystem_types) or "unknown filesystem"
                requirements.append(
                    f"Slot {slot} contains {device.partition_count} partition(s) ({filesystems}); "
                    "a future deployment would erase them."
                )
            if device.is_read_only is True:
                requirements.append(f"Slot {slot} is read-only and would need to be made writable.")
            if device.is_offline is True:
                requirements.append(f"Slot {slot} is offline and would need to be brought online.")
            if device.disk_number is None:
                unknowns.append(f"Slot {slot} Windows disk number is unavailable.")

        blockers = _unique(blockers)
        requirements = _unique(requirements)
        unknowns = _unique(unknowns)
        if blockers:
            status = UNSUPPORTED
            explanation = "Safety validation failed. Deployment planning is blocked."
        elif unknowns:
            status = UNKNOWN
            explanation = "Windows did not expose enough information for a safe decision."
        elif requirements:
            status = PARTIALLY_SUPPORTED
            explanation = (
                "The drives were identified, but Windows reports requirements that a future "
                "destructive deployment would have to resolve."
            )
        else:
            status = SUPPORTED
            explanation = (
                "The saved plan maps uniquely to connected, healthy, pool-eligible fixed disks."
            )

        display_requirements = tuple(requirements + unknowns)
        drive_assessments = tuple(
            _drive_assessment(slot, pair_name, device)
            for slot, pair_name, device in selected
        )
        return BackendAssessment(
            backend_name=self.name,
            status=status,
            explanation=explanation,
            requirements=display_requirements,
            blockers=tuple(blockers),
            drives=drive_assessments,
            dry_run_text=_dry_run_preview(
                self.name,
                plan,
                drive_assessments,
                tuple(blockers),
            ),
        )


class VhdxStorageSpacesBackend(RaidBackend):
    """Evaluate file-backed virtual disks while preserving physical USB data."""

    def __init__(self) -> None:
        self._probe_results: dict[int, VhdxProbeResult] = {}
        self._together_result: VhdxTogetherResult | None = None
        self._together_identities: dict[int, str | None] = {}
        self._production_snapshot: dict[int, tuple] = {}
        self._production_saved_plan: SavedRaid10Plan | None = None

    @property
    def name(self) -> str:
        return "VHDX-backed Storage Spaces"

    def record_probe(self, result: VhdxProbeResult) -> None:
        self._probe_results[result.slot] = result

    def probe_for_slot(self, slot: int) -> VhdxProbeResult | None:
        return self._probe_results.get(slot)

    def record_together(
        self,
        result: VhdxTogetherResult | None,
        devices: tuple[StorageDevice, ...] = (),
        plan: SavedRaid10Plan | None = None,
    ) -> None:
        """Replace session evidence; never turn it into a single-slot probe result."""
        self._together_result = result
        self._together_identities = {
            device.slot: _physical_identity(device)
            for device in devices if device.slot is not None
        } if len(devices) == 4 else {}
        self._production_snapshot = {
            device.slot: validation_fingerprint(device)
            for device in devices if device.slot is not None
        }
        self._production_saved_plan = plan

    def production_plan(
        self, plan: SavedRaid10Plan | None,
        devices: tuple[StorageDevice, ...] | list[StorageDevice],
        *, ambiguous_slots: set[int] | frozenset[int] = frozenset(),
        application_drives: frozenset[str] | None = None,
    ) -> ProductionPlan:
        selected, blockers = _vhdx_plan_members(plan, devices, ambiguous_slots) if plan else ([], [])
        current = {device.slot: validation_fingerprint(device) for _, _, device in selected}
        proven = (
            self._together_succeeded(devices, ambiguous_slots)
            and plan is not None and plan == self._production_saved_plan
            and current == self._production_snapshot
        )
        if application_drives is None:
            application_drives = frozenset(
                Path(value).drive.casefold() for value in
                (__file__, sys.executable, sys.argv[0], str(Path.cwd()))
                if Path(value).drive
            )
        return plan_production(plan, selected, blockers, proven,
                               application_drives=application_drives)

    def _together_succeeded(
        self,
        devices: tuple[StorageDevice, ...] | list[StorageDevice],
        ambiguous_slots: set[int] | frozenset[int],
    ) -> bool:
        result = self._together_result
        if (
            result is None or result.status != "READY" or not result.simultaneous_verified
            or len(result.members) != 4
            or {member.slot for member in result.members} != {1, 2, 3, 4}
            or set(self._together_identities) != {1, 2, 3, 4}
        ):
            return False
        for member in result.members:
            matches = [device for device in devices if device.slot == member.slot]
            if (
                len(matches) != 1 or not matches[0].is_connected
                or member.slot in ambiguous_slots
                or not self._together_identities[member.slot]
                or _physical_identity(matches[0]) != self._together_identities[member.slot]
                or not member.identity_verified or member.disk is None
                or member.disk.can_pool is not True or member.status != "READY"
                or member.cleanup_status != "Detached and temporary file deleted"
                or member.leftover_path is not None or member.may_remain_attached
            ):
                return False
        return True

    def assess(
        self,
        plan: SavedRaid10Plan | None,
        devices: tuple[StorageDevice, ...] | list[StorageDevice],
        *,
        ambiguous_slots: set[int] | frozenset[int] = frozenset(),
    ) -> BackendAssessment:
        together_succeeded = self._together_succeeded(devices, ambiguous_slots)
        if plan is None:
            needs_admin = next(
                (
                    result
                    for result in reversed(tuple(self._probe_results.values()))
                    if result.status == NEEDS_ADMINISTRATOR
                ),
                None,
            )
            return BackendAssessment(
                backend_name=self.name,
                status=NEEDS_ADMINISTRATOR if needs_admin else UNKNOWN,
                explanation=(
                    needs_admin.explanation
                    if needs_admin
                    else "Save a RAID10 plan; four-member capability validation has succeeded."
                    if together_succeeded
                    else "Save a RAID10 plan and probe one mounted USB slot first."
                ),
                requirements=("A saved logical-slot RAID10 plan is required.",),
                blockers=(),
                drives=(),
                dry_run_text=_empty_preview(),
            )

        selected, blockers = _vhdx_plan_members(plan, devices, ambiguous_slots)
        saved_plan_blockers = tuple(blockers)
        requirements: list[str] = []
        filesystem_blocked = False
        for slot, _pair, device in selected:
            filesystems = {value.casefold() for value in device.filesystem_types}
            if "fat32" in filesystems:
                filesystem_blocked = True
                blockers.append(
                    f"Slot {slot} uses FAT32. A production VHDX larger than 4 GiB "
                    "requires migration to NTFS for this Windows-only deployment; the application will not reformat it."
                )
            elif not filesystems:
                requirements.append(f"Slot {slot} filesystem type is unknown.")
            if not device.drive_letters:
                blockers.append(f"Slot {slot} has no mounted writable filesystem.")
            if device.is_read_only is True:
                blockers.append(f"Slot {slot} is read-only.")

        current_by_slot = {slot: device for slot, _pair, device in selected}
        relevant_results = [
            result
            for result in self._probe_results.values()
            if result.slot in current_by_slot
            and result.source_identity == _display_identity(current_by_slot[result.slot])
        ]
        successful = [
            result
            for result in relevant_results
            if result.status == SUPPORTED and result.leftover_path is None
        ]
        capability_verified = bool(successful) or together_succeeded
        if not capability_verified:
            requirements.insert(0, "Probe one selected USB slot successfully before deployment planning.")

        blockers = _unique(blockers)
        requirements = _unique(requirements)
        if capability_verified:
            status = SUPPORTED
            explanation = _deployment_summary(
                blockers, requirements,
                filesystem_blocked=filesystem_blocked,
                saved_plan_blocked=bool(saved_plan_blockers),
            )
        elif not capability_verified and relevant_results:
            latest = relevant_results[-1]
            status = latest.status
            explanation = latest.explanation
        elif not capability_verified:
            status = UNKNOWN
            explanation = "No successful temporary VHDX capability probe is available."
        elif requirements:
            status = PARTIALLY_SUPPORTED
            explanation = "The probe succeeded, but some production filesystem facts remain unknown."
        else:
            status = SUPPORTED
            explanation = (
                "A temporary VHDX was attachable, pool-eligible, and cleaned up; all saved "
                "members have production-compatible mounted filesystems."
            )

        production = self.production_plan(plan, devices, ambiguous_slots=ambiguous_slots)
        production_members = {member.slot: member for member in production.members}
        drive_assessments = tuple(
            _vhdx_drive_assessment(slot, pair, device, production_members.get(slot))
            for slot, pair, device in selected
        )
        return BackendAssessment(
            backend_name=self.name,
            status=status,
            explanation=explanation,
            requirements=tuple(requirements),
            blockers=tuple(blockers),
            drives=drive_assessments,
            dry_run_text=production.preview,
        )


def _deployment_summary(
    blockers: list[str], requirements: list[str], *,
    filesystem_blocked: bool, saved_plan_blocked: bool,
) -> str:
    """Summarize existing assessment facts without changing any validation state."""
    if blockers:
        if filesystem_blocked and saved_plan_blocked:
            reason = "host filesystem and saved-plan validation requirements remain"
        elif filesystem_blocked:
            reason = "host filesystems require migration to NTFS"
        elif saved_plan_blocked:
            reason = "saved-plan validation requirements remain"
        else:
            reason = "mounted writable host filesystem requirements remain"
        return f"Production deployment: BLOCKED — {reason}."
    if requirements:
        return "Production deployment: WARNING — filesystem information remains incomplete."
    return "Production deployment: READY"


def _vhdx_plan_members(
    plan: SavedRaid10Plan,
    devices: tuple[StorageDevice, ...] | list[StorageDevice],
    ambiguous_slots: set[int] | frozenset[int],
) -> tuple[list[tuple[int, str, StorageDevice]], list[str]]:
    by_slot: dict[int, list[StorageDevice]] = {}
    for device in devices:
        if device.slot is not None:
            by_slot.setdefault(device.slot, []).append(device)
    selected: list[tuple[int, str, StorageDevice]] = []
    blockers: list[str] = []
    for pair_index, pair in enumerate(plan.pairs):
        pair_name = f"Pair {chr(ord('A') + pair_index)}"
        for slot in pair:
            matches = by_slot.get(slot, [])
            if len(matches) != 1:
                blockers.append(f"Slot {slot} does not map to exactly one current physical disk.")
                continue
            device = matches[0]
            selected.append((slot, pair_name, device))
            if not device.is_connected:
                blockers.append(f"Slot {slot} is disconnected.")
            if slot in ambiguous_slots:
                blockers.append(f"Slot {slot} has an ambiguous physical identity.")
            if device.is_boot_disk is True or device.is_system_disk is True:
                blockers.append(f"Slot {slot} is a Windows system or boot disk and is forbidden.")
            elif device.is_boot_disk is None or device.is_system_disk is None:
                blockers.append(
                    f"Slot {slot} could not be proven independent of the Windows system/boot disk."
                )
    identities = [_physical_identity(device) for _slot, _pair, device in selected]
    counts = Counter(identity for identity in identities if identity is not None)
    duplicates = {identity for identity, count in counts.items() if count > 1}
    for slot, _pair, device in selected:
        identity = _physical_identity(device)
        if identity is None:
            blockers.append(f"Slot {slot} has no unambiguous physical-disk identity.")
        elif identity in duplicates:
            blockers.append(f"Slot {slot} resolves to a physical disk already used by another plan member.")
    return selected, blockers


def _vhdx_drive_assessment(
    slot: int,
    pair: str,
    device: StorageDevice,
    production_member: ProductionMember | None = None,
) -> BackendDriveAssessment:
    drive = device.drive_letters[0] if device.drive_letters else "<unmounted>"
    filesystem = ", ".join(device.filesystem_types) or "unknown filesystem"
    path = production_member.path if production_member else None
    host = production_member.volume.identity if path and production_member.volume else "UNPROVEN"
    return BackendDriveAssessment(
        slot=slot,
        pair=pair,
        physical_disk=f"Current {drive} (display only); persistent host {host}; planned {path or 'BLOCKED — unproven host mapping'}",
        model=device.model or "Unknown",
        identity=_display_identity(device),
        capacity_bytes=device.capacity_bytes,
        bus_type=device.bus_type or "USB",
        media_type=device.media_type or "Unknown",
        removable=_yes_no_unknown(device.is_removable),
        can_pool="Determined by VHDX probe",
        partition_state=filesystem,
        health=device.health_status or device.operational_status or "Unknown",
        operations=(
            "PREVIEW ONLY: this version leaves the existing USB filesystem and user files unchanged.",
            f"FUTURE: use the revalidated persistent host path {path or '<UNPROVEN VOLUME GUID>'}; never a drive letter as identity.",
            "After host filesystem migration, rediscover Volume GUIDs and recalculate capacity/free space and VHDX sizing before creating any production file.",
            "FUTURE: attach the file-backed virtual disk and revalidate CanPool.",
        ),
    )


def _drive_assessment(
    slot: int,
    pair: str,
    device: StorageDevice,
) -> BackendDriveAssessment:
    operations = ["Verify current slot-to-physical-disk mapping and identity."]
    if device.is_offline is True:
        operations.append("FUTURE DESTRUCTIVE DEPLOYMENT: bring the disk online.")
    if device.is_read_only is True:
        operations.append("FUTURE DESTRUCTIVE DEPLOYMENT: clear the read-only flag.")
    if device.partition_count is None or device.partition_count > 0:
        operations.append(
            "FUTURE DESTRUCTIVE DEPLOYMENT: erase partitions and filesystem data."
        )
    operations.extend(
        (
            "FUTURE DESTRUCTIVE DEPLOYMENT: claim the disk for a storage pool.",
            "FUTURE DESTRUCTIVE DEPLOYMENT: create mirrored/striped virtual storage.",
        )
    )
    partition_state = "Unknown"
    if device.partition_count is not None:
        filesystems = ", ".join(device.filesystem_types) or "no mounted filesystem"
        partition_state = (
            f"{device.partition_count} partition(s); {filesystems}; "
            f"style {device.partition_style or 'unknown'}"
        )
    return BackendDriveAssessment(
        slot=slot,
        pair=pair,
        physical_disk=(
            f"Disk {device.disk_number}" if device.disk_number is not None
            else device.device_path or "Unknown"
        ),
        model=device.model or "Unknown",
        identity=_display_identity(device),
        capacity_bytes=device.capacity_bytes,
        bus_type=device.bus_type or "Unknown",
        media_type=device.media_type or "Unknown",
        removable=_yes_no_unknown(device.is_removable),
        can_pool=_yes_no_unknown(device.can_pool),
        partition_state=partition_state,
        health=" / ".join(
            value for value in (device.health_status, device.operational_status) if value
        ) or "Unknown",
        operations=tuple(operations),
    )


def _physical_identity(device: StorageDevice) -> str | None:
    for prefix, value in (
        ("storage", device.storage_unique_id),
        ("container", device.container_id),
        ("pnp", device.pnp_device_id),
        ("path", device.device_path),
    ):
        if value:
            return f"{prefix}:{' '.join(value.split()).casefold()}"
    return None


def _display_identity(device: StorageDevice) -> str:
    return (
        device.storage_unique_id
        or device.container_id
        or device.pnp_device_id
        or device.device_path
        or "Unknown"
    )


def _yes_no_unknown(value: bool | None) -> str:
    if value is None:
        return "Unknown"
    return "Yes" if value else "No"


def _empty_preview() -> str:
    return (
        "PREVIEW ONLY — NOT EXECUTED\n"
        "No saved RAID10 plan is available, so no deployment steps were generated.\n"
    )


def _dry_run_preview(
    backend_name: str,
    plan: SavedRaid10Plan,
    drives: tuple[BackendDriveAssessment, ...],
    blockers: tuple[str, ...],
) -> str:
    lines = [
        "PREVIEW ONLY — NOT EXECUTED",
        "NO DISK COMMANDS WERE RUN. THIS VERSION CANNOT MODIFY STORAGE.",
        "",
        f"Backend: {backend_name}",
        f"Saved plan timestamp: {plan.saved_at}",
        "",
        "Current saved-plan mapping:",
    ]
    for drive in drives:
        lines.append(
            f"- {drive.pair}, Slot {drive.slot} -> {drive.physical_disk}; "
            f"{drive.model}; {drive.identity}; {_capacity(drive.capacity_bytes)}"
        )
        lines.extend(f"    - {operation}" for operation in drive.operations)
    if blockers:
        lines.extend(("", "SAFETY STOP — future deployment must not proceed:"))
        lines.extend(f"- {blocker}" for blocker in blockers)
        lines.extend(
            (
                "",
                "No command outline was generated because safety validation failed.",
            )
        )
        return "\n".join(lines) + "\n"

    disk_numbers = [
        drive.physical_disk.removeprefix("Disk ")
        for drive in drives
        if drive.physical_disk.startswith("Disk ")
    ]
    disk_list = ", ".join(disk_numbers) or "<verified disk numbers>"
    lines.extend(
        (
            "",
            "Future PowerShell command outline (TEXT ONLY):",
            "# PREVIEW ONLY — NOT EXECUTED",
            f"$plannedDiskNumbers = @({disk_list})",
            "$plannedDisks = Get-Disk | Where-Object Number -in $plannedDiskNumbers",
            "# Re-verify model, serial/UniqueId, size, BusType, IsBoot, and IsSystem.",
            "# FUTURE DESTRUCTIVE: Clear-Disk for each explicitly re-confirmed member.",
            "# FUTURE DESTRUCTIVE: New-StoragePool using verified pool-eligible disks.",
            "# FUTURE DESTRUCTIVE: New-VirtualDisk with mirrored resiliency and validated columns.",
            "# FUTURE DESTRUCTIVE: Initialize-Disk, New-Partition, and Format-Volume.",
            "",
            f"These lines are documentation only. No production execution API exists in v{__version__}.",
        )
    )
    return "\n".join(lines) + "\n"


def _capacity(value: int | None) -> str:
    return f"{value / 1024**3:.1f} GiB" if value is not None else "unknown capacity"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
