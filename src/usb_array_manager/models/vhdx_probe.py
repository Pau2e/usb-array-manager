from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VhdxDiskInfo:
    disk_number: int
    identity: str
    bus_type: str
    media_type: str
    can_pool: bool | None
    operational_status: str
    health_status: str
    volume_root: str | None = None


@dataclass(frozen=True, slots=True)
class VhdxBenchmarkComparison:
    direct_read_mbps: float
    direct_write_mbps: float
    direct_sustained_write_mbps: float
    vhdx_read_mbps: float
    vhdx_write_mbps: float
    vhdx_sustained_write_mbps: float

    @property
    def read_overhead_percent(self) -> float:
        return _overhead(self.direct_read_mbps, self.vhdx_read_mbps)

    @property
    def write_overhead_percent(self) -> float:
        return _overhead(self.direct_write_mbps, self.vhdx_write_mbps)

    @property
    def sustained_write_overhead_percent(self) -> float:
        return _overhead(
            self.direct_sustained_write_mbps,
            self.vhdx_sustained_write_mbps,
        )


@dataclass(frozen=True, slots=True)
class VhdxProbeResult:
    slot: int
    status: str
    vhdx_path: str
    attach_status: str
    virtual_disk: VhdxDiskInfo | None
    cleanup_status: str
    filesystem: str
    explanation: str
    blockers: tuple[str, ...] = ()
    leftover_path: str | None = None
    benchmark: VhdxBenchmarkComparison | None = None
    source_identity: str = ""
    diagnostic_details: str = ""


def _overhead(direct: float, vhdx: float) -> float:
    if direct <= 0:
        return 0.0
    return ((vhdx - direct) / direct) * 100.0
