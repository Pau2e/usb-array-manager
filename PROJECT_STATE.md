# USB Array Manager — Project State

## Current version

**v0.8.0**

Host Preparation interaction fix: enabled buttons previously disagreed with the
handlers' inventory-scan guard, causing enabled clicks to be silently discarded.
Accepted workers also showed no initial progress during their slow Windows query.
The page now tracks scan, external busy and active-action states separately,
snapshots selectedRows rather than stale currentRow, updates on selection/current
cell changes, and updates existing table items with signals blocked during refresh.
Accepted actions immediately show Rechecking/Recording/Reviewing text and disable
competing controls. Duplicates/invalid requests get visible reasons; completion,
failure, cancellation and worker-start failure restore state without another click.
Post-format validation has a UI wrapper with progress, cleanup of UI state and an
explicit four-PREPARED prerequisite; existing probe execution remains unchanged.
Full suite: **291 tests passed**, including ten new interaction tests using mouse,
keyboard, stale-current-index selection, scan lifecycle, blocked workers plus GUI
heartbeat, duplicate requests and success/failure/cancellation. No real disk actions
were run. Formatting service/safeguards and frozen 8192 MiB probe remain unchanged.

Dirty-filesystem destructive-format policy (2026-10-16): a fresh read-only query
confirmed physical disks Healthy/Online, Win32 status OK, for all four members.
Slots 1/2/4 have filesystem-only Warning/Full Repair Needed with both dirty flags;
Slot 3 is clean. The narrow, fully evidenced FAT32 pattern can now enter review
with status FILESYSTEM DIRTY — DESTRUCTIVE FORMAT MAY CONTINUE WITH EXTRA
ACKNOWLEDGEMENT. A separate unchecked checkbox acknowledges inconsistent filesystem,
damaged/unrecoverable data, and destruction of that filesystem. Backup and typed
confirmation remain mandatory. Unknown, conflicting or missing warning provenance,
physical health problems, identity ambiguity and protected disks still block.
Health evidence is bound to confirmation; final Windows checks enforce disk health,
volume state, uniqueness and exclusions again. Post-format NTFS must remain Healthy.
No auto repair, dirty-bit clear, formatting or production deployment was executed.
Live read-only policy check reproduced the three conditional reviews and one READY;
real slots.json and host_preparation.json bytes were verified unchanged. Frozen
VHDX probe, simultaneous validation and Storage Spaces backend remain unchanged.
Verification after the policy change: full suite **281 tests passed**; Python
compilation, focused static checks and whitespace checks passed. Synthetic Windows
guard tests prove acknowledged dirty dispatch, refusal without acknowledgement,
missing dirty evidence, physical warning, and health changes at final dispatch.

The following records describe the earlier audits before that policy revision.

Health-source audit / diagnostic visibility: the observed BLOCKED state on Slots
1, 2 and 4 is legitimate safety behavior. Get-Volume and direct MSFT_Volume return
health Warning (underlying HealthStatus=1), OperationalStatus Full Repair Needed
(53263 / 0xD00F), and Win32_Volume.DirtyBitSet / Win32_LogicalDisk.VolumeDirty true.
Slot 3 returns Healthy (0), operational OK (2), both dirty flags false. All hosts
are mounted FAT32 and query providers agree; no query errors or fallback produced
the Warning. Win32_Volume.Status itself is null for all four and is not selected.

Disk OK comes from Win32_DiskDrive.Status. Get-Disk and Get-PhysicalDisk both report
Healthy (raw 0), but the existing Find-PhysicalDisk rejects the latter's slightly
different size observations and retains the Win32 fallback. No relaxed matching
or Storage Spaces/probe change was made. Health codes and operational codes are
distinct; the app currently blocks on normalized volume health outside HEALTHY/OK,
not by remapping dirty/operational data. No media-failure conclusion was drawn.

Inventory now carries immutable JSON health-provenance packets through the existing
models. Host Preparation exposes them in Raw health / decision (read-only), with
separate changed-only logs. Missing legacy provenance is labelled unavailable;
it never creates a dirty-bit or API claim. The health acceptance condition and
formatter are unchanged. The larger JSON serialization retains nested raw values
and its encoded inventory command is tested below the Windows command-length limit.

Verification: 271 tests passed, including 10 new health-provenance tests. Live
read-only inventory reproduced raw codes, normalization and existing decisions.
No real formatting, scan/repair, dirty-bit clear, VHDX lifecycle, pool creation or
slot-identity upgrade occurred. Current slots.json bytes stayed unchanged during
the audit. Frozen VHDX probe, simultaneous validation and backend files are unchanged.

Identity-reconciliation fix (2026-10-16): the initial v0.8.0 matcher wrongly required
a nonempty storage_unique_id. This field existed but was optional in v0.7.5.3;
all four actual saved/current values are null. Saved PnP, USB parent, container,
serial and byte capacity each match one current physical member. No normalization
mismatch was the cause. Zero matches were also incorrectly labelled AMBIGUOUS.

