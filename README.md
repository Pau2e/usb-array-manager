# USB Array Manager

A Windows desktop application that inventories connected USB storage devices,
maps them to persistent logical slots, and safely benchmarks mounted filesystems.

## Version 0.7.5.3 — Conservative sizing and separate inventory/benchmark tabs

Production VHDX sizing uses the smallest usable host volume. Reserve
**max(2 GiB, 8% of that capacity)** as host filesystem working space, add
**256 MiB** of VHDX/filesystem overhead, and round the remaining logical size
DOWN to 256 MiB. The same calculated size is used on all four members. For
approximately 28.64 GiB hosts this gives about 26.00 GiB each, 104 GiB raw
virtual capacity and 52 GiB theoretical two-way mirrored capacity; formatted
capacity is estimated slightly lower, never promised exactly.

This remains a pre-format estimate for FAT32 hosts. Migration requires fresh
Volume GUID discovery, capacity/free-space and existing-data checks, recalculated
reserves/sizes and current validation before any future creation. Command previews
retain `<RECALCULATED_VHDX_SIZE_MIB>` while migration is outstanding.

**Overview** and **Benchmarks** are separate full-width tabs again. The original
widgets, inventory/benchmark models, slot state, controller signals and sequential
batch services are reused, not cloned. Changing tabs does not reload or copy
benchmark results or slot state. The unused merged-page wrapper was removed.
Current runtime, project and installed package metadata report **0.7.5.3**.
Existing preview/readiness, GUID identity, probing, cleanup, safety and RAID/Storage
Spaces layout behavior remain intact. No production action or Version 0.8 work
has been added.

Manual checks: verify the sizing calculation and pre-format placeholders; switch
between Overview and Benchmarks after a result, verify slots/results remain the
same, and check slot assignment, per-slot benchmark actions, Runs and Benchmark All.

## Historical release 0.7.5.2 — Preview/readiness cleanup

Final UI cleanup merges inventory and benchmark controls into one
**Overview / Benchmarks** page. The original inventory panel sits on the left
and the original benchmark panel on the right, in a resizable 45% / 55%
horizontal split. Both retain their existing models, selection, signals and
services; no independent second benchmark UI exists. Per-slot benchmark buttons
are placed beside the slot column visually, keeping actions visible in the
narrower panel without changing model indices. Wider tables scroll horizontally.
The standalone Benchmarks navigation tab is removed. Runs, Benchmark All,
qualification/status/help, slot assignment and refresh remain unchanged.

That release's source, project/package metadata and installed editable distribution
reported **0.7.5.2**. Earlier version headings below describe historical changes,
not the current running application.

Deployment preview and Production readiness now use the same production planning
text. Each member displays its current letter for readability, its authoritative
persistent Volume GUID, and the deterministic `USBArrayManager\slotN.vhdx` path.
Unknown host identity never falls back to a drive-letter-based production path.

Blocked plans still show the complete future sequence and conditional command
templates, labelled **PREVIEW ONLY — BLOCKED — DO NOT EXECUTE**. This does not
remove any blocker or add an executor. After FAT32 → NTFS migration, rediscover
and revalidate every Volume GUID, restore user data, remeasure capacity/free space,
recalculate reserves/VHDX size and repeat four-member validation before production
creation. Current sizing is a **pre-format estimate**, not an approved final size.
Migration command templates use post-format GUID and size placeholders, never
blindly carrying old values forward.

Manual checks: after four-member success on FAT32, open Deployment preview and
confirm the blocked banner, four GUID/path mappings, all future steps and sizing
placeholders. Compare it with Production readiness's command preview. No production
files, formatting, pools or startup registrations should be created.

## Historical release 0.7.5.1 — UI/state summary cleanup

The backend capability label is shown once. Production status wording is derived
from active filesystem and saved-plan blocker categories, without changing the
underlying blockers or validation rules. Planned operations headings use
`Planned operations by drive — Slot N` with explicit paragraph separation.

The user also approved a narrow temporary-performance-volume preparation fix:
the verified temporary VHDX partition is formatted before assigning its test
drive letter, preventing exposure of an unformatted drive to Explorer. This
avoids the Windows "You need to format the disk" prompt caused by early letter
assignment. It does not change physical USB letters, modify physical partitions,
alter capability-only probing/CanPool queries, or bypass identity verification.
Format errors still stop preparation and use the existing detach/delete cleanup.
No global automount, Windows notification or registry settings are modified.

