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
    size_bytes: int | None = None
    cannot_pool_reasons: tuple[str, ...] = ()
    other_cannot_pool_reason_description: str = ""
    storage_unique_id: str = ""
    size_is_exact: bool = True

    @property
    def pool_rejection_details(self) -> str:
        if self.can_pool is not False:
            return ""
        return "; ".join(
            (*self.cannot_pool_reasons, self.other_cannot_pool_reason_description)
        ).strip("; ") or "Windows did not report a reason."


@dataclass(frozen=True, slots=True)
class VhdxBenchmarkComparison:
    direct_read_mbps: float
    direct_write_mbps: float
    direct_sustained_write_mbps: float
    vhdx_read_mbps: float
    vhdx_write_mbps: float
    vhdx_sustained_write_mbps: float
    averaged_overhead_percent: tuple[float, float, float] | None = None

    @property
    def read_overhead_percent(self) -> float:
        if self.averaged_overhead_percent is not None:
            return self.averaged_overhead_percent[0]
        return _overhead(self.direct_read_mbps, self.vhdx_read_mbps)

    @property
    def write_overhead_percent(self) -> float:
        if self.averaged_overhead_percent is not None:
            return self.averaged_overhead_percent[1]
        return _overhead(self.direct_write_mbps, self.vhdx_write_mbps)

    @property
    def sustained_write_overhead_percent(self) -> float:
        if self.averaged_overhead_percent is not None:
            return self.averaged_overhead_percent[2]
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