The preparation proof now has explicit PROVEN MATCH / NO MATCH / AMBIGUOUS MATCH
results. It requires a complete saved storage-ID/PnP/serial chain or complete
USB-parent/container/PnP/serial chain, model/capacity corroboration, no missing/
conflicting saved evidence, and uniqueness across current members and reservations.
The new physical_identity.py is a pure policy over the existing StorageDevice
model; no second inventory implementation exists. Serial/instance-ID distinctions
are preserved while case, outer whitespace and valid GUID braces are normalized.

Read-only details/logging show normalized comparisons and rejection reasons;
unchanged diagnostics are not logged repeatedly. Explicit legacy-proof recording
adds versioned preparation_identity metadata only after re-query and unique proof.
Original identity/snapshots are preserved; an unavailable Storage UniqueId stays
unavailable. Diagnostic scans never save upgrades. The formatter independently
rejects an unproven physical identity even if typed confirmation data is supplied.

Actual read-only audit: all four physical matches PROVEN; Slots 1, 2 and 4 remain
BLOCKED by host-volume health Warning. Slot 3 is READY FOR PREPARATION. All remain
FAT32 and 0/4 PREPARED. Initial application-path exclusions pass on the captured
inventory, but final Windows backing-disk checks are deliberately not run by
identity diagnostics and remain required before any future format. No formatting,
conversion, initialization, VHDX operation or real saved-slot upgrade was performed.

Fix verification: complete suite **261 tests passed**, including 17 new identity
reconciliation tests. Proven 8 GiB VHDX services remain unchanged. The real
slots.json SHA256 matches the file captured at the beginning of the investigation.

Scope: Safe Host Preparation only. Backend / Deployment now has a separated Host
Preparation page with all four members, details, per-slot persistent stages,
read-only Recheck, and an explicitly confirmed FAT32 → NTFS format worker.
Backup acknowledgement is per member and unchecked; exact typed confirmation is
required. Backup verification/copy/restoration are not implemented or claimed.
There is no Format All or automatic continuation to another member.

The existing StorageDevice/HostVolume detector and SlotStore identities are reused.
Slot assignments retain preparation_reference so routine reconciliation cannot
replace the safety baseline. Preparation compares all saved strong anchors,
serial/model/capacity and current associations; conflict, ambiguity or unknown
protection blocks. The Windows adapter reruns the existing inventory inside its
process, checks protected path backing disks and exact partition/volume objects,
and formats only the verified volume CIM object. No repartitioning or disk clean
path exists. Administrator rights and a working Storage provider are required.

The worker journals FORMATTING before dispatch, then REDISCOVERING, REVALIDATING
and PREPARED only from observed results. Physical identity owns the slot; old
Volume GUIDs are historical and current NTFS GUID/capacity/free space are queried
again. Failed/unfinished work requires explicit read-only review and never triggers
automatic format retry. App UI jobs are serialized; an interprocess store lock
and Windows helper lock preserve the single-member boundary even when the helper
outlives a forcibly closed GUI. Stage records live beside slots.json in
host_preparation.json. Persistent PREPARED is checked against current inventory.

The page derives a fresh post-format size with the unchanged v0.7.5.3 formula.
All four prepared hosts are required for its next-validation action. Earlier
four-member proof is invalidated before preparation/recheck. Successful subsequent
validation records timestamp/generation/new host snapshots for audit only; fresh
session evidence remains mandatory. Host changes invalidate session proof even
if later reverted. The existing legacy capability controls remain available.

Frozen: 8192 MiB temporary dynamic probes, VHDX identity/CanPool/cleanup services,
four-member temporary lifecycle, benchmark/sequential averaging and shared planner.
Production creation, pool/mirror/final-volume creation and startup persistence are
NOT IMPLEMENTED. Existing production command text remains PREVIEW ONLY.

Implementation checks use fake formatting/inventory, synthetic Windows PowerShell
storage functions and UI tests. No live physical formatting, VHDX probe, pool or
startup configuration was executed. Actual host migration/hardware acceptance
is not yet verified. See README for the user workflow and limits.

Verification on 2026-10-16: complete unittest suite **244 tests passed** (the 216
baseline tests plus 28 host-preparation tests). Compilation, selected unused/
undefined-code checks and git whitespace checks passed. Runtime and installed
editable package metadata both report 0.8.0. Host Preparation was visually checked
with synthetic members at 1280×720 and 1366×768. The vhdx_probe.py,
vhdx_together.py and raid_backend.py implementations have no changes from baseline.

## Historical v0.7.5.3 checkpoint

Version 0.7 milestone closed on 2026-10-13 at the user's request. This checkpoint
includes all completed changes through v0.7.5.3. Production deployment remains
PREVIEW ONLY; no v0.8 work or production/destructive action has been enabled.

Production sizing is now conservative: reserve max(2 GiB, 8% of the smallest
usable host), subtract an additional 256 MiB VHDX/filesystem allowance, and
round logical capacity DOWN to 256 MiB. For ~28.64 GiB this calculates 26 GiB
per member, not a hard-coded size. The same reserve/overhead feeds existing
full-growth free-space checks. Capacity estimates reuse the calculated logical
size; mirror/layout assumptions and formulas are unchanged. Pre-format warnings,
post-format recalculation and migration command placeholders remain mandatory.