Manual checks: verify the concise top summary for FAT32-only and combined
blockers; check each slot heading; run a confirmed performance test and verify
that Windows no longer prompts to format the temporary drive and cleanup succeeds.
Do not click Format in a Windows prompt; cancel and inspect Logs / Details if one
still appears. No production deployment or Version 0.8 work is included.

## Version 0.7.5 — Production readiness (PREVIEW ONLY)

Backend / Deployment includes a Production readiness tab with checklist, host
filesystem facts, deterministic Volume GUID file paths, sizing/capacity estimates,
future command text and reboot/auto-attach planning. A proven backend is SUPPORTED
even when production deployment is BLOCKED by FAT32. NTFS is recommended for this
Windows-only plan; filesystem migration is never performed automatically.

The equal dynamic VHDX logical size is derived from the smallest usable host
volume, now reserving max(2 GiB, 8%) per host plus 256 MiB for VHDX/filesystem overhead, rounded
down to 256 MiB. Each host must have enough current free space for full growth
and the reserve; existing files are not assumed reclaimable. Capacity previews
use two-way mirroring, a 1% pool allocation margin and then a 1% NTFS allowance.
These are estimates, not provider allocation guarantees.

The planned Storage Spaces geometry is **two-way mirror / RAID10-like**, four
members, two data copies and two columns. This does not enforce the RAID planner's
fixed mirror pairs or guarantee survival of two member failures. Future deployment
must confirm that Windows honors the requested geometry, without silent fallback.
The final mount point is unconfigured and reported as WARNING when other checks
are ready.

