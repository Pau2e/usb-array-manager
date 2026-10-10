# USB Array Manager — Project State

## Current version

**v0.5.3**

The current work builds on the v0.5.2 checkpoint and applies two v0.5.3 UI-only
bugfixes. All automated tests pass.

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
│   │   └── saved_raid10_plan.py        # Durable plan and readiness models
│   ├── services/
│   │   ├── windows_cim.py             # Read-only Windows device discovery
│   │   ├── device_monitor.py          # WM_DEVICECHANGE event filter
│   │   ├── slot_store.py              # Persistent slot identity and JSON store
│   │   ├── benchmark.py               # Safe filesystem benchmark orchestration
│   │   ├── windows_unbuffered_io.py   # Windows unbuffered/write-through file I/O
│   │   ├── benchmark_store.py         # Latest result per slot in JSON
│   │   ├── raid10_planner.py          # Pairing search, formulas, and warnings
│   │   ├── raid10_failure_simulator.py# Pure failure/degraded-mode simulation
│   │   ├── raid10_plan_store.py        # Atomic, versioned logical-slot plan
│   │   └── raid10_readiness.py         # Strict read-only preflight rules
│   └── ui/
│       ├── main_window.py             # Inventory, slots, benchmark workflow
│       ├── device_table_model.py      # Inventory/benchmark table model
│       ├── device_tabs.py             # Overview, Benchmarks, and Logs tabs
│       └── raid10_planner_dialog.py   # Planner and failure simulator GUI
└── tests/                             # unittest coverage for every service/UI seam
```

The design separates immutable data models, platform/service logic, and PySide6
UI. Device discovery and benchmarks run in `QThread` workers so the GUI remains
responsive. RAID calculations and failure simulations are pure Python services
and do not touch disks.

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
- Only the latest benchmark per slot is stored; there is no history, trend view,
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

### v0.6 — Benchmark history and comparison

Keep USB operations within the existing temporary-file safety boundary. Preserve
multiple benchmark runs per logical slot, show trends and test conditions, and
make readiness use the latest compatible result. Do not begin real RAID creation
until the Windows storage implementation, privilege model, recovery behavior,
and destructive-action confirmation design have been separately specified and
reviewed.

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

Before making the next change, run the full test suite and check `git status` so
this handoff document is not mistaken for an already committed file.
