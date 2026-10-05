# USB Array Manager

A read-only Windows desktop application that detects connected USB storage
devices and displays their hardware information in a PySide6 table.

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
- Persistent logical slot mapping is not implemented yet
- RAID, formatting, partitioning, and benchmarking remain out of scope

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

Version 0.1 is intentionally read-only. It does not format, partition,
initialize, mount, unmount, erase, or write to storage devices.