Overview and Benchmarks are separate full-width tabs again. Existing panel/model
instances, aliases, selection, button and batch signals are reused; no duplicate
results or slot state. The merged-page wrapper and its column reordering were
removed. Planner/failure tabs still use dynamic insertion before Backend /
Deployment and share one workspace. Runtime, project and installed package
metadata report 0.7.5.3. Regression tests were updated for separate tabs and
extended for switch persistence, sizing/overhead, rounding and representative
small/large capacities. No production/destructive execution or v0.8 work exists.

Preview/readiness presentation cleanup only. The old VHDX deployment-preview
generator (which stopped at FAT32 blockers and used letter-based mapping) was
removed; backend Deployment preview now reuses the production planner's canonical
preview. Current letters are display-only, Volume GUIDs authoritative, and file
paths deterministic. Blocked plans show the complete future procedure under
PREVIEW ONLY — BLOCKED — DO NOT EXECUTE. For filesystem migration, command
templates use revalidated Volume GUID/recalculated sizing placeholders. Both
preview and readiness details label current sizing as a pre-format estimate and
require fresh capacity/free-space/reserve/VHDX sizing after formatting and user
data restoration. Sizing formulas, validation state, probe/benchmark lifecycle,
CanPool and RAID logic remain unchanged. Six regression tests cover the shared
blocked preview, GUID identity, no carried-forward sizing, fresh measurements,
read-only UI and no storage-command execution. The earlier summary cleanup remains:

Presentation cleanup: one backend capability label, production summary based on
the existing active filesystem/saved-plan blocker categories, and separated
per-slot operation headings. Validation, sizing, saved plans, RAID calculations
and deployment execution behavior are unchanged.

With explicit user approval, temporary performance-test preparation now formats
the already verified temporary VHDX partition before assigning its drive letter.
Previously New-Partition assigned the letter while the volume was still RAW,
allowing Explorer to show a format prompt. Physical USB letters/partitions,
capability-only probing, CanPool and shared detach/delete cleanup are unchanged.
No live physical/VHDX performance test was run during this fix; prompt suppression
needs user verification. Regression tests capture the generated preparation script
and assert identity guards, stop-on-error, format-before-letter ordering and letter
re-query. The v0.7.5 production planner remains in place:

Adds preview-only production readiness and layout planning in
`services/production_planning.py`, presented by a dedicated
`ui/production_readiness.py` tab. Existing physical safety/member selection and
four-member proof are reused; production evidence additionally binds the saved
plan, Volume GUIDs, filesystems and capacities captured at validation start.
No evidence survives restart. Recomputed readiness separates backend SUPPORTED
from production BLOCKED (including FAT32), reports host free space and derives
equal dynamic VHDX sizes from the smallest usable host with explicit reserves.
Read-only Win32_Volume inventory supplies GUID/filesystem/capacity/free-space
facts. Missing/ambiguous/mismatching volume associations fail closed.

The future layout is two-way mirror / RAID10-like, two data copies/two columns,
not fixed RAID10 pairs; one member failure is guaranteed tolerable, two are not.
Capacity estimates and conditional DiskPart/PowerShell/startup instructions are
TEXT ONLY. Final mount point remains unconfigured (WARNING after other checks
pass). No production storage executor or enabled deployment button exists.
Temporary lifecycle, eligibility queries, RAID formulas and benchmark behavior
are unchanged. The previous 0.7.4.1 bugfix remains in place:

Fixes the deployment-planning prerequisite state: a clean, identity-verified 4/4
simultaneous CanPool success now counts as sufficient capability evidence. The
latest four-member result is retained separately in VhdxStorageSpacesBackend for
the current session and is bound to the test's physical-slot identities. A later
failed result, worker error, or mismatching current identity invalidates that
evidence. Independent single-slot probe behavior is preserved. No capability
state is persisted or inferred from saved plans on restart. All four FAT32
production blockers remain unchanged; lifecycle, CanPool queries, calculations,
and deployment behavior are unchanged by this bugfix.

The v0.7.4 capability workflow remains in place:

Adds Validate 4 VHDXs Together: a separate temporary capability workflow in
`services/vhdx_together.py` with a dedicated Qt worker. It preflights all four
members before creating one uniquely owned 8 GiB expandable VHDX per USB, attaches
all four, validates image-path/disk/Storage Spaces identities and CanPool from a
shared snapshot, rechecks all image links, then detaches/deletes every owned file
in `finally`. Creation/attachment commands are sequential; only the attachment
set is simultaneous. It never benchmarks, initializes, partitions, formats or
creates a pool. Failure stages and per-member cleanup are logged; residual files
or potentially attached members block further VHDX operations in the session.
The compact result table is in the Four-member validation result tab. READY
requires verified simultaneous eligibility and successful cleanup. FAT32 remains
separately blocked for production deployment. The user's initial 2026-10-13 log confirms
all four VHDXs attached simultaneously, their image paths verified, and cleanup
succeeded. A Windows PowerShell 5.1 JSON-array wrapper bug made the shared
physical-disk rows appear as one `{value, Count}` object, producing UNKNOWN
CanPool results. The query now explicitly enumerates decoded array members.
Malformed records are logged as query failures instead of misleading READY
objects. A regression test runs the actual Windows PowerShell serializer on
synthetic four-disk data; other tests use fake Windows providers and tiny fixture
files in temporary directories. The user subsequently confirmed the corrected
live test with a screenshot showing 4/4 members READY simultaneously, all four
identities verified, CanPool=True, and all temporary VHDXs detached/deleted.

