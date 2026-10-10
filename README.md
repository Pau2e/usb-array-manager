# USB Array Manager

A Windows desktop application that inventories connected USB storage devices,
maps them to persistent logical slots, and safely benchmarks mounted filesystems.

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

Qualification defaults:

- `UNSTABLE`: write-sample coefficient of variation is at least 0.50
- `SLOW WRITE`: sustained write is below 20 MB/s
- `SLOW READ`: sequential read is below 50 MB/s
- `GOOD`: none of the conditions above apply

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
initialize, mount, unmount, erase, or access raw storage devices.
