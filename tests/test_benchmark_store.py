import tempfile
import unittest
from pathlib import Path

from usb_array_manager.models.benchmark_result import BenchmarkResult
from usb_array_manager.services.benchmark_store import BenchmarkStore


class BenchmarkStoreTests(unittest.TestCase):
    def test_result_survives_store_restart_and_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark_results.json"
            result = BenchmarkResult(
                slot=4,
                tested_at="2026-10-05T12:00:00+00:00",
                drive_letter="L:",
                file_size_bytes=1024**3,
                sequential_read_mbps=100.0,
                sequential_write_mbps=40.0,
                burst_write_mbps=60.0,
                sustained_write_mbps=30.0,
                write_stability_cv=0.1,
                qualification="GOOD",
            )
            BenchmarkStore(path).save(result)

            loaded = BenchmarkStore(path).load_all()
            self.assertEqual(loaded[4], result)

            BenchmarkStore(path).remove_slots(4)
            self.assertEqual(BenchmarkStore(path).load_all(), {})


if __name__ == "__main__":
    unittest.main()