The v0.7.3 batch behavior remains in place:

Adds sequential Benchmark All, Probe All 4, and Run Performance Test All controls.
The existing single-run workers/backend functions perform every run and its
cleanup. Benchmarks and VHDX performance support 1/3/5 repetitions; arithmetic
means are calculated separately in `services/batch_results.py`. Benchmark means
are persisted with optional run-count/individual-run fields in the existing
schema. VHDX mean results are shown in a compact session table and individual
runs are logged. Jobs share one UI busy boundary. Cancellation and window close
finish the current batch run and cleanup; cleanup failure stops the batch.
Backend / Deployment uses side-by-side probe and results/deployment areas, with
balanced performance columns and local detail scrolling instead of a whole-page
vertical scrollbar. Requirements, disk mapping, and deployment preview are
available in the right-hand tabbed detail area.

The v0.7.2 capability fixes below remain in place:

The Probe VHDX Backend button now always uses an 8 GiB expandable disk on the
selected filesystem, including FAT32, mixed, or unknown filesystems. It does
not fall back to 512 MiB based on the filesystem. The capability-only probe
inspects the blank virtual disk without allocating/filling its logical capacity.
The existing free-space preflight still requires 8 GiB plus a 512 MiB reserve.
Capability is accepted only from an
independently matched file-backed physical-disk record. The UI and logs expose
disk size and Windows rejection details. A negative undersized or
capacity-constrained probe is inconclusive, rather than evidence that every
VHDX-backed backend is unsupported.

The affected Windows 10 22H2 Storage Management path was repaired by restoring
the missing installed `mispace.mof` registration/classes after backing up WMI.
Live NTFS tests then returned `CanPool=False / Insufficient Capacity` for a
512 MiB expandable VHDX and `CanPool=True` for an 8 GiB expandable VHDX. Both were
detached/deleted, and disk, partition, and filesystem inventories were unchanged.
No actual pool or production array has been created.

The current work adds an explicitly initiated, temporary VHDX-backed Storage
Spaces capability and performance probe. Production deployment remains dry-run
only and physical USB disk structure and existing files are never modified.

## Project purpose

USB Array Manager is a Windows desktop portfolio application for inventorying and
qualifying USB flash drives that may eventually be used in a redundant storage
array. It currently detects USB storage, assigns persistent logical slots,
benchmarks mounted filesystems safely, and simulates RAID10 layouts and failures.

It does **not** create or manage a real RAID array. The current emphasis is on
safe observation, repeatable device identity, measurement, and planning.

## Supported environment and dependencies

- Windows 10 or later
- Python 3.12 or later
- PySide6 `>=6.7,<7`
- Windows PowerShell (`powershell.exe`) and built-in CIM/Storage/PnP commands
- Python standard library, including `ctypes` for the limited Windows file-I/O
  wrapper used by benchmarks

There is no third-party WMI package and no RAID library.

Install and run:

```powershell
py -3.12 -m pip install -e .
py -3.12 -m usb_array_manager.app
```

Run the console inventory with `py -3.12 -m usb_array_manager`. Run tests with:

```powershell
py -3.12 -m unittest discover -s tests -v
```

## Architecture and folder structure

```text
usb-array-manager/
├── pyproject.toml
├── README.md
├── PROJECT_STATE.md
├── src/usb_array_manager/
│   ├── app.py                         # PySide6 application entry point
│   ├── __main__.py                    # Console inventory entry point
│   ├── models/
│   │   ├── storage_device.py          # Normalized device record
│   │   ├── benchmark_result.py        # Persisted benchmark result
│   │   ├── raid_plan.py               # Planner estimates and suggestion models
│   │   ├── raid_failure.py            # Failure-simulation result models
│   │   ├── raid_backend.py            # Backend capability/dry-run models
│   │   ├── vhdx_probe.py              # Temporary VHDX probe/result models
│   │   └── saved_raid10_plan.py        # Durable plan and readiness models
│   ├── services/
│   │   ├── windows_cim.py             # Read-only Windows device discovery
│   │   ├── device_monitor.py          # WM_DEVICECHANGE event filter
│   │   ├── slot_store.py              # Persistent slot identity and JSON store
│   │   ├── benchmark.py               # Safe filesystem benchmark orchestration
│   │   ├── windows_unbuffered_io.py   # Windows unbuffered/write-through file I/O
│   │   ├── benchmark_store.py         # Latest result per slot in JSON
│   │   ├── batch_results.py           # Pure arithmetic means of completed runs
│   │   ├── raid10_planner.py          # Pairing search, formulas, and warnings
│   │   ├── raid10_failure_simulator.py# Pure failure/degraded-mode simulation
│   │   ├── raid10_plan_store.py        # Atomic, versioned logical-slot plan
│   │   ├── raid10_readiness.py         # Strict read-only preflight rules
│   │   ├── raid_backend.py             # Backend capability and dry-run checks
│   │   └── vhdx_probe.py               # Restricted temporary VHDX workflow
│   └── ui/
│       ├── main_window.py             # Inventory, slots, benchmark workflow
│       ├── device_table_model.py      # Inventory/benchmark table model
│       ├── device_tabs.py             # Separate Overview, Benchmarks, and other tab layouts
│       └── raid10_planner_dialog.py   # Planner and failure simulator GUI
└── tests/                             # unittest coverage for every service/UI seam
```

