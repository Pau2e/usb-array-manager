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