Layout parameter reference: [Microsoft New-VirtualDisk documentation](https://learn.microsoft.com/en-us/powershell/module/storage/new-virtualdisk).

Production eligibility reuses clean four-member simultaneous validation and binds
it to the saved plan, physical slot identities, host Volume GUIDs, filesystem and
capacity snapshot. Letter changes alone are harmless. Filesystem migration,
changed volume identities or saved plans require fresh validation. Results are
session-only, not inferred from a saved plan after restart. Missing or ambiguous
volume mappings block planning; only read-only Win32_Volume inspection was added.

All production steps are **PREVIEW ONLY — NOT EXECUTED**. There is no deployment
executor, enabled Deploy/Create button, production file creation, physical disk
formatting, pool creation, drive-letter assignment or startup registration.
Existing temporary capability/benchmark controls retain their own confirmations
and cleanup behavior.

Manual regression checks:

- With a saved plan, run the existing four-member validation: backend SUPPORTED,
  four-member checklist READY, FAT32 host filesystem BLOCKED; no extra single probe.
- Check all four host identities, filesystem/free-space values and deterministic
  `slot1.vhdx` through `slot4.vhdx` paths; no production files should exist.
- Review the size derivation, capacity chain, commands and startup notes.
- Unplug a member or change the saved plan: production validation must be BLOCKED.
- Restart: validation must require current session proof.
- At 1280×720 / 1366×768, verify the new local tabs/details remain accessible and
  all existing single-slot and sequential batch controls still work.

## Version 0.1 features

- Detect connected USB storage devices
- Display model, serial number, capacity, drive letters, and physical device path
- Display USB vendor ID and product ID when Windows exposes them
- Refresh automatically and on demand
- Keep device discovery off the GUI thread
- Perform inventory queries only; no disk-changing operations are implemented

## Version 0.2 progress

- Display BusType, MediaType, CanPool, and HealthStatus when Windows exposes them
- Detect USB storage insertion and removal through Windows device-change events
- Keep a slow reconciliation scan as a fallback for missed notifications
- Assign persistent logical slots 1–4 using layered Windows device identity
- Reserve assigned slots while their devices are disconnected
- Store slot mappings in `Documents\USBArrayManager\slots.json`
- Migrate existing AppData slot mappings automatically without deleting the old copy
- RAID, formatting, partitioning, and raw-disk operations remain out of scope

## Version 0.3 features

- Benchmark one connected, assigned slot at a time
- Measure sequential read, sequential write, initial burst write, and sustained write
- Offer an optional lightweight random 4K test
- Show progress and allow safe cancellation
- Store the latest result for each slot in
  `Documents\USBArrayManager\benchmark_results.json`
- Qualify results as `GOOD`, `SLOW WRITE`, `SLOW READ`, `UNSTABLE`, or
  `NOT TESTED`

The benchmark creates a unique 1 GiB temporary file on the mounted filesystem.
It never opens or overwrites a raw disk. The file is opened with Windows
unbuffered I/O, writes use write-through I/O, and the temporary file is deleted
after success, failure, or cancellation. At least 1.5 GiB must be free so that
512 MiB remains unused.

The first 64 MiB is reported as burst write speed. Sustained speed is measured
over the remainder of the file, after a flash drive's fast write cache may have
started to fill. The USB device's own controller cache can still influence the
results, so these numbers are intended for comparing and qualifying the array's
USB drives, not as laboratory-grade hardware measurements.

Version 0.3.1 opens the temporary test file with Windows unbuffered I/O so
sequential and random reads are measured from the USB device instead of the
Windows file cache. Write tests use write-through I/O. Results produced by the
older cached method are ignored and shown as `NOT TESTED` until rerun.

## Version 0.4 features

- Build a completely read-only RAID10 simulation from connected logical slots
- Select an even number of drives and manually define each mirror pair
- Suggest pairings that favor similar capacities and sustained write speeds
- Estimate pair and complete-array capacity, efficiency, write, conservative
  read, and theoretical maximum read performance
- Identify pair bottlenecks and warn about capacity mismatch, write-speed
  mismatch, slow drives, missing benchmarks, and disconnected slots
- Explain the performance assumptions and label every throughput value as an
  estimate rather than guaranteed RAID performance

For a mirror pair, usable capacity and sustained write use the slower or smaller
member. Conservative read uses the slower member, while theoretical maximum read
adds both members. Complete-array throughput adds the estimates of the striped
mirror pairs. Real performance may be lower because the model does not include
shared USB hub bandwidth, USB topology, controller overhead, filesystem,
workload, or the behavior of a future RAID implementation.

The planner never creates an array and never writes to a drive.

Version 0.4.1 validates every possible mirror-pair layout for four selected
drives. Suggestions maximize the sum of each pair's slower sustained write
speed. When write estimates are tied or within 5%, lower capacity waste is used
as the secondary decision. If benchmark data is missing, the planner does not
guess throughput and falls back to minimizing capacity waste. The GUI shows the
number of layouts evaluated and explains why its suggestion was selected.

Version 0.4.2 adds a read-only failure and degraded-mode simulator to the current
mirror pairing. Any selected slot can be marked as simulated failed. Each pair is
shown as `HEALTHY`, `DEGRADED`, or `FAILED`; the complete array remains available
only while every mirror pair has at least one working member. Degraded read
estimates use the surviving member's benchmark result. The simulator lists
working drives, lost redundancy, at-risk pairs, and whether another failure can
be tolerated. Simulation never changes a disk or creates a RAID array.

## Version 0.5 features

- Save one RAID10 mirror layout by persistent logical slot number in
  `Documents\USBArrayManager\raid10_plan.json`
- Reload the saved plan when the application starts and reconcile it with current
  slot reservations instead of drive letters or physical-disk numbers
- Report `READY`, `WARNING`, or `NOT READY` with explicit preflight reasons
- Treat missing mappings, disconnected or ambiguous members, unhealthy Windows
  status, missing benchmarks, and unstable members as not ready
- Warn about benchmarks older than 30 days, capacity mismatch, slow members, and
  unavailable health or capacity information
- Export a plain-text, read-only plan and readiness summary for documentation
- Preserve the existing rule that a slot reassignment removes that slot's saved
  benchmark, making the plan not ready until the new member is tested

The saved plan is only a logical-slot planning record. Saving, validating, or
exporting it never issues disk-management commands and never writes to USB media.

## Version 0.5.1 UI organization

- Reorganizes the application into Overview, Benchmarks, RAID10 Planner, Failure
  Simulator, and Logs / Details tabs
- Keeps hardware inventory and qualification concise on the Overview tab
- Moves benchmark results, timestamps, progress, cancellation, and controls into
  a dedicated Benchmarks tab without changing benchmark safety behavior
- Separates planning and failure simulation visually while retaining one shared
  RAID estimate and simulated-failure state
- Records meaningful hot-plug, reconciliation, slot, benchmark, saved-plan, and
  validation events without logging unchanged periodic scans

Version 0.5.1 changes GUI organization only. Storage discovery, slot identity,
benchmarking, RAID calculations, optimization, failure rules, persistence,
readiness validation, and all disk behavior are unchanged.

## Version 0.5.2 UI polish

- Displays saved benchmark timestamps in readable local time with the local
  timezone instead of raw ISO strings
- Shortens long identity values in the Overview table while preserving complete
  serial, storage, container, USB, and PnP identity details in the tooltip
- Makes RAID10 drive selection and pairing controls more compact and removes
  unnecessary vertical expansion
- Verifies the populated interface at 1280×720 and 1366×768 while preserving the
  shared Planner/Failure Simulator state

Version 0.5.2 is presentation-only. It does not change device discovery, slot
identity, benchmarking, RAID calculations, optimization, failure simulation,
plan persistence, readiness validation, or disk behavior.

## Version 0.5.3 UI bugfixes

- Displays the local benchmark timestamp as `YYYY-MM-DD HH:MM` without the long
  Windows timezone name; persisted ISO timestamps remain unchanged
- Keeps Failure Simulator columns stable after checkbox changes, repeated resets,
  tab switches, and planner-state refreshes
- Sizes compact failure columns to their contents and stretches the read-estimate
  column across the remaining table width using persistent Qt header policies

Version 0.5.3 changes UI rendering only. RAID calculations, benchmark execution,
failure rules, persistence, readiness validation, and disk behavior are unchanged.

## Version 0.6 backend capability and dry-run planning

- Adds a GUI-independent RAID backend interface with a read-only Windows Storage
  Spaces capability implementation
- Probes Windows disk number, removable/fixed state, BusType, MediaType,
  CanPool, partitions, filesystems, health, operational status, and boot/system
  protection fields
- Adds a Backend / Deployment tab with `SUPPORTED`, `PARTIALLY SUPPORTED`,
  `UNSUPPORTED`, or `UNKNOWN` status and explicit requirements or blockers
- Reconciles every dry-run member through the saved logical-slot plan and shows
  its current physical disk, identity, model, capacity, and future operations
- Rejects system/boot disks, duplicate physical identities, disconnected or
  ambiguous members, and saved-plan mapping mismatches
- Shows a PowerShell command outline only as text under the permanent label
  `PREVIEW ONLY — NOT EXECUTED`

Version 0.6 has no RAID execution method and no Create RAID button. Its additional
Windows queries use only read-only inventory commands. It never initializes,
clears, partitions, formats, pools, mounts, unmounts, or otherwise modifies a disk.

## Version 0.7.2 VHDX-backed Storage Spaces evaluation

- Adds `VHDX-backed Storage Spaces` through the existing `RaidBackend` interface
- Lets the user select one mounted logical USB slot for an explicit temporary probe
- Creates a uniquely named dynamic VHDX only inside
  `.usb-array-manager-vhdx-test` on that selected filesystem, with a logical
  size of 8 GiB for every filesystem, including FAT32; the capability-only
  probe does not allocate or fill the complete 8 GiB
- Attaches the temporary VHDX and reports its virtual disk identity, BusType,
  size, MediaType, CanPool, rejection reason, operational status, and health
- Always attempts detach and deletion; cleanup failures prominently report the
  exact leftover path and never continue silently
- Optionally compares direct-filesystem and VHDX-filesystem sequential read,
  sequential write, and sustained write using temporary benchmark files
- Initializes and formats only the disposable virtual disk inside the temporary
  VHDX for that optional performance test; the physical USB disk is never passed
  to initialization, partitioning, formatting, or Storage Spaces creation commands
- Blocks production planning on FAT32 because files larger than 4 GiB cannot be
  stored there, without offering or performing filesystem conversion
- Produces a four-slot mapping and future Storage Spaces requirements as
  `PREVIEW ONLY — NOT EXECUTED`; no production VHDX or real pool is created

The temporary VHDX lifecycle uses built-in Windows DiskPart, so the Hyper-V
PowerShell module is not required. PowerShell Storage cmdlets are used only to
inspect and verify the newly attached file-backed disk. Missing DiskPart,
insufficient free space, non-poolable virtual disks, and any attach, detach, or
cleanup failure are reported without modifying existing USB data.

DiskPart is invoked only with explicit temporary `/s` script files. Create and
attach share one script, detach uses a separate cleanup script, every script ends
with `exit`, and each temporary script is deleted immediately after its process
finishes. The generated commands and captured exit code/stdout/stderr are retained
in Logs / Details.

Before creating a VHDX, the probe takes a read-only disk snapshot. If the modern
Windows Storage provider itself fails (for example, `Get-Disk` returns CIM
`Invalid property`), the probe records the fully decoded provider diagnostic and
falls back to `Win32_DiskDrive` for safe before/after disk-number detection. In
that case it independently tries `Get-PhysicalDisk`, the primordial storage-pool
pipeline, and direct `MSFT_PhysicalDisk` CIM. A provider object is accepted only
when its identifiers and virtual-disk attributes safely match the verified new
disk. If none can expose a match, `CanPool` remains unknown rather than being
guessed, and the main UI directs the user to Logs / Details.

Every attached VHDX is independently correlated with a file-backed physical-disk
record before accepting `CanPool`; a disk number alone cannot override a
conflicting Storage UniqueId or bus type. Logs include each candidate's identity,
size, `CannotPoolReason`, and `OtherCannotPoolReasonDescription`. A negative
undersized or capacity-constrained probe is reported as inconclusive (`UNKNOWN`)
while preserving Windows' actual `CanPool=False` value. Other rejections apply
to the tested configuration and do not establish universal VHDX incompatibility.

On the investigated Windows 10 22H2 machine, a temporary 512 MiB expandable VHDX
on NTFS reported `Insufficient Capacity`, while an 8 GiB expandable VHDX reported
`CanPool=True`. Both were verified, detached, and deleted. This confirms
eligibility for that configuration; it does not validate a four-USB production
array or measure its performance.

DiskPart VHDX operations require the application to be started as Administrator.
The elevation preflight runs before creating the test folder or VHDX. A
non-elevated session reports `NEEDS ADMINISTRATOR`, and VHDX performance testing
stays disabled until that slot and physical-device identity complete a successful
capability probe.

Qualification defaults:

- `UNSTABLE`: write-sample coefficient of variation is at least 0.50
- `SLOW WRITE`: sustained write is below 20 MB/s
- `SLOW READ`: sequential read is below 50 MB/s
- `GOOD`: none of the conditions above apply

## Version 0.7.3 batch controls and averaged results

- Benchmarks: choose 1, 3, or 5 runs and use **Benchmark All** for every connected
  assigned slot. A confirmation lists the targets and repetitions; every run
  repeats the existing mounted-filesystem preflight and temporary-file cleanup.
- Backend / Deployment: **Probe All 4** checks the saved RAID members (or slots
  1–4 before a plan is saved). **Run Performance Test All** requires a successful
  capability probe for every current member and supports 1, 3, or 5 runs.
- All jobs run sequentially. A worker must finish its complete cleanup before
  another slot or run starts. Conflicting single-slot and batch controls are
  disabled while a storage job is active.
- Benchmark means become the current saved result; the table shows the run count.
  Optional `run_count` and `individual_runs` fields preserve the existing JSON
  schema and measurement method. Older unbuffered results load as one run; older
  cached results remain ignored as before. Qualification uses the existing rule
  on averaged read, sustained write, and write-stability measurements.
- A compact performance table shows mean direct/VHDX read, write, sustained-write
  speeds, and the arithmetic mean of each run's overhead percentage. VHDX results
  remain in the current session, with every individual run in Logs / Details.
- A failed slot is reported and remaining slots continue when cleanup succeeds.
  Incomplete slots retain their previous benchmark; partial repetitions are logged
  but never saved as a complete requested-run average. Cleanup failures stop the
  batch. Cancel and window close finish the current batch run and cleanup first.
- Backend / Deployment uses a horizontal workspace: probe controls/details on
  the left; batch results and tabbed requirements, disk mapping, and preview on
  the right. Direct, VHDX, and overhead columns share available table space.
  Splitters resize each area; only individual detail views scroll when needed.
  Physical-disk operations,
  benchmark measurement formulas, eligibility, RAID planning, and dry-run behavior
  keep their existing implementation.

## Version 0.7.4 simultaneous four-member capability validation

Use **Validate 4 VHDXs Together** in Backend / Deployment to test all four
assigned RAID members as one temporary attachment set. After confirming the
listed members, the application preflights all four identities, mounted writable
filesystems, free space, and system/boot-disk exclusions before creating files.
It creates one unique 8 GiB expandable VHDX per member inside a separate
invocation-owned directory under `.usb-array-manager-vhdx-test`.

Creation and attachment commands still run one at a time. All four VHDXs then
remain attached during a shared Storage Spaces capability snapshot. The existing
DiskPart lifecycle, inventory fallback, and Storage Spaces matching helpers are
reused. Read-only `Get-DiskImage` links each exact file path to its attached disk;
file-backed type, virtual size, disk identity, UniqueId, DeviceId/ObjectId and
virtual model are checked where exposed. All four image links are checked again
after the capability queries. Ambiguous or duplicate mappings are rejected.
Shared JSON arrays are explicitly enumerated for Windows PowerShell 5.1 so
physical disks remain separate records. Malformed serialization is diagnosed as
a provider-query failure; it cannot masquerade as one successful disk record.

The **Four-member validation** result tab shows Slot, VHDX, Disk, Identity
verified, CanPool, CannotPoolReason and Status. Hover a row for the full path,
identity fields, failure reason and cleanup status. Overall **READY** requires
all four members verified and `CanPool=True` during the shared attachment window,
followed by successful cleanup. A negative, missing or ambiguous result reports
PARTIAL/FAILED and a per-slot reason. Production deployment remains a separate
assessment: FAT32 does not alone block this small expandable temporary test, but
still blocks production VHDXs larger than its single-file limit.

A `finally` cleanup attempts every owned member independently, even after a
creation or attachment failure. Ownership is tracked through the unique private
directory and file identity. Cleanup detaches by the temporary file path and
deletes only that invocation's file; a replaced file is preserved. Detach failures
keep the file for recovery. Any leftover or potentially attached member is listed
explicitly and further VHDX testing in that session is blocked until the issue is
resolved and the app restarted. Window close requests a stop between stages and
waits for cleanup; it never terminates the worker.

This capability test creates no pool, partitions, filesystem or production VHDX,
and performs no benchmarks. Existing Benchmark All, Probe All 4, and Performance
Test All remain sequential. Full diagnostics and cleanup outcomes appear in
Logs / Details.

## Version 0.7.4.1 validation-state bugfix

A successful four-member validation now satisfies the deployment-planning probe
prerequisite without a separate single-slot probe. The backend retains the latest
four-member result in the current session, tied to the physical identities used
by that test. All four identities must remain current and unambiguous, all four
members must have `CanPool=True`, and cleanup must have succeeded.

A later failed validation replaces that evidence, and unexpected worker failure
clears it. A valid independent single-slot probe still satisfies the prerequisite
as before. No capability proof is persisted to disk; restarting the app does not
inherit proof from the previous session or a reloaded RAID10 plan. FAT32 production
blockers, lifecycle/CanPool checks, calculations and text-only deployment behavior
remain unchanged.

## Requirements

- Windows 10 or later
- Python 3.12
- Windows PowerShell

## Installation

```powershell
py -3.12 -m pip install -e .
```

## Run the GUI

```powershell
py -3.12 -m usb_array_manager.app
```

The original console detector is also available:

```powershell
py -3.12 -m usb_array_manager
```

## Run the tests

```powershell
py -3.12 -m unittest discover -s tests -v
```

## Safety

Inventory and slot detection are read-only. Version 0.3 writes only a temporary
benchmark file after explicit confirmation. It does not format, partition,
initialize, mount, unmount, erase, or access raw physical storage devices. Version
0.7.5 production planning only inspects, calculates and displays text. Existing
optional VHDX performance tests may initialize and format only a newly created
disposable virtual disk inside its uniquely named temporary VHDX, after explicit confirmation.
The simultaneous four-member capability test never initializes or formats even
its temporary VHDXs.