The design separates immutable data models, platform/service logic, and PySide6
UI. Device discovery, benchmarks, and temporary VHDX probes run in `QThread`
workers so the GUI remains responsive. RAID calculations and failure simulations
are pure Python services and do not touch disks.

## Implemented features

### v0.1 — USB inventory

- Detects connected USB storage through `Win32_DiskDrive` and associated
  partition/logical-disk CIM records.
- Displays model, serial, capacity, drive letters, physical device path, VID/PID,
  and connection state.
- Provides console and GUI inventory views.

### v0.2 — Storage details, monitoring, and logical slots

- Adds BusType, MediaType, CanPool, and HealthStatus where Windows exposes them.
- Listens for `WM_DEVICECHANGE` arrival/removal/device-tree changes, debounced by
  500 ms, then rescans inventory.
- Runs a 30-second reconciliation scan as a fallback for missed Windows events.
- Supports persistent Slot 1 through Slot 4 assignments.
- Prevents two devices from reserving the same slot.
- Keeps a reserved, disconnected row visible and recognizes the device again
  after reconnection.

### v0.3 / v0.3.1 — Benchmark and qualification

- Benchmarks one connected, assigned, mounted slot at a time.
- Measures sequential read, average sequential write, initial burst write,
  sustained write, and write stability.
- Offers an optional lightweight random 4K read/write test.
- Shows progress, supports cancellation, and cleans up after success, failure,
  or cancellation.
- Stores the latest valid result per slot and displays `GOOD`, `SLOW WRITE`,
  `SLOW READ`, `UNSTABLE`, or `NOT TESTED`.
- Measurement method version 2 uses unbuffered reads and write-through writes;
  older cached results are deliberately ignored.

### v0.4 / v0.4.1 — Read-only RAID10 planner

- Selects an even number of connected logical slots and permits manual mirror
  pair assignment.
- Calculates per-pair and whole-array capacity/performance estimates.
- Warns about capacity mismatch, speed mismatch, slow bottlenecks, disconnected
  devices, and missing benchmarks.
- Exhaustively evaluates valid mirror layouts and explains the suggested layout.
- Labels performance as estimates, not guaranteed RAID performance.

### v0.4.2 — Failure/degraded-mode simulator

- Marks any selected member as `Simulated failed`; this changes simulation state
  only.
- Shows `HEALTHY`, `DEGRADED`, or `FAILED` per mirror pair and for the array.
- Explains availability, remaining members, lost redundancy, at-risk pairs, and
  whether another failure can be tolerated.
- Estimates healthy/degraded read performance from saved benchmark data.
- Includes `Reset simulated failures`.
- Planner GUI is horizontally split: sections 1–3 on the left and section 4,
  warnings, and assumptions on the right. Native minimize, maximize/restore, and
  close title-bar buttons remain enabled.

### v0.5 — Persistent plan and readiness validator

- Saves one versioned RAID10 mirror layout atomically as logical slot numbers.
- Reloads the saved layout and reconciles it with current slot reservations.
- Reports `READY`, `WARNING`, or `NOT READY` with explicit preflight reasons.
- Uses a 30-day benchmark freshness warning and relies on slot reassignment to
  invalidate benchmark results for a changed member.
- Detects missing, disconnected, ambiguous, unhealthy, unbenchmarked, stale,
  slow, unstable, and capacity-mismatched members.
- Exports a plain-text plan/readiness summary for project documentation.

### v0.5.1 — Tabbed UI organization

- Separates Overview, Benchmarks, RAID10 Planner, Failure Simulator, and Logs /
  Details into five top-level tabs.
- Keeps planner and failure pages connected to one shared current estimate and
  simulated-failure state.
- Adds focused overview and benchmark table models while preserving the original
  combined model for compatibility.
- Records meaningful hot-plug, reconciliation, slot, benchmark, plan, and
  validation events without logging unchanged periodic scans.
- Keeps the window usable at a 1050 × 650 minimum size and preserves all v0.5
  service, safety, persistence, readiness, and calculation behavior.

### v0.5.2 — Final UI polish

- Formats benchmark timestamps as readable local date/time values with timezone.
- Shortens identity text in the Overview table and exposes complete identity
  fields through tooltips.
- Compacts RAID10 drive selection and pairing controls to remove unused vertical
  space.
- Verifies populated layouts at 1280 × 720 and 1366 × 768.
- Preserves the single shared Planner/Failure Simulator plan and failure state.

### v0.5.3 — Timestamp and simulator sizing fixes

- Renders benchmark timestamps as local `YYYY-MM-DD HH:MM` values without
  changing persisted ISO timestamps.
- Uses stable `ResizeToContents` policies for compact Failure Simulator columns
  and `Stretch` for the final read-estimate column.
- Reapplies the shared header policy whenever simulation rows are refreshed so
  repeated resets, checkbox changes, tab switches, and planner refreshes cannot
  collapse the table toward the left.

