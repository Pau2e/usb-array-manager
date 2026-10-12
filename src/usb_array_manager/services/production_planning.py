"""Pure production planning. No process, filesystem-write or storage execution API."""
from __future__ import annotations

import re
from dataclasses import dataclass

from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import HostVolume, StorageDevice

GIB = 1024**3
ALIGNMENT = 256 * 1024**2
VHDX_OVERHEAD = 256 * 1024**2
VOLUME_GUID = re.compile(r"^\\\\\?\\Volume\{[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}\\$")


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProductionMember:
    slot: int
    letter: str
    volume: HostVolume | None
    path: str | None
    filesystem_ready: bool
    action: str


@dataclass(frozen=True, slots=True)
class ProductionPlan:
    status: str
    checks: tuple[ReadinessCheck, ...]
    members: tuple[ProductionMember, ...]
    logical_bytes: int
    reserve_bytes: int
    smallest_bytes: int
    physical_bytes: int
    virtual_raw_bytes: int
    mirrored_bytes: int
    formatted_bytes: int
    preview: str


def validation_fingerprint(device: StorageDevice) -> tuple:
    """Letters/free space may change; physical host layout may not."""
    return (
        device.capacity_bytes, device.partition_count,
        tuple(sorted(value.casefold() for value in device.filesystem_types)),
        tuple(sorted((
            (volume.identity or "").casefold(),
            (volume.filesystem or "").casefold(), volume.capacity_bytes if volume.capacity_bytes is not None else -1,
        ) for volume in device.host_volumes)),
    )


