from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    slot: int
    tested_at: str
    drive_letter: str
    file_size_bytes: int
    sequential_read_mbps: float
    sequential_write_mbps: float
    burst_write_mbps: float
    sustained_write_mbps: float
    write_stability_cv: float
    qualification: str
    measurement_method_version: int = 2
    random_4k_read_iops: float | None = None
    random_4k_write_iops: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkResult:
        return cls(
            slot=int(data["slot"]),
            tested_at=str(data["tested_at"]),
            drive_letter=str(data["drive_letter"]),
            file_size_bytes=int(data["file_size_bytes"]),
            sequential_read_mbps=float(data["sequential_read_mbps"]),
            sequential_write_mbps=float(data["sequential_write_mbps"]),
            burst_write_mbps=float(data["burst_write_mbps"]),
            sustained_write_mbps=float(data["sustained_write_mbps"]),
            write_stability_cv=float(data["write_stability_cv"]),
            qualification=str(data["qualification"]),
            measurement_method_version=int(
                data.get("measurement_method_version", 1)
            ),
            random_4k_read_iops=(
                float(data["random_4k_read_iops"])
                if data.get("random_4k_read_iops") is not None
                else None
            ),
            random_4k_write_iops=(
                float(data["random_4k_write_iops"])
                if data.get("random_4k_write_iops") is not None
                else None
            ),
        )