### v0.6 — Backend capability and dry-run planning

- Adds a GUI-independent `RaidBackend` abstraction and a read-only Windows
  Storage Spaces capability implementation.
- Extends inventory with disk number, removable/fixed state, boot/system flags,
  read-only/offline state, partition count/style, filesystems, and operational
  status using read-only Windows queries.
- Adds a Backend / Deployment tab showing status, explanations, requirements,
  blockers, physical mappings, and future operations for the saved plan.
- Produces a PowerShell outline only as text labeled `PREVIEW ONLY — NOT
  EXECUTED`.
- Blocks system/boot disks, duplicate physical identities, disconnected or
  ambiguous members, and saved-plan mapping mismatches.
- Deliberately provides no execution API and no Create RAID control.

### v0.7.1 — VHDX-backed Storage Spaces evaluation

- Adds a second Windows `RaidBackend` implementation for VHDX-backed members.
- Probes one selected mounted USB slot using a unique temporary VHDX file.
- Performs an Administrator-token preflight before creating any probe folder or
  file and reports `NEEDS ADMINISTRATOR` when elevation is required.
- Falls back to read-only `Win32_DiskDrive` snapshots when the Windows Storage
  CIM provider cannot enumerate disks, then independently tries
  `Get-PhysicalDisk`, the primordial-pool pipeline, and direct
  `MSFT_PhysicalDisk` CIM for `CanPool`; unmatched capability remains unknown.
- Queries the attached virtual disk's identity, BusType, MediaType, CanPool,
  operational status, health, and Storage Spaces eligibility.
- Guarantees detach/delete attempts through cleanup-finally handling and reports
  any leftover path prominently.
- Optionally benchmarks direct USB and the temporary VHDX filesystem and reports
  read/write/sustained-write overhead.
- Initializes and formats only the disposable virtual disk for the optional
  performance test; no physical disk number is accepted for that workflow.
- Blocks FAT32 production plans without formatting or converting the USB drive.
- Generates a four-member production mapping as preview text only; it creates no
  production VHDX files, pool, or virtual disk.

## Important design decisions

1. **No drive-letter or PHYSICALDRIVE identity.** Both can change after reboot or
   reconnection and are display/current-access properties only.
2. **Layered identity with unambiguous matching.** A field is accepted only when
   its value maps to exactly one unmatched connected device and one unmatched
   saved slot. This avoids silently assigning identical devices to the wrong slot.
3. **Serial number is evidence, not authority.** It is only part of a composite
   fallback fingerprint because USB firmware may omit, clone, or alter it.
4. **Atomic local persistence.** JSON is written to a temporary file, flushed and
   `fsync`ed, then replaced atomically. Invalid JSON is reported rather than
   silently overwritten.
5. **Event plus reconciliation model.** Windows device events provide normal fast
   updates; periodic scans recover from missed or coalesced events.
6. **No raw-disk benchmarking.** Benchmarks operate only on a uniquely named file
   inside a mounted filesystem and require explicit confirmation.
7. **Pure planning services.** RAID formulas, pairing search, and failure rules
   consume models and return models; they contain no disk-management calls.
8. **Unknown data stays unknown.** Array totals that require missing capacity or
   benchmark values are reported unavailable rather than guessed.
9. **Production backend planning has no executor.** Backend assessment consumes
   immutable inventory data and returns immutable display models. The v0.7.1
   executor is restricted to one uniquely named temporary VHDX and cannot create
   production members or a Storage Spaces pool.

## Persistent slot identification strategy

Slot mappings are stored at:

```text
%USERPROFILE%\Documents\USBArrayManager\slots.json
```

An older `%LOCALAPPDATA%\USBArrayManager\slots.json` is copied to the visible
Documents location when needed; the legacy copy is not deleted.

`SlotStore` normalizes identity text and attempts matches in this order:

1. `storage_unique_id`, prefixed with its Windows `UniqueIdFormat`
2. PnP `container_id`
3. parent `usb_device_id`
4. disk `pnp_device_id`
5. SHA-256 composite fingerprint of model, capacity, VID, PID, and serial
6. PnP `location_paths`

At every layer, a match is used only when unique on both sides. Already matched
devices and slots are removed before the next fallback. Saved records also retain
a last-known device snapshot so a disconnected reserved slot can still be shown.

This strategy tolerates drive-letter and `PHYSICALDRIVE` renumbering and usually
handles duplicate serials via stronger Windows identities. It cannot guarantee a
stable identity when a cheap device exposes no unique ID and multiple units have
the same model/capacity/VID/PID/serial. The final location-path fallback can also
change if the device is moved to another USB port or hub topology.

## Local persisted data

- Slots: `%USERPROFILE%\Documents\USBArrayManager\slots.json`
- Benchmarks: `%USERPROFILE%\Documents\USBArrayManager\benchmark_results.json`
- RAID10 plan: `%USERPROFILE%\Documents\USBArrayManager\raid10_plan.json`

Benchmark results are associated with logical slot numbers. Reassigning or
unassigning a slot removes affected saved benchmark results to prevent results
from following the wrong physical device.

## Benchmark methodology

Default settings:

