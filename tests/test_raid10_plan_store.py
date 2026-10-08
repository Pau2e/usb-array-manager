import json
import tempfile
import unittest
from pathlib import Path

from usb_array_manager.services.raid10_plan_store import (
    Raid10PlanStore,
    Raid10PlanStoreError,
)


class Raid10PlanStoreTests(unittest.TestCase):
    def test_plan_survives_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raid10_plan.json"
            plan = Raid10PlanStore(path).save(
                ((1, 2), (3, 4)),
                saved_at="2026-10-08T00:00:00+00:00",
            )

            loaded = Raid10PlanStore(path).load()

        self.assertEqual(loaded, plan)
        self.assertEqual(loaded.pairs, ((1, 2), (3, 4)))

    def test_invalid_schema_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raid10_plan.json"
            path.write_text(
                json.dumps({"schema_version": 99, "pairs": []}),
                encoding="utf-8",
            )

            with self.assertRaises(Raid10PlanStoreError):
                Raid10PlanStore(path).load()

    def test_duplicate_slot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                Raid10PlanStore(Path(directory) / "plan.json").save(
                    ((1, 2), (2, 3))
                )


if __name__ == "__main__":
    unittest.main()

