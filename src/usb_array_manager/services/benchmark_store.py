from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from usb_array_manager.models.benchmark_result import BenchmarkResult


SCHEMA_VERSION = 1


class BenchmarkStoreError(RuntimeError):
    """Raised when saved benchmark results cannot be read or written."""


def default_benchmark_results_path() -> Path:
    return Path.home() / "Documents" / "USBArrayManager" / "benchmark_results.json"


class BenchmarkStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_benchmark_results_path()

    def load_all(self) -> dict[int, BenchmarkResult]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported schema")
            raw_results = data.get("results")
            if not isinstance(raw_results, dict):
                raise ValueError("missing results")
            return {
                int(slot): BenchmarkResult.from_dict(result)
                for slot, result in raw_results.items()
                if isinstance(result, dict)
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise BenchmarkStoreError(
                f"Could not read benchmark results: {self.path}"
            ) from error

    def save(self, result: BenchmarkResult) -> None:
        results = self.load_all()
        results[result.slot] = result
        self._save_all(results)

    def remove_slots(self, *slots: int) -> None:
        results = self.load_all()
        changed = False
        for slot in slots:
            if results.pop(slot, None) is not None:
                changed = True
        if changed:
            self._save_all(results)

    def _save_all(self, results: dict[int, BenchmarkResult]) -> None:
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "results": {
                str(slot): results[slot].to_dict() for slot in sorted(results)
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(".tmp")
            with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise BenchmarkStoreError(
                f"Could not save benchmark results: {self.path}"
            ) from error