- Temporary file: 1 GiB
- Sequential transfer block: 4 MiB
- Write sample window: 64 MiB
- Burst region: first 64 MiB
- Free-space reserve: 512 MiB, so at least 1.5 GiB must be free
- Optional random test: 256 deterministic 4 KiB operations

Workflow:

1. Confirm the drive has a mounted root and enough free space.
2. Confirm all transfer sizes align with the filesystem sector size.
3. Create a unique `.usb_array_manager_benchmark_<uuid>` directory and test file.
4. Sequentially write the file using Windows unbuffered, write-through I/O.
5. Report burst speed from the first 64 MiB, average write from the whole file,
   and sustained speed from the remainder after the burst region.
6. Calculate write stability as the coefficient of variation of 64 MiB write
   samples after the burst region.
7. Read the file sequentially with unbuffered I/O to avoid a false result from the
   Windows file cache.
8. If selected, run 256 random 4K writes and reads at reproducible offsets and
   report IOPS.
9. Delete the temporary file and directory in `finally`, including cancellation
   and error paths. Cleanup failure is surfaced as an error.

Speeds use decimal MB/s (`bytes / 1,000,000 / seconds`). Qualification is applied
in this priority order:

- `UNSTABLE`: write-sample coefficient of variation `>= 0.50`
- `SLOW WRITE`: sustained write `< 20 MB/s`
- `SLOW READ`: sequential read `< 50 MB/s`
- `GOOD`: none of the above

Burst speed represents the fast initial region, which may benefit from a flash
device's SLC/controller cache. Sustained speed excludes that first region and is
more useful for comparing longer writes, but a 1 GiB test can still be too short
to exhaust large device caches. Results are qualification estimates, not
laboratory measurements.

## RAID10 planner formulas and assumptions

For mirror pair members `a` and `b`:

- Usable capacity: `min(capacity(a), capacity(b))`
- Estimated sustained write: `min(sustained_write(a), sustained_write(b))`
- Conservative read: `min(sequential_read(a), sequential_read(b))`
- Theoretical maximum read: `sequential_read(a) + sequential_read(b)`
- Capacity waste: `abs(capacity(a) - capacity(b))`

For the complete array of striped mirror pairs:

- Total raw capacity: sum of every selected member capacity
- Total usable capacity: sum of pair usable capacities
- Capacity efficiency: `total_usable / total_raw * 100`
- Estimated sustained write: sum of pair estimated sustained writes
- Conservative read: sum of pair conservative reads
- Theoretical maximum read: sum of pair theoretical maximum reads

If any required input for a total is unknown, that total is unavailable. Writes
must reach both members of a mirror, so the slower member limits a pair. Reads
may be distributed across mirror members, but that depends on a future RAID
implementation. The planner assumes mirror pairs can be striped in parallel.

These formulas do not cap results for shared USB hub bandwidth and do not model
USB topology, controller overhead, filesystem, workload, queue depth, thermal
throttling, or real RAID implementation behavior.

## Pairing optimization logic

`suggest_mirror_pairing()` recursively enumerates every perfect matching of the
selected drives. Four drives have exactly three valid mirror-pair layouts.

For each candidate:

- Write score = sum of the slower sustained-write result in each pair.
- Capacity waste = sum of the absolute capacity difference inside each pair.

Selection rules:

1. With complete sustained-write data, find the maximum write score.
2. Treat candidates within `max(1.0 MB/s, 5% of the best score)` as near-tied.
3. Among near-tied candidates, choose the least capacity waste.
4. Then prefer the higher write score and finally a deterministic slot-number
   signature.
5. If any selected drive lacks sustained-write data, do not guess throughput;
   minimize capacity waste and use the slot signature as the deterministic tie
   breaker.

The GUI reports how many layouts were evaluated and whether the result was the
highest write estimate, a near-best lower-waste layout, or a capacity-only
fallback caused by missing benchmarks.

## Failure and degraded-mode simulation rules

For each mirror pair:

- `HEALTHY`: no simulated failures; two working members.
- `DEGRADED`: exactly one failed member; the surviving member provides data but
  the pair has no redundancy.
- `FAILED`: both members failed; that pair's striped data has no surviving copy.

For the complete RAID10 array:

- `HEALTHY` when every pair is healthy.
- `DEGRADED — data still available` when one or more pairs are degraded and no
  pair has failed.
- `FAILED — data unavailable` as soon as any one mirror pair loses both members.

Healthy-pair read estimates use the planner's conservative minimum and
theoretical sum. A degraded pair uses the surviving member's sequential-read
result for both conservative and maximum read. Complete degraded read estimates
sum the available pair estimates. If a pair has failed, array read estimates are
unavailable.

Failure tolerance text is intentionally conditional: after one member fails,
another failure is safe only if it occurs in a still-healthy pair. If every pair
is degraded, any additional failure causes array failure.

## Safety rules

- Never format, partition, initialize, erase, mount, unmount, or alter a disk.
- Never open a raw physical disk for benchmark writes.
- Never implement real RAID or rebuild operations without a new, explicit user
  request and a separate safety design/review.
- Device inventory must remain read-only.
- Planner and failure controls must remain simulation-only and clearly labeled.
- Require explicit confirmation immediately before every write benchmark.
- Require a mounted filesystem, sufficient free space, and aligned transfers.
- Always attempt temporary benchmark cleanup on success, cancellation, or error.
- Do not guess missing identities, capacities, or performance results.
- Preserve and debug existing working modules rather than rewriting the project.

