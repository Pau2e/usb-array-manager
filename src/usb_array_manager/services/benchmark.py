from __future__ import annotations

import os
import random
import shutil
import statistics
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from usb_array_manager.models.benchmark_result import BenchmarkResult


MEBIBYTE = 1024 * 1024


class BenchmarkError(RuntimeError):
    """Raised when a filesystem benchmark cannot complete safely."""


class BenchmarkSafetyError(BenchmarkError):
    """Raised before writing when the target does not meet safety checks."""


class BenchmarkCancelled(BenchmarkError):
    """Raised when the user requests cancellation."""


@dataclass(frozen=True, slots=True)
class BenchmarkSettings:
    file_size_bytes: int = 1024 * MEBIBYTE
    block_size_bytes: int = 4 * MEBIBYTE
    sample_size_bytes: int = 64 * MEBIBYTE
    burst_size_bytes: int = 64 * MEBIBYTE
    free_space_reserve_bytes: int = 512 * MEBIBYTE
    random_4k_operations: int = 256

    def validate(self) -> None:
        values = (
            self.file_size_bytes,
            self.block_size_bytes,
            self.sample_size_bytes,
            self.burst_size_bytes,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Benchmark sizes must be positive.")
        if self.burst_size_bytes >= self.file_size_bytes:
            raise ValueError("Burst size must be smaller than the test file.")


ProgressCallback = Callable[[int, str], None]


def check_benchmark_target(root: Path, settings: BenchmarkSettings) -> int:
    settings.validate()
    if not root.exists() or not root.is_dir():
        raise BenchmarkSafetyError(f"The mounted drive is not available: {root}")

    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError as error:
        raise BenchmarkSafetyError(
            f"Could not read free space for {root}."
        ) from error

    required = settings.file_size_bytes + settings.free_space_reserve_bytes
    if free_bytes < required:
        raise BenchmarkSafetyError(
            "Not enough free space. "
            f"Required: {_gib(required):.2f} GiB; available: {_gib(free_bytes):.2f} GiB."
        )
    return free_bytes


def run_benchmark(
    root: Path,
    slot: int,
    drive_letter: str,
    *,
    settings: BenchmarkSettings | None = None,
    include_random_4k: bool = False,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> BenchmarkResult:
    active_settings = settings or BenchmarkSettings()
    check_benchmark_target(root, active_settings)
    cancel = cancel_event or threading.Event()
    report = progress or (lambda _percent, _message: None)

    temporary_directory = root / f".usb_array_manager_benchmark_{uuid.uuid4().hex}"
    temporary_file = temporary_directory / "temporary-test-file.bin"
    result: BenchmarkResult | None = None
    operation_error: Exception | None = None
    cleanup_error: OSError | None = None
    temporary_directory_created = False

    try:
        temporary_directory.mkdir(exist_ok=False)
        temporary_directory_created = True
        write_metrics = _sequential_write(
            temporary_file, active_settings, report, cancel
        )
        read_mbps = _sequential_read(
            temporary_file, active_settings, report, cancel
        )

        random_read_iops: float | None = None
        random_write_iops: float | None = None
        if include_random_4k:
            random_read_iops, random_write_iops = _random_4k(
                temporary_file, active_settings, report, cancel
            )

        qualification = qualify_result(
            read_mbps,
            write_metrics.sustained_mbps,
            write_metrics.stability_cv,
        )
        report(100, "Benchmark complete")
        result = BenchmarkResult(
            slot=slot,
            tested_at=datetime.now(timezone.utc).isoformat(),
            drive_letter=drive_letter,
            file_size_bytes=active_settings.file_size_bytes,
            sequential_read_mbps=read_mbps,
            sequential_write_mbps=write_metrics.average_mbps,
            burst_write_mbps=write_metrics.burst_mbps,
            sustained_write_mbps=write_metrics.sustained_mbps,
            write_stability_cv=write_metrics.stability_cv,
            qualification=qualification,
            random_4k_read_iops=random_read_iops,
            random_4k_write_iops=random_write_iops,
        )
    except Exception as error:
        operation_error = error
    finally:
        if temporary_directory_created:
            try:
                temporary_file.unlink(missing_ok=True)
                temporary_directory.rmdir()
            except OSError as error:
                cleanup_error = error

    if operation_error is not None:
        if cleanup_error is not None:
            raise BenchmarkError(
                f"{operation_error} Temporary file cleanup also failed: {cleanup_error}"
            ) from operation_error
        raise operation_error
    if cleanup_error is not None:
        raise BenchmarkError(
            f"Benchmark finished, but its temporary files could not be deleted: {cleanup_error}"
        ) from cleanup_error
    if result is None:
        raise BenchmarkError("Benchmark ended without a result.")
    return result


@dataclass(frozen=True, slots=True)
class _WriteMetrics:
    average_mbps: float
    burst_mbps: float
    sustained_mbps: float
    stability_cv: float


def _sequential_write(
    path: Path,
    settings: BenchmarkSettings,
    report: ProgressCallback,
    cancel: threading.Event,
) -> _WriteMetrics:
    data = os.urandom(min(settings.block_size_bytes, settings.file_size_bytes))
    total_written = 0
    sample_written = 0
    samples: list[float] = []
    start = time.perf_counter()
    sample_start = start
    burst_elapsed: float | None = None

    report(0, "Writing temporary benchmark file")
    with path.open("xb", buffering=0) as file:
        while total_written < settings.file_size_bytes:
            _raise_if_cancelled(cancel)
            amount = min(len(data), settings.file_size_bytes - total_written)
            written = file.write(data[:amount])
            if written != amount:
                raise BenchmarkError("Windows reported a partial benchmark write.")
            total_written += written
            sample_written += written

            if burst_elapsed is None and total_written >= settings.burst_size_bytes:
                file.flush()
                os.fsync(file.fileno())
                burst_elapsed = time.perf_counter() - start

            sample_complete = (
                sample_written >= settings.sample_size_bytes
                or total_written == settings.file_size_bytes
            )
            if sample_complete:
                file.flush()
                os.fsync(file.fileno())
                now = time.perf_counter()
                elapsed = max(now - sample_start, 1e-9)
                if total_written > settings.burst_size_bytes:
                    samples.append(_mbps(sample_written, elapsed))
                sample_written = 0
                sample_start = now
                percent = int((total_written / settings.file_size_bytes) * 55)
                report(percent, "Measuring sequential and sustained write speed")

    total_elapsed = max(time.perf_counter() - start, 1e-9)
    burst_elapsed = burst_elapsed or total_elapsed
    burst_bytes = min(settings.burst_size_bytes, settings.file_size_bytes)
    sustained_bytes = settings.file_size_bytes - burst_bytes
    sustained_elapsed = max(total_elapsed - burst_elapsed, 1e-9)
    sustained_mbps = _mbps(sustained_bytes, sustained_elapsed)
    stability_cv = _coefficient_of_variation(samples)
    return _WriteMetrics(
        average_mbps=_mbps(settings.file_size_bytes, total_elapsed),
        burst_mbps=_mbps(burst_bytes, burst_elapsed),
        sustained_mbps=sustained_mbps,
        stability_cv=stability_cv,
    )


def _sequential_read(
    path: Path,
    settings: BenchmarkSettings,
    report: ProgressCallback,
    cancel: threading.Event,
) -> float:
    total_read = 0
    start = time.perf_counter()
    report(55, "Reading temporary benchmark file")
    with path.open("rb", buffering=0) as file:
        while True:
            _raise_if_cancelled(cancel)
            data = file.read(settings.block_size_bytes)
            if not data:
                break
            total_read += len(data)
            percent = 55 + int((total_read / settings.file_size_bytes) * 35)
            report(min(percent, 90), "Measuring sequential read speed")

    if total_read != settings.file_size_bytes:
        raise BenchmarkError("The temporary benchmark file could not be read completely.")
    return _mbps(total_read, max(time.perf_counter() - start, 1e-9))


def _random_4k(
    path: Path,
    settings: BenchmarkSettings,
    report: ProgressCallback,
    cancel: threading.Event,
) -> tuple[float, float]:
    operation_count = settings.random_4k_operations
    if operation_count <= 0:
        return 0.0, 0.0

    block_size = 4096
    block_count = settings.file_size_bytes // block_size
    generator = random.Random(0)
    offsets = [generator.randrange(block_count) * block_size for _ in range(operation_count)]
    data = os.urandom(block_size)

    report(90, "Running optional random 4K write test")
    write_start = time.perf_counter()
    with path.open("r+b", buffering=0) as file:
        for index, offset in enumerate(offsets):
            _raise_if_cancelled(cancel)
            file.seek(offset)
            if file.write(data) != block_size:
                raise BenchmarkError("Windows reported a partial random 4K write.")
            report(90 + int(((index + 1) / operation_count) * 5), "Random 4K write")
        file.flush()
        os.fsync(file.fileno())
    write_iops = operation_count / max(time.perf_counter() - write_start, 1e-9)

    report(95, "Running optional random 4K read test")
    read_start = time.perf_counter()
    with path.open("rb", buffering=0) as file:
        for index, offset in enumerate(reversed(offsets)):
            _raise_if_cancelled(cancel)
            file.seek(offset)
            if len(file.read(block_size)) != block_size:
                raise BenchmarkError("The random 4K read was incomplete.")
            report(95 + int(((index + 1) / operation_count) * 5), "Random 4K read")
    read_iops = operation_count / max(time.perf_counter() - read_start, 1e-9)
    return read_iops, write_iops


def qualify_result(
    read_mbps: float,
    sustained_write_mbps: float,
    write_stability_cv: float,
) -> str:
    if write_stability_cv >= 0.50:
        return "UNSTABLE"
    if sustained_write_mbps < 20.0:
        return "SLOW WRITE"
    if read_mbps < 50.0:
        return "SLOW READ"
    return "GOOD"


def _raise_if_cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise BenchmarkCancelled("Benchmark cancelled by the user.")


def _mbps(byte_count: int, elapsed_seconds: float) -> float:
    return byte_count / 1_000_000 / elapsed_seconds


def _gib(byte_count: int) -> float:
    return byte_count / 1024**3


def _coefficient_of_variation(samples: list[float]) -> float:
    if len(samples) < 2:
        return 0.0
    mean = statistics.fmean(samples)
    return statistics.pstdev(samples) / mean if mean > 0 else 0.0