def plan_production(
    plan: SavedRaid10Plan | None,
    selected: list[tuple[int, str, StorageDevice]],
    safety_blockers: list[str],
    simultaneous_ready: bool,
    *,
    application_drives: frozenset[str] = frozenset(),
) -> ProductionPlan:
    """Recalculate on every assessment, never cache a READY deployment result."""
    checks: list[ReadinessCheck] = []
    members: list[ProductionMember] = []
    blockers = list(safety_blockers)
    if plan is None or len(plan.slots) != 4 or set(plan.slots) != {1, 2, 3, 4}:
        blockers.append("A saved four-member plan is required.")
    disk_numbers = [device.disk_number for _, _, device in selected]
    if len(disk_numbers) != 4 or None in disk_numbers or len(set(disk_numbers)) != 4:
        blockers.append("Four distinct current physical disk numbers are required in addition to persistent identity.")
    for slot, _pair, device in sorted(selected):
        if device.is_read_only is True or device.is_offline is True:
            blockers.append(f"Slot {slot} is read-only or offline.")
        if (device.bus_type or "").casefold() != "usb":
            blockers.append(f"Slot {slot} is not proven to be a USB host disk.")
        if (device.health_status or "").casefold() not in {"healthy", "ok"}:
            blockers.append(f"Slot {slot} health is unhealthy or unknown.")
        if any(letter.casefold() in application_drives for letter in device.drive_letters):
            blockers.append(f"Slot {slot} contains the application and is forbidden.")
        volume = device.host_volumes[0] if len(device.host_volumes) == 1 else None
        if len(device.drive_letters) != 1 or device.partition_count != 1:
            volume = None
        if volume and (volume.drive_letter or "").casefold() != device.drive_letters[0].casefold():
            blockers.append(f"Slot {slot} volume association does not match the current mounted host.")
            volume = None
        if volume and {(value or "").casefold() for value in device.filesystem_types} != {(volume.filesystem or "").casefold()}:
            blockers.append(f"Slot {slot} filesystem observations disagree; refresh and revalidate.")
        stable = bool(volume and VOLUME_GUID.fullmatch(volume.identity or ""))
        if volume and (volume.is_system is not False or volume.is_boot is not False):
            blockers.append(f"Slot {slot} host volume is system/boot or cannot be proven safe.")
        fs_ready = bool(volume and (volume.filesystem or "").upper() == "NTFS")
        action = (
            "No host filesystem migration required." if fs_ready
            else "Back up all data; migrate host filesystem to NTFS (DESTRUCTIVE, future only); "
                 "restore data and re-prove the new Volume GUID. No automatic formatting."
        )
        members.append(ProductionMember(
            slot, device.drive_letters[0] if device.drive_letters else "Unmounted",
            volume, (volume.identity + f"USBArrayManager\\slot{slot}.vhdx") if stable else None,
            fs_ready, action,
        ))
    volumes = [member.volume for member in members]
    identities = [(volume.identity or "").casefold() for volume in volumes if volume]
    stable_ready = (
        len(members) == 4 and all(member.path for member in members)
        and len(set(identities)) == 4
    )
    if not stable_ready:
        blockers.append("Persistent host volume mapping is missing, duplicate or ambiguous; do not guess.")
    capacities = [
        min(device.capacity_bytes or 0, member.volume.capacity_bytes or 0)
        for (_slot, _pair, device), member in zip(sorted(selected), members)
        if member.volume
    ]
    smallest = min(capacities) if len(capacities) == 4 else 0
    reserve = max(2 * GIB, (smallest * 8 + 99) // 100) if smallest else 0
    logical = max(0, (smallest - reserve - VHDX_OVERHEAD) // ALIGNMENT * ALIGNMENT)
    sizing_ready = logical >= GIB and smallest > 0
    filesystem_ready = len(members) == 4 and all(member.filesystem_ready for member in members)
    free_ready = sizing_ready and len(members) == 4 and all(
        member.volume and member.volume.free_bytes is not None
        and member.volume.capacity_bytes is not None
        and 0 <= member.volume.free_bytes <= member.volume.capacity_bytes
        and member.volume.free_bytes >= logical + reserve + VHDX_OVERHEAD
        for member in members
    )
    def add(name: str, ready: bool, detail: str) -> None:
        checks.append(ReadinessCheck(name, "READY" if ready else "BLOCKED", detail))
    add("Four-member VHDX validation", simultaneous_ready,
        "Current 4/4 simultaneous identity, CanPool and cleanup proof required; no extra single-slot probe.")
    add("Slot identity mapping", not blockers and len(members) == 4,
        "; ".join(blockers) or "Four unique connected, healthy, non-system members.")
    add("Storage Spaces eligibility", simultaneous_ready, "All four verified VHDXs must report CanPool=True together.")
    add("Host filesystem", filesystem_ready,
        "NTFS required for this Windows-only plan. FAT32 cannot host the planned production-size VHDX; migration required.")
    add("Production VHDX sizing", sizing_ready,
        f"Smallest usable host: {gib(smallest)}; reserved host slack max(2 GiB, 8%) = {gib(reserve)} per host; "
        f"VHDX / filesystem overhead allowance: {gib(VHDX_OVERHEAD)}; "
        f"subtract slack and overhead, round down to 256 MiB: recommended production VHDX {gib(logical)} each, Dynamic. "
        "The reserve retains host filesystem working space at full VHDX growth.")
    add("Free-space requirement", free_ready,
        f"Each host requires {gib(logical + reserve + VHDX_OVERHEAD)} free for full VHDX growth, "
        "overhead and retained reserve. Existing user data is not counted as reclaimable.")
    add("Persistent volume identity", stable_ready,
        "Exactly one proven Volume GUID per slot, unique across members. Letters are display only; "
        "reformatting invalidates identity and requires fresh validation.")
    add("Storage Spaces layout", len(members) == 4,
        "Two-way mirror / RAID10-like layout: 4 members, 2 data copies, 2 columns, fixed provisioning. "
        "Not true RAID10 or the planner's fixed mirror pairs. One member failure guaranteed; "
        "two failures are NOT guaranteed tolerable. Provider must confirm requested geometry. Final filesystem NTFS; mount point not configured.")
    checks.append(ReadinessCheck("Deployment commands", "PREVIEW ONLY", "Text only; no Deploy/Create action or production execution API."))
    checks.append(ReadinessCheck("Final mount point", "WARNING", "Not configured; a future deployment must choose and validate a collision-free mount point."))
    raw = logical * 4
    mirrored = raw // 2
    # Explicit conservative estimates, not Storage Spaces allocation guarantees.
    virtual_size = mirrored * 99 // 100 // ALIGNMENT * ALIGNMENT
    formatted = virtual_size * 99 // 100
    state = (
        "BLOCKED" if any(check.status == "BLOCKED" for check in checks)
        else "WARNING" if any(check.status == "WARNING" for check in checks)
        else "READY FOR DEPLOYMENT"
    )
    preview = deployment_preview(members, logical, virtual_size, state, plan)
    return ProductionPlan(state, tuple(checks), tuple(members), logical, reserve, smallest,
                          sum(device.capacity_bytes or 0 for _, _, device in selected),
                          raw, mirrored, formatted, preview)


def gib(value: int | None) -> str:
    return f"{value / GIB:.2f} GiB" if value is not None else "Unknown"


def deployment_preview(
    members: list[ProductionMember], logical: int, virtual_size: int, state: str,
    saved_plan: SavedRaid10Plan | None = None,
) -> str:
    needs_migration = any(not member.filesystem_ready for member in members)
    lines = [
        *(["PREVIEW ONLY — BLOCKED — DO NOT EXECUTE"] if state == "BLOCKED" else []),
        "PREVIEW ONLY — NOT EXECUTED", f"Production deployment: {state}",
        "This is a contingent procedure, NOT an executable script. No production files or pools created.",
        f"Saved plan timestamp: {saved_plan.saved_at if saved_plan else 'NONE'}",
        f"Saved logical slots: {saved_plan.slots if saved_plan else 'NONE'}; fixed RAID10 pairs are not enforced by Storage Spaces.",
        "Current drive letters are display only, never persistent identifiers.",
        f"Current VHDX sizing estimate: {gib(logical)} per member; "
        + ("PRE-FORMAT ESTIMATE — NOT an approved post-format size." if needs_migration else "revalidate capacity and free space before any future creation."),
        "Current identity and deterministic file layout (authoritative host: proven Volume GUID, NOT letter):",
    ]
    for member in members:
        lines.extend((
            f"Slot {member.slot}",
            f"  Current drive letter (readability only): {member.letter}",
            f"  Current host filesystem: {member.volume.filesystem if member.volume else 'UNKNOWN'}",
            f"  Persistent Volume GUID (authoritative): {member.volume.identity if member.path and member.volume else 'UNPROVEN — DO NOT GUESS'}",
            f"  Deterministic production VHDX path: {member.path or 'BLOCKED — persistent host mapping unproven'}",
        ))
    lines.extend((
        "", "Complete FUTURE deployment sequence — TEXT ONLY:",
        "1. Back up and verify all existing member data.",
        "2. DESTRUCTIVE (future, explicit consent): migrate non-NTFS host filesystems to NTFS; restore data.",
        "   No host formatting commands generated: current identities will become stale after migration.",
        "3. Rediscover/revalidate each post-format Volume GUID against its persistent physical slot; check health, system/boot and application-disk exclusions.",
        "4. REQUIRED AFTER FORMATTING: re-inspect capacity/free space after restoring user data, recalculate reserves and production VHDX sizing, and rebuild paths from the revalidated Volume GUIDs.",
        "   Never carry the pre-format size or old Volume GUID forward blindly if post-format capacity or identity changes.",
        "5. Re-run four-member simultaneous validation after ANY identity, filesystem or plan change; stop on ambiguity or insufficient free space.",
        "6. FUTURE FILE CREATION: check for existing files, create the dedicated directory and four equally sized dynamic production VHDXs using freshly calculated sizes and proven paths.",
        "7. Attach and identity-verify all four production VHDXs by path, size, UniqueId, DeviceId/ObjectId and File Backed Virtual type.",
        "8. Revalidate CanPool=True and healthy status for all four simultaneously; build $verifiedVhdxPhysicalDisks from exact verified identities, never position/disk number alone.",
        "9. FUTURE STORAGE MODIFICATION: create a Storage Spaces pool containing only the four verified VHDX-backed disks.",
        "10. Create a two-way mirrored virtual disk (RAID10-like, not fixed RAID10 pairs); verify two copies/two columns and freshly calculated usable size.",
        "11. DESTRUCTIVE TO NEW VERIFIED VIRTUAL DISK ONLY: initialize/partition the new Storage Spaces virtual disk and create its final NTFS filesystem.",
        "12. Choose/validate a collision-free final mount point; assign it only in a future explicitly approved deployment.",
        "13. Plan safe boot-time VHDX attachment using the revalidated persistent host identities.",
        "14. Verify final pool/virtual disk/filesystem health, mount point and reboot recovery.",
        "", "VHDX creation / attachment — FUTURE FILE CREATION (DiskPart /s scripts, text only):",
    ))
    for member in members:
        path = (
            f"<REVALIDATED_VOLUME_GUID_SLOT_{member.slot}>\\USBArrayManager\\slot{member.slot}.vhdx"
            if needs_migration else member.path or f"<UNPROVEN HOST VOLUME FOR SLOT {member.slot}>"
        )
        size = "<RECALCULATED_VHDX_SIZE_MIB>" if needs_migration else str(logical // 1024**2)
        lines.extend((f"Slot {member.slot} — contingent on fresh host identity and sizing verification:",
                      f'create vdisk file="{path}" maximum={size} type=expandable',
                      f'select vdisk file="{path}"', "attach vdisk", "exit"))
    planned_virtual_size = "<RECALCULATED_MIRRORED_SIZE_BYTES>" if needs_migration else str(virtual_size)
    lines.extend((
        "", "Storage Spaces creation — FUTURE STORAGE MODIFICATION (PowerShell TEXT ONLY):",
        "# Stop unless exactly 4 verified file-backed members, no physical USB disks, no existing pool/name collision.",
        "$pool = New-StoragePool -FriendlyName 'USBArrayManagerPool' -StorageSubSystemUniqueId '<REVALIDATED SUBSYSTEM UNIQUE ID>' -PhysicalDisks $verifiedVhdxPhysicalDisks",
        f"$virtual = New-VirtualDisk -StoragePoolUniqueId $pool.UniqueId -FriendlyName 'USBArrayManagerMirror' -ResiliencySettingName Mirror -NumberOfDataCopies 2 -NumberOfColumns 2 -ProvisioningType Fixed -Size {planned_virtual_size}",
        "# Re-query geometry/copies/health; STOP if Windows cannot honor the requested layout. No silent fallback.",
        "", "Final volume — DESTRUCTIVE TO NEW VERIFIED STORAGE SPACES VIRTUAL DISK ONLY:",
        "$finalDisk = $virtual | Get-Disk # Reverify this exact new pool virtual disk; never a USB disk.",
        "$finalDisk | Initialize-Disk -PartitionStyle GPT",
        "$partition = $finalDisk | New-Partition -UseMaximumSize # No drive letter assigned automatically.",
        "$partition | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'USBArrayManager' -Confirm:$true",
        "# Final drive letter / mount point: NOT CONFIGURED. Select a collision-free destination explicitly before deployment.",
        "", "Startup / persistence configuration — PLANNING ONLY:",
        "Locate each host using its recorded Volume GUID plus persistent physical slot identity, not drive letters.",
        "Re-prove volume-to-physical-disk association and exact owned VHDX path before attachment.",
        "Future elevated startup orchestration would wait for all four hosts, attach verified VHDXs sequentially,",
        "then wait for Storage Spaces discovery and validate array health before exposing the final mount point.",
        "If a USB is missing: report degraded/unavailable; never create replacements, initialize, repair or format automatically.",
        "Duplicate/ambiguous/changed identities: block auto-attachment and request manual review.",
        "No Scheduled Tasks, services, registry entries or startup scripts are created in this version.",
    ))
    return "\n".join(lines)
