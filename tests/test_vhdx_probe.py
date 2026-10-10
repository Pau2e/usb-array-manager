import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from usb_array_manager.models.raid_backend import (
    NEEDS_ADMINISTRATOR,
    PROBE_FAILED,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
)
from usb_array_manager.models.saved_raid10_plan import SavedRaid10Plan
from usb_array_manager.models.storage_device import StorageDevice
from usb_array_manager.models.vhdx_probe import (
    VhdxBenchmarkComparison,
    VhdxDiskInfo,
)
from usb_array_manager.services.raid_backend import VhdxStorageSpacesBackend
from usb_array_manager.services.vhdx_probe import (
    TEST_FOLDER_NAME,
    DiskPartVhdxRunner,
    VhdxProbeError,
    VhdxProbeSettings,
    _clean_process_output,
    probe_vhdx_backend,
)


def device(slot: int = 1, filesystem: str = "exFAT") -> StorageDevice:
    return StorageDevice(
        model=f"USB {slot}",
        serial_number=f"SERIAL-{slot}",
        capacity_bytes=64 * 1024**3,
        drive_letters=(f"{chr(70 + slot)}:",),
        device_path=rf"\\.\PHYSICALDRIVE{slot + 4}",
        pnp_device_id=f"USBSTOR\\DISK-{slot}",
        usb_vid="1234",
        usb_pid="5678",
        is_connected=True,
        bus_type="USB",
        media_type="Removable Media",
        health_status="Healthy",
        storage_unique_id=f"UNIQUE-{slot}",
        slot=slot,
        disk_number=slot + 4,
        is_removable=True,
        is_boot_disk=False,
        is_system_disk=False,
        is_read_only=False,
        is_offline=False,
        partition_count=1,
        partition_style="MBR",
        filesystem_types=(filesystem,),
        operational_status="Online",
    )


class FakeRunner:
    def __init__(self, *, can_pool=True, attach_error=None, detach_error=None) -> None:
        self.can_pool = can_pool
        self.attach_error = attach_error
        self.detach_error = detach_error
        self.created = False
        self.detached = False
        self.prepare_filesystem = False

    def create(self, path: Path, _size: int) -> None:
        self.created = True
        path.write_bytes(b"temporary-vhdx")

    def attach(self, _path: Path, *, prepare_filesystem: bool) -> VhdxDiskInfo:
        self.prepare_filesystem = prepare_filesystem
        if self.attach_error:
            raise VhdxProbeError(self.attach_error)
        return VhdxDiskInfo(
            42,
            "VHDX-UNIQUE",
            "File Backed Virtual",
            "Unspecified",
            self.can_pool,
            "OK",
            "Healthy",
            "V:\\" if prepare_filesystem else None,
        )

    def detach(self, _path: Path) -> None:
        self.detached = True
        if self.detach_error:
            raise VhdxProbeError(self.detach_error)


class PartialCreateFailureRunner(FakeRunner):
    def create(self, path: Path, _size: int) -> None:
        path.write_bytes(b"uncertain-partial-file")
        raise VhdxProbeError("create failed")


class VhdxProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = VhdxProbeSettings(
            vhdx_size_bytes=1024,
            free_space_reserve_bytes=0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_probe(self, runner: FakeRunner, **kwargs):
        kwargs.setdefault("elevation_checker", lambda: True)
        return probe_vhdx_backend(
            device(),
            root=self.root,
            settings=self.settings,
            runner=runner,
            name_factory=lambda: "fixed",
            **kwargs,
        )

    def test_successful_vhdx_probe_and_cleanup(self) -> None:
        runner = FakeRunner()
        result = self.run_probe(runner)

        self.assertEqual(result.status, SUPPORTED)
        self.assertTrue(runner.created)
        self.assertTrue(runner.detached)
        self.assertEqual(result.virtual_disk.bus_type, "File Backed Virtual")
        self.assertEqual(result.cleanup_status, "Detached and temporary file deleted")
        self.assertFalse(Path(result.vhdx_path).exists())

    def test_vhdx_not_poolable(self) -> None:
        result = self.run_probe(FakeRunner(can_pool=False))

        self.assertEqual(result.status, UNSUPPORTED)
        self.assertIn("CanPool=False", result.explanation)
        self.assertIsNone(result.leftover_path)

    def test_unknown_poolability_is_unknown(self) -> None:
        result = self.run_probe(FakeRunner(can_pool=None))
        self.assertEqual(result.status, UNKNOWN)

    def test_non_elevated_probe_stops_before_any_file_operation(self) -> None:
        runner = FakeRunner()

        result = self.run_probe(runner, elevation_checker=lambda: False)

        self.assertEqual(result.status, NEEDS_ADMINISTRATOR)
        self.assertIn("Restart USB Array Manager as Administrator", result.explanation)
        self.assertIn("non-elevated", result.diagnostic_details)
        self.assertFalse(runner.created)
        self.assertFalse((self.root / TEST_FOLDER_NAME).exists())

    def test_unknown_elevation_state_stops_probe_as_unknown(self) -> None:
        runner = FakeRunner()

        result = self.run_probe(runner, elevation_checker=lambda: None)

        self.assertEqual(result.status, UNKNOWN)
        self.assertIn("could not determine", result.explanation)
        self.assertFalse(runner.created)
        self.assertFalse((self.root / TEST_FOLDER_NAME).exists())

    def test_needs_administrator_propagates_to_backend_without_saved_plan(self) -> None:
        backend = VhdxStorageSpacesBackend()
        result = self.run_probe(FakeRunner(), elevation_checker=lambda: False)

        backend.record_probe(result)
        assessment = backend.assess(None, [device()])

        self.assertEqual(assessment.status, NEEDS_ADMINISTRATOR)
        self.assertIn("Restart USB Array Manager as Administrator", assessment.explanation)

    def test_fat32_does_not_block_small_temporary_probe(self) -> None:
        result = probe_vhdx_backend(
            device(filesystem="FAT32"),
            root=self.root,
            settings=self.settings,
            runner=FakeRunner(),
            name_factory=lambda: "fat32",
            elevation_checker=lambda: True,
        )

        self.assertEqual(result.status, SUPPORTED)
        self.assertEqual(result.filesystem, "FAT32")

    def test_insufficient_free_space_stops_before_creation(self) -> None:
        runner = FakeRunner()
        with patch(
            "usb_array_manager.services.vhdx_probe.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            result = self.run_probe(runner)

        self.assertEqual(result.status, PROBE_FAILED)
        self.assertIn("Insufficient free space", result.explanation)
        self.assertFalse(runner.created)

    def test_attach_failure_still_deletes_file(self) -> None:
        runner = FakeRunner(attach_error="attach failed")
        result = self.run_probe(runner)

        self.assertEqual(result.status, PROBE_FAILED)
        self.assertIn("attach failed", result.explanation)
        self.assertTrue(runner.detached)
        self.assertIsNone(result.leftover_path)

    def test_detach_failure_reports_leftover_path(self) -> None:
        result = self.run_probe(FakeRunner(detach_error="detach failed"))

        self.assertEqual(result.status, PROBE_FAILED)
        self.assertIn("FAILED to detach", result.cleanup_status)
        self.assertEqual(result.leftover_path, result.vhdx_path)
        Path(result.leftover_path).unlink()

    def test_cleanup_failure_reports_leftover_path(self) -> None:
        original_unlink = Path.unlink

        def fail_vhdx(path: Path, *args, **kwargs):
            if path.suffix == ".vhdx":
                raise OSError("delete denied")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_vhdx):
            result = self.run_probe(FakeRunner())

        self.assertEqual(result.status, PROBE_FAILED)
        self.assertIn("FAILED to delete", result.cleanup_status)
        self.assertEqual(result.leftover_path, result.vhdx_path)
        original_unlink(Path(result.leftover_path))

    def test_existing_test_filename_collision_is_never_overwritten(self) -> None:
        folder = self.root / TEST_FOLDER_NAME
        folder.mkdir()
        existing = folder / "probe-fixed.vhdx"
        existing.write_bytes(b"keep-me")
        runner = FakeRunner()

        result = self.run_probe(runner)

        self.assertEqual(result.status, PROBE_FAILED)
        self.assertFalse(runner.created)
        self.assertEqual(existing.read_bytes(), b"keep-me")
        self.assertIsNone(result.leftover_path)

    def test_failed_creation_with_residue_is_reported_but_not_deleted(self) -> None:
        result = self.run_probe(PartialCreateFailureRunner())

        self.assertEqual(result.status, PROBE_FAILED)
        self.assertEqual(result.leftover_path, result.vhdx_path)
        self.assertEqual(Path(result.leftover_path).read_bytes(), b"uncertain-partial-file")
        Path(result.leftover_path).unlink()

    def test_performance_probe_uses_virtual_filesystem_and_cleans_up(self) -> None:
        comparison = VhdxBenchmarkComparison(100, 50, 40, 95, 45, 36)
        runner = FakeRunner()
        with patch(
            "usb_array_manager.services.vhdx_probe._benchmark_comparison",
            return_value=comparison,
        ) as benchmark:
            result = self.run_probe(runner, performance=True)

        self.assertEqual(result.status, SUPPORTED)
        self.assertTrue(runner.prepare_filesystem)
        self.assertIs(result.benchmark, comparison)
        benchmark.assert_called_once()
        self.assertFalse(Path(result.vhdx_path).exists())

    def test_system_disk_is_rejected_before_writing(self) -> None:
        runner = FakeRunner()
        result = probe_vhdx_backend(
            replace(device(), is_system_disk=True),
            root=self.root,
            settings=self.settings,
            runner=runner,
            elevation_checker=lambda: True,
        )

        self.assertEqual(result.status, UNSUPPORTED)
        self.assertIn("system or boot", result.explanation)
        self.assertFalse(runner.created)

    def test_diskpart_unavailable_is_unknown(self) -> None:
        with patch(
            "usb_array_manager.services.vhdx_probe.shutil.which",
            return_value=None,
        ):
            result = self.run_probe(DiskPartVhdxRunner())

        self.assertEqual(result.status, UNKNOWN)
        self.assertIn("DiskPart is unavailable", result.explanation)
        self.assertIsNone(result.leftover_path)

    def test_unknown_system_disk_state_is_rejected_before_writing(self) -> None:
        runner = FakeRunner()
        result = probe_vhdx_backend(
            replace(device(), is_system_disk=None),
            root=self.root,
            settings=self.settings,
            runner=runner,
            elevation_checker=lambda: True,
        )

        self.assertEqual(result.status, UNSUPPORTED)
        self.assertIn("could not prove", result.explanation)
        self.assertFalse(runner.created)


class DiskPartVhdxRunnerTests(unittest.TestCase):
    @staticmethod
    def disk(number: int, bus_type: str = "File Backed Virtual") -> VhdxDiskInfo:
        return VhdxDiskInfo(
            number,
            f"IDENTITY-{number}",
            bus_type,
            "Unspecified",
            True,
            "Online",
            "Healthy",
        )

    def test_diskpart_create_attach_detach_lifecycle(self) -> None:
        runner = DiskPartVhdxRunner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.vhdx"
            commands: list[tuple[str, ...]] = []

            def run_diskpart(items: tuple[str, ...]) -> str:
                commands.append(items)
                if items[0].startswith("create vdisk"):
                    path.write_bytes(b"vhdx")
                return "DiskPart succeeded"

            inventories = (
                {0: self.disk(0, "SATA")},
                {0: self.disk(0, "SATA"), 42: self.disk(42)},
                {0: self.disk(0, "SATA")},
            )
            with (
                patch.object(runner, "_run_diskpart", side_effect=run_diskpart),
                patch.object(runner, "_enumerate_disks", side_effect=inventories),
            ):
                runner.create(path, 512 * 1024**2)
                detected = runner.attach(path, prepare_filesystem=False)
                runner.detach(path)

        quoted = f'"{path}"'
        self.assertEqual(
            commands,
            [
                (
                    f"create vdisk file={quoted} maximum=512 type=expandable",
                ),
                (f"select vdisk file={quoted}", "attach vdisk"),
                (f"select vdisk file={quoted}", "detach vdisk"),
            ],
        )
        self.assertEqual(detected.disk_number, 42)

    def test_diskpart_script_uses_one_command_per_line_and_is_removed(self) -> None:
        runner = DiskPartVhdxRunner()
        observed: dict[str, object] = {}

        def run(command, **_kwargs):
            script_path = Path(command[2])
            observed["command"] = command
            observed["script_path"] = script_path
            observed["script"] = script_path.read_text(encoding="utf-16")
            return SimpleNamespace(
                returncode=0,
                stdout="DiskPart succeeded".encode("utf-16"),
                stderr=b"",
            )

        with (
            patch(
                "usb_array_manager.services.vhdx_probe.shutil.which",
                return_value=r"C:\Windows\System32\diskpart.exe",
            ),
            patch(
                "usb_array_manager.services.vhdx_probe.subprocess.run",
                side_effect=run,
            ),
        ):
            output = runner._run_diskpart(
                ('select vdisk file="C:\\probe.vhdx"', "attach vdisk")
            )

        self.assertEqual(
            observed["command"][:2],
            [r"C:\Windows\System32\diskpart.exe", "/s"],
        )
        self.assertEqual(
            observed["script"],
            'select vdisk file="C:\\probe.vhdx"\nattach vdisk\n',
        )
        self.assertFalse(observed["script_path"].exists())
        self.assertEqual(output, "DiskPart succeeded")

    def test_winerror_740_is_classified_as_needing_administrator(self) -> None:
        runner = DiskPartVhdxRunner()
        elevation_error = OSError(
            "[WinError 740] The requested operation requires elevation"
        )
        with (
            patch(
                "usb_array_manager.services.vhdx_probe.shutil.which",
                return_value=r"C:\Windows\System32\diskpart.exe",
            ),
            patch(
                "usb_array_manager.services.vhdx_probe.subprocess.run",
                side_effect=elevation_error,
            ),
        ):
            with self.assertRaises(VhdxProbeError) as raised:
                runner._run_diskpart(("attach vdisk",))

        self.assertTrue(raised.exception.needs_administrator)
        self.assertIn("Restart USB Array Manager as Administrator", str(raised.exception))
        self.assertIn("WinError 740", raised.exception.details)

    def test_attach_rejects_ambiguous_new_disk_numbers(self) -> None:
        runner = DiskPartVhdxRunner()
        before = {0: self.disk(0, "SATA")}
        after = {
            0: self.disk(0, "SATA"),
            41: self.disk(41),
            42: self.disk(42),
        }
        with (
            patch.object(runner, "_run_diskpart", return_value="attached"),
            patch.object(runner, "_enumerate_disks", side_effect=(before, after)),
        ):
            with self.assertRaisesRegex(VhdxProbeError, "exactly one new disk"):
                runner.attach(Path("probe.vhdx"), prepare_filesystem=False)

    def test_attach_rejects_non_file_backed_disk(self) -> None:
        runner = DiskPartVhdxRunner()
        before = {0: self.disk(0, "SATA")}
        after = {0: self.disk(0, "SATA"), 42: self.disk(42, "USB")}
        with (
            patch.object(runner, "_run_diskpart", return_value="attached"),
            patch.object(runner, "_enumerate_disks", side_effect=(before, after)),
        ):
            with self.assertRaisesRegex(VhdxProbeError, "File Backed Virtual"):
                runner.attach(Path("probe.vhdx"), prepare_filesystem=False)

    def test_performance_setup_receives_only_verified_virtual_disk(self) -> None:
        runner = DiskPartVhdxRunner()
        before = {0: self.disk(0, "SATA")}
        virtual = self.disk(42)
        after = {0: self.disk(0, "SATA"), 42: virtual}
        with (
            patch.object(runner, "_run_diskpart", return_value="attached"),
            patch.object(runner, "_enumerate_disks", side_effect=(before, after)),
            patch.object(
                runner,
                "_prepare_filesystem",
                return_value="V:\\",
            ) as prepare,
        ):
            detected = runner.attach(
                Path("probe.vhdx"),
                prepare_filesystem=True,
            )

        prepare.assert_called_once_with(virtual)
        self.assertEqual(detected.volume_root, "V:\\")

    def test_powershell_clixml_is_reduced_to_readable_diagnostics(self) -> None:
        raw = (
            '#< CLIXML\n<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<S S="Error">Storage inspection failed_x000D__x000A_See details</S>'
            "</Objs>"
        ).encode()

        cleaned = _clean_process_output(b"", raw)

        self.assertNotIn("CLIXML", cleaned)
        self.assertNotIn("_x000D_", cleaned)
        self.assertEqual(cleaned, "Storage inspection failed\r\nSee details")

class VhdxBackendAssessmentTests(unittest.TestCase):
    def test_successful_single_probe_enables_four_member_dry_run(self) -> None:
        backend = VhdxStorageSpacesBackend()
        plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-10T10:00:00+00:00")
        devices = [device(slot) for slot in range(1, 5)]
        with tempfile.TemporaryDirectory() as directory:
            result = probe_vhdx_backend(
                devices[0],
                root=Path(directory),
                settings=VhdxProbeSettings(
                    vhdx_size_bytes=1024,
                    free_space_reserve_bytes=0,
                ),
                runner=FakeRunner(),
                name_factory=lambda: "backend",
                elevation_checker=lambda: True,
            )
        backend.record_probe(result)

        assessment = backend.assess(plan, devices)

        self.assertEqual(assessment.status, SUPPORTED)
        self.assertEqual(len(assessment.drives), 4)
        self.assertIn("Slot 1", assessment.dry_run_text)
        self.assertIn("Slot 4", assessment.dry_run_text)
        self.assertIn("PREVIEW ONLY — NOT EXECUTED", assessment.dry_run_text)

    def test_fat32_blocks_production_dry_run(self) -> None:
        backend = VhdxStorageSpacesBackend()
        plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-10T10:00:00+00:00")
        devices = [device(slot, "FAT32" if slot == 1 else "exFAT") for slot in range(1, 5)]

        assessment = backend.assess(plan, devices)

        self.assertEqual(assessment.status, UNSUPPORTED)
        self.assertTrue(any("larger than 4 GiB" in item for item in assessment.blockers))
        self.assertIn("BLOCKED", assessment.dry_run_text)


if __name__ == "__main__":
    unittest.main()
