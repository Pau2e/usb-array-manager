from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from typing import Any

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.models.vhdx_probe import VhdxBenchmarkComparison
from usb_array_manager.services.benchmark import qualify_result


def average_benchmark_results(
    results: Sequence[BenchmarkResult],
) -> BenchmarkResult:
    """Return one persisted result without changing the benchmark measurements."""
    if not results:
        raise ValueError("At least one benchmark result is required.")
    first = results[0]
    if any(item.slot != first.slot for item in results):
        raise ValueError("Benchmark results from different slots cannot be averaged.")

    read = _mean(item.sequential_read_mbps for item in results)
    write = _mean(item.sequential_write_mbps for item in results)
    burst = _mean(item.burst_write_mbps for item in results)
    sustained = _mean(item.sustained_write_mbps for item in results)
    stability = _mean(item.write_stability_cv for item in results)
    random_read = _optional_mean(item.random_4k_read_iops for item in results)
    random_write = _optional_mean(item.random_4k_write_iops for item in results)
    return BenchmarkResult(
        slot=first.slot,
        tested_at=results[-1].tested_at,
        drive_letter=results[-1].drive_letter,
        file_size_bytes=results[-1].file_size_bytes,
        sequential_read_mbps=read,
        sequential_write_mbps=write,
        burst_write_mbps=burst,
        sustained_write_mbps=sustained,
        write_stability_cv=stability,
        qualification=qualify_result(read, sustained, stability),
        measurement_method_version=results[-1].measurement_method_version,
        random_4k_read_iops=random_read,
        random_4k_write_iops=random_write,
        run_count=len(results),
        individual_runs=tuple(_benchmark_snapshot(item) for item in results),
    )


def average_vhdx_comparisons(
    results: Sequence[VhdxBenchmarkComparison],
) -> VhdxBenchmarkComparison:
    if not results:
        raise ValueError("At least one VHDX performance result is required.")
    return VhdxBenchmarkComparison(
        direct_read_mbps=_mean(item.direct_read_mbps for item in results),
        direct_write_mbps=_mean(item.direct_write_mbps for item in results),
        direct_sustained_write_mbps=_mean(
            item.direct_sustained_write_mbps for item in results
        ),
        vhdx_read_mbps=_mean(item.vhdx_read_mbps for item in results),
        vhdx_write_mbps=_mean(item.vhdx_write_mbps for item in results),
        vhdx_sustained_write_mbps=_mean(
            item.vhdx_sustained_write_mbps for item in results
        ),
        averaged_overhead_percent=(
            _mean(item.read_overhead_percent for item in results),
            _mean(item.write_overhead_percent for item in results),
            _mean(item.sustained_write_overhead_percent for item in results),
        ),
    )


def _benchmark_snapshot(result: BenchmarkResult) -> dict[str, Any]:
    return {
        "tested_at": result.tested_at,
        "sequential_read_mbps": result.sequential_read_mbps,
        "sequential_write_mbps": result.sequential_write_mbps,
        "burst_write_mbps": result.burst_write_mbps,
        "sustained_write_mbps": result.sustained_write_mbps,
        "write_stability_cv": result.write_stability_cv,
        "qualification": result.qualification,
        "random_4k_read_iops": result.random_4k_read_iops,
        "random_4k_write_iops": result.random_4k_write_iops,
    }


def _mean(values: Iterable[float]) -> float:
    return statistics.fmean(values)


def _optional_mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.fmean(present) if present else None