## What is still read-only

The following never modify USB-drive contents:

- USB detection and inventory queries
- Bus/media/health metadata collection
- Connection monitoring and reconciliation scans
- Logical slot matching and disconnected-slot representation
- RAID10 capacity/performance planning
- Pairing optimization
- Failure/degraded-mode simulation

Slot and benchmark-result stores write JSON only to the user's Documents folder,
not to USB media. The **benchmark is the sole feature that writes to a USB
filesystem**: it writes only its temporary test file after confirmation and then
deletes it. No feature writes raw sectors or changes storage configuration.

## Known limitations

- Windows-only; discovery depends on Windows PowerShell, CIM, Storage, and PnP
  provider behavior.
- The `Get-PhysicalDisk` correlation is heuristic: disk number plus size first,
  then unique serial plus size. Some USB bridges may not expose Storage fields.
- Detection is not instantaneous in every case; event handling still performs a
  full PowerShell inventory scan, with a 30-second fallback scan.
- There are exactly four logical slots.
- Weak/duplicated USB firmware identities can remain ambiguous. Location paths
  can change when a device moves ports.
- Only the latest benchmark/average per slot is stored, with individual runs for
  that average; there is no cross-batch history, trend view,
  thermal monitoring, or test-profile selection.
- Benchmark results require a mounted writable filesystem and free space. A
  sudden unplug or power loss during a benchmark is outside the application's
  control.
- A 1 GiB sustained test may not expose long-duration cache exhaustion or thermal
  throttling. The random 4K test is intentionally lightweight.
- Benchmark values belong to the current filesystem/device path and can be
  affected by other system activity.
- RAID10 plans and simulated failure selections are not persisted between app
  sessions.
- The planner allows a two-drive single mirror for experimentation but warns that
  conventional RAID10 normally uses at least four drives.
- Performance estimates ignore shared 10 Gbps hub contention and cannot predict
  the behavior of a future RAID engine.
- There is no real RAID creation, write path, parity/mirroring engine, rebuild,
  scrub, failure detection, or recovery workflow.

## Exact next recommended milestone

### Next — USB-hosted VHDX validation

Validate temporary VHDX capability reporting across more Windows editions, USB
filesystems, DiskPart behavior, and Storage cmdlet versions. Do not add production RAID execution
until the privilege model, recovery behavior, destructive confirmation flow, and
independent safety review have been explicitly specified.

## Important files and functions to inspect first

1. `src/usb_array_manager/ui/main_window.py`
   - `MainWindow.__init__()` — assembles monitoring, persistence, and actions.
   - `MainWindow.refresh_devices()` / `_show_devices()` — scan/reconcile flow.
   - `MainWindow._assign_selected_slot()` — assignment and benchmark invalidation.
   - `MainWindow._start_benchmark()` — safety check and confirmation boundary.
   - `MainWindow._open_raid10_planner()` — planner handoff.
2. `src/usb_array_manager/services/windows_cim.py`
   - `detect_usb_storage_devices()` and `_record_to_device()` — authoritative
     inventory normalization.
3. `src/usb_array_manager/services/slot_store.py`
   - `_IDENTITY_MATCH_ORDER`, `SlotStore.reconcile()`, `SlotStore.assign()`,
     `_identity_from_device()`, and `_match_assignments()`.
4. `src/usb_array_manager/services/benchmark.py`
   - `BenchmarkSettings`, `check_benchmark_target()`, `run_benchmark()`,
     `_sequential_write()`, `_sequential_read()`, `_random_4k()`, and
     `qualify_result()`.
5. `src/usb_array_manager/services/windows_unbuffered_io.py`
   - Verify Windows flags, aligned buffers, sector alignment, and cleanup before
     changing benchmark behavior.
6. `src/usb_array_manager/services/benchmark_store.py`
   - `BenchmarkStore.load_all()` deliberately accepts only measurement method 2.
7. `src/usb_array_manager/services/raid10_planner.py`
   - `estimate_raid10()`, `_estimate_pair()`, `suggest_mirror_pairing()`,
     `_all_pairings()`, and `_select_candidate()`.
8. `src/usb_array_manager/services/raid10_failure_simulator.py`
   - `simulate_failures()` and `_simulate_pair()` contain all status rules.
9. `src/usb_array_manager/ui/raid10_planner_dialog.py`
   - Current two-column planner/simulator presentation and manual pairing logic.
10. Tests to read alongside each service:
    - `tests/test_slot_store.py`
    - `tests/test_benchmark.py` and `tests/test_benchmark_store.py`
    - `tests/test_raid10_planner.py`
    - `tests/test_raid10_failure_simulator.py`
    - `tests/test_raid10_planner_dialog.py`
11. `src/usb_array_manager/services/raid_backend.py`
   - `RaidBackend` deliberately exposes assessment only; there is no execution
     method.
   - `WindowsStorageSpacesBackend.assess()` owns backend safety validation and
     dry-run generation.

Before making the next change, run the full test suite and check `git status` so
this handoff document is not mistaken for an already committed file.
