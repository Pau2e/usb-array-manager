import tempfile
import threading
import unittest
from pathlib import Path

from usb_array_manager.services.benchmark import (
    BenchmarkCancelled,
    BenchmarkSafetyError,
    BenchmarkSettings,
    qualify_result,
    run_benchmark,
)


class BenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.settings = BenchmarkSettings(
            file_size_bytes=2 * 1024**2,
            block_size_bytes=64 * 1024,
            sample_size_bytes=256 * 1024,
            burst_size_bytes=256 * 1024,
            free_space_reserve_bytes=0,
            random_4k_operations=8,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_benchmark_uses_and_removes_temporary_file(self) -> None:
        progress: list[int] = []

        result = run_benchmark(
            self.root,
            2,
            "T:",
            settings=self.settings,
            include_random_4k=True,
            progress=lambda percent, _message: progress.append(percent),
        )

        self.assertEqual(result.slot, 2)
        self.assertGreater(result.sequential_read_mbps, 0)
        self.assertGreater(result.sequential_write_mbps, 0)
        self.assertIsNotNone(result.random_4k_read_iops)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancelled_benchmark_removes_temporary_file(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(BenchmarkCancelled):
            run_benchmark(
                self.root,
                1,
                "T:",
                settings=self.settings,
                cancel_event=cancel_event,
            )

        self.assertEqual(list(self.root.iterdir()), [])

    def test_refuses_target_without_required_free_space(self) -> None:
        unsafe_settings = BenchmarkSettings(
            file_size_bytes=10**20,
            block_size_bytes=4096,
            sample_size_bytes=8192,
            burst_size_bytes=4096,
            free_space_reserve_bytes=0,
        )

        with self.assertRaises(BenchmarkSafetyError):
            run_benchmark(self.root, 1, "T:", settings=unsafe_settings)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_qualification_thresholds(self) -> None:
        self.assertEqual(qualify_result(100, 40, 0.1), "GOOD")
        self.assertEqual(qualify_result(100, 10, 0.1), "SLOW WRITE")
        self.assertEqual(qualify_result(20, 40, 0.1), "SLOW READ")
        self.assertEqual(qualify_result(100, 40, 0.7), "UNSTABLE")


if __name__ == "__main__":
    unittest.main()
