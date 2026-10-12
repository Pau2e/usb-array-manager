import json
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
    def __init__(self, *, can_pool=True, attach_error=None, detach_error=None,
                 size_bytes=None, reasons=(), other_reason="") -> None:
        self.can_pool = can_pool
        self.attach_error = attach_error
        self.detach_error = detach_error
        self.created = False
        self.detached = False
        self.prepare_filesystem = False
        self.size_bytes = size_bytes
        self.reasons = reasons
        self.other_reason = other_reason

    def create(self, path: Path, _size: int) -> None:
        self.created = True
        self.created_size = _size
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
            size_bytes=self.size_bytes,
            cannot_pool_reasons=self.reasons,
            other_cannot_pool_reason_description=self.other_reason,
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

    def test_ntfs_default_probe_uses_pool_eligibility_size(self) -> None:
        runner = FakeRunner(size_bytes=8 * 1024**3)
        result = probe_vhdx_backend(
            device(filesystem="NTFS"), root=self.root, runner=runner,
            elevation_checker=lambda: True,
        )
        self.assertEqual(runner.created_size, 8 * 1024**3)
        self.assertEqual(result.status, SUPPORTED)

    def test_fat32_default_probe_uses_eight_gib(self) -> None:
        runner = FakeRunner(size_bytes=8 * 1024**3)
        result = probe_vhdx_backend(
            device(filesystem="FAT32"), root=self.root, runner=runner,
            elevation_checker=lambda: True,
        )
        self.assertEqual(runner.created_size, 8 * 1024**3)
        self.assertEqual(result.status, SUPPORTED)

    def test_mixed_filesystems_default_probe_uses_eight_gib(self) -> None:
        runner = FakeRunner(size_bytes=8 * 1024**3)
        mixed = replace(device(filesystem="FAT32"),
                        drive_letters=("G:", "H:"),
                        filesystem_types=("FAT32", "NTFS"))
        result = probe_vhdx_backend(
            mixed, root=self.root, runner=runner, elevation_checker=lambda: True,
        )
        self.assertEqual(runner.created_size, 8 * 1024**3)
        self.assertEqual(result.status, SUPPORTED)

    def test_unknown_and_exfat_filesystems_do_not_reduce_default_size(self) -> None:
        for filesystems in ((), ("exFAT",)):
            with self.subTest(filesystems=filesystems):
                runner = FakeRunner(size_bytes=8 * 1024**3)
                result = probe_vhdx_backend(
                    replace(device(), filesystem_types=filesystems), root=self.root,
                    runner=runner, elevation_checker=lambda: True,
                )
                self.assertEqual(runner.created_size, 8 * 1024**3)
                self.assertEqual(result.status, SUPPORTED)

    def test_default_probe_does_not_downgrade_when_eight_gib_will_not_fit(self) -> None:
        runner = FakeRunner()
        with patch("usb_array_manager.services.vhdx_probe.shutil.disk_usage",
                   return_value=SimpleNamespace(free=1024**3)):
            result = probe_vhdx_backend(
                device(filesystem="FAT32"), root=self.root, runner=runner,
                elevation_checker=lambda: True,
            )
        self.assertEqual(result.status, PROBE_FAILED)
        self.assertFalse(runner.created)
        self.assertIn("8704 MiB required", result.explanation)

    def test_small_nonpoolable_probe_is_inconclusive(self) -> None:
        result = self.run_probe(FakeRunner(can_pool=False, size_bytes=512 * 1024**2))

        self.assertEqual(result.status, UNKNOWN)
        self.assertIs(result.virtual_disk.can_pool, False)
        self.assertIn("CanPool=False", result.explanation)
        self.assertIn("512 MiB", result.explanation)
        self.assertIn("inconclusive", result.explanation)
        self.assertIsNone(result.leftover_path)

    def test_insufficient_capacity_reason_is_inconclusive_and_preserved(self) -> None:
        result = self.run_probe(FakeRunner(
            can_pool=False, size_bytes=8 * 1024**3,
            reasons=("Insufficient Capacity (7)",), other_reason="Provider capacity constraint",
        ))
        self.assertEqual(result.status, UNKNOWN)
        self.assertIn("Insufficient Capacity (7)", result.explanation)
        self.assertIn("Provider capacity constraint", result.explanation)
        self.assertEqual(result.virtual_disk.size_bytes, 8 * 1024**3)

    def test_large_rejected_probe_without_reason_is_unknown(self) -> None:
        result = self.run_probe(FakeRunner(can_pool=False, size_bytes=8 * 1024**3))
        self.assertEqual(result.status, UNKNOWN)
        self.assertIn("did not report a reason", result.explanation)

    def test_large_rejected_probe_reports_bounded_current_state(self) -> None:
        result = self.run_probe(FakeRunner(
            can_pool=False, size_bytes=8 * 1024**3, reasons=("Offline (6)",),
        ))
        self.assertEqual(result.status, UNSUPPORTED)
        self.assertIn("tested disk in its current state", result.explanation)
        self.assertIn("Offline (6)", result.virtual_disk.pool_rejection_details)

    def test_unknown_poolability_is_unknown(self) -> None:
        result = self.run_probe(FakeRunner(can_pool=None))
        self.assertEqual(result.status, UNKNOWN)

    def test_storage_provider_warning_is_concise_in_probe_result(self) -> None:
        runner = FakeRunner(can_pool=None)
        runner.storage_inspection_warning = (
            "The Windows Storage provider failed while querying Get-Disk/"
            "Get-PhysicalDisk; CanPool is unavailable. See Logs / Details."
        )

        result = self.run_probe(runner)

        self.assertEqual(result.status, UNKNOWN)
        self.assertEqual(result.explanation, runner.storage_inspection_warning)
        self.assertNotIn("CLIXML", result.explanation)

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
        runner = DiskPartVhdxRunner()
        with (
            patch(
                "usb_array_manager.services.vhdx_probe.shutil.which",
                return_value=None,
            ),
            patch.object(runner, "_enumerate_disks", return_value={}),
        ):
            result = self.run_probe(runner)

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
    def setUp(self) -> None:
        # Every test is a simulation, including newly added capability queries.
        patcher = patch.object(
            DiskPartVhdxRunner, "_run_powershell",
            side_effect=VhdxProbeError("Mock provider unavailable"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
            size_bytes=512 * 1024**2,
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

    def test_create_and_attach_uses_one_script_and_before_after_inventory(self) -> None:
        runner = DiskPartVhdxRunner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.vhdx"
            before = {0: self.disk(0, "SATA")}
            virtual = self.disk(42)
            after = {0: self.disk(0, "SATA"), 42: virtual}
            observed: list[tuple[str, ...]] = []

            def run_diskpart(commands: tuple[str, ...]) -> str:
                observed.append(commands)
                path.write_bytes(b"vhdx")
                return "created and attached"

            with (
                patch.object(runner, "_run_diskpart", side_effect=run_diskpart),
                patch.object(
                    runner,
                    "_enumerate_disks",
                    side_effect=(before, after),
                ) as enumerate_disks,
                patch.object(runner, "_inspect_attached_storage_disk", return_value=virtual),
            ):
                detected = runner.create_and_attach(
                    path,
                    512 * 1024**2,
                    prepare_filesystem=False,
                )

        quoted = f'"{path}"'
        self.assertEqual(
            observed,
            [
                (
                    f"create vdisk file={quoted} maximum=512 type=expandable",
                    f"select vdisk file={quoted}",
                    "attach vdisk",
                )
            ],
        )
        self.assertEqual(enumerate_disks.call_count, 2)
        self.assertIs(detected, virtual)

    def test_diskpart_script_uses_one_command_per_line_and_is_removed(self) -> None:
        runner = DiskPartVhdxRunner()
        observed: dict[str, object] = {}

        def run(command, **_kwargs):
            script_path = Path(command[2])
            observed["diagnostic_before_run"] = runner.diagnostic_details
            observed["command"] = command
            observed["script_path"] = script_path
            observed["script"] = script_path.read_text(encoding="utf-8")
            return SimpleNamespace(
                returncode=0,
                stdout=b"DiskPart succeeded",
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
                (
                    'create vdisk file="J:\\path\\probe.vhdx" maximum=512 type=expandable',
                    'select vdisk file="J:\\path\\probe.vhdx"',
                    "attach vdisk",
                )
            )

        self.assertEqual(
            observed["command"][:2],
            [r"C:\Windows\System32\diskpart.exe", "/s"],
        )
        self.assertEqual(
            observed["script"],
            'create vdisk file="J:\\path\\probe.vhdx" maximum=512 type=expandable\n'
            'select vdisk file="J:\\path\\probe.vhdx"\n'
            "attach vdisk\n"
            "exit\n",
        )
        self.assertFalse(observed["script_path"].exists())
        self.assertEqual(output, "DiskPart succeeded")
        self.assertIn("Exit code: 0", runner.diagnostic_details)
        self.assertIn("Stdout:\nDiskPart succeeded", runner.diagnostic_details)
        self.assertNotIn(str(observed["script_path"]), runner.diagnostic_details)
        self.assertIn(
            "DiskPart script to execute",
            observed["diagnostic_before_run"],
        )

    def test_generic_diskpart_help_is_an_execution_failure(self) -> None:
        runner = DiskPartVhdxRunner()
        generic_help = b"""Microsoft DiskPart version 10.0
ACTIVE      - Marks the current partition as active.
ASSIGN      - Assigns a drive letter or mount point.
CREATE      - Creates a volume, partition or virtual disk.
SELECT      - Shifts the focus to an object.
"""
        with (
            patch(
                "usb_array_manager.services.vhdx_probe.shutil.which",
                return_value=r"C:\Windows\System32\diskpart.exe",
            ),
            patch(
                "usb_array_manager.services.vhdx_probe.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=generic_help,
                    stderr=b"",
                ),
            ),
        ):
            with self.assertRaisesRegex(
                VhdxProbeError,
                "displayed command help",
            ):
                runner._run_diskpart(("attach vdisk",))

        self.assertIn("Exit code: 0", runner.diagnostic_details)
        self.assertIn("CREATE", runner.diagnostic_details)

    def test_nonzero_diskpart_create_is_a_probe_execution_failure(self) -> None:
        runner = DiskPartVhdxRunner()
        with (
            patch(
                "usb_array_manager.services.vhdx_probe.shutil.which",
                return_value=r"C:\Windows\System32\diskpart.exe",
            ),
            patch(
                "usb_array_manager.services.vhdx_probe.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=b"DiskPart has encountered an error.",
                ),
            ),
        ):
            with self.assertRaisesRegex(VhdxProbeError, "reported an error"):
                runner._run_diskpart(
                    ('create vdisk file="J:\\probe.vhdx" maximum=512 type=expandable',)
                )

        self.assertIn("Exit code: 1", runner.diagnostic_details)
        self.assertIn(
            "Stderr:\nDiskPart has encountered an error.",
            runner.diagnostic_details,
        )

    def test_diskpart_text_error_fails_even_with_zero_exit_code(self) -> None:
        runner = DiskPartVhdxRunner()
        with (
            patch(
                "usb_array_manager.services.vhdx_probe.shutil.which",
                return_value=r"C:\Windows\System32\diskpart.exe",
            ),
            patch(
                "usb_array_manager.services.vhdx_probe.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=b"Virtual Disk Service error: operation failed.",
                    stderr=b"",
                ),
            ),
        ):
            with self.assertRaisesRegex(VhdxProbeError, "could not be completed"):
                runner._run_diskpart(("attach vdisk",))

        self.assertIn("Exit code: 0", runner.diagnostic_details)

    def test_detach_uses_explicit_script_and_exit(self) -> None:
        runner = DiskPartVhdxRunner()
        observed: dict[str, object] = {}

        def run(command, **_kwargs):
            script_path = Path(command[2])
            observed["command"] = command
            observed["script"] = script_path.read_text(encoding="utf-8")
            observed["script_path"] = script_path
            return SimpleNamespace(returncode=0, stdout=b"detached", stderr=b"")

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
            runner.detach(Path(r"J:\path\probe.vhdx"))

        self.assertEqual(
            observed["script"],
            'select vdisk file="J:\\path\\probe.vhdx"\ndetach vdisk\nexit\n',
        )
        self.assertEqual(observed["command"][1], "/s")
        self.assertFalse(observed["script_path"].exists())

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
            patch.object(runner, "_inspect_attached_storage_disk", return_value=virtual),
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

    def test_performance_letter_is_assigned_only_after_format_success(self) -> None:
        runner = DiskPartVhdxRunner()
        with patch.object(runner, "_run_powershell", return_value="V:\\") as run:
            self.assertEqual(runner._prepare_filesystem(self.disk(42)), "V:\\")
        script = run.call_args.args[0]
        partition_line = next(line for line in script.splitlines() if "New-Partition" in line)
        self.assertNotIn("AssignDriveLetter", partition_line)
        self.assertLess(script.index("Format-Volume"), script.index("Add-PartitionAccessPath"))
        self.assertIn("-AssignDriveLetter -ErrorAction Stop", script)
        self.assertIn("Format-Volume -FileSystem NTFS", script)
        self.assertIn("-Confirm:$false -ErrorAction Stop", script)
        self.assertIn("Get-Partition -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber", script)
        self.assertLess(script.index("Refusing to initialize"), script.index("Initialize-Disk"))
        self.assertLess(script.index("identity changed"), script.index("Initialize-Disk"))

    def test_performance_preparation_failure_propagates_to_existing_cleanup(self) -> None:
        runner = DiskPartVhdxRunner()
        error = VhdxProbeError("Formatting failed")
        with patch.object(runner, "_run_powershell", side_effect=error):
            with self.assertRaisesRegex(VhdxProbeError, "Formatting failed"):
                runner._prepare_filesystem(self.disk(42))

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

    def test_powershell_clixml_plaintext_prefix_is_preserved(self) -> None:
        raw = (
            '#< CLIXML\r\nGet-Disk failed: Invalid property\r\n'
            '<Objs Version="1.1.0.1" '
            'xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<Obj S="progress"><MS><PR N="Record">'
            '<AV>Preparing modules for first use.</AV>'
            "</PR></MS></Obj></Objs>"
        ).encode()

        cleaned = _clean_process_output(b"", raw)

        self.assertEqual(cleaned, "Get-Disk failed: Invalid property")
        self.assertNotIn("CLIXML", cleaned)
        self.assertNotIn("Preparing modules", cleaned)

    def test_storage_provider_failure_uses_win32_inventory_fallback(self) -> None:
        diagnostics: list[str] = []
        runner = DiskPartVhdxRunner(diagnostic_callback=diagnostics.append)
        fallback = {5: self.disk(5, "USB")}
        with (
            patch.object(
                runner,
                "_enumerate_storage_disks",
                side_effect=VhdxProbeError(
                    "Windows storage inspection failed.",
                    details=(
                        "Get-Disk failed: Invalid property\n"
                        "FullyQualifiedErrorId: HRESULT 0x80041031,Get-Disk"
                    ),
                ),
            ) as storage_inventory,
            patch.object(
                runner,
                "_enumerate_win32_disks",
                return_value=fallback,
            ) as win32_inventory,
        ):
            first = runner._enumerate_disks()
            second = runner._enumerate_disks()

        self.assertEqual(first, fallback)
        self.assertEqual(second, fallback)
        storage_inventory.assert_called_once()
        self.assertEqual(win32_inventory.call_count, 2)
        self.assertIn("Get-Disk failed: Invalid property", diagnostics[0])
        self.assertIn("CanPool is unavailable", runner.storage_inspection_warning)

    def test_fallback_still_attempts_post_attach_storage_query(self) -> None:
        runner = DiskPartVhdxRunner()
        runner._storage_provider_available = False
        before = {0: self.disk(0, "SATA")}
        virtual = replace(self.disk(42), can_pool=None)
        enriched = replace(virtual, can_pool=True)
        after = {0: self.disk(0, "SATA"), 42: virtual}
        with (
            patch.object(runner, "_enumerate_disks", return_value=after),
            patch.object(
                runner,
                "_inspect_attached_storage_disk",
                return_value=enriched,
            ) as storage_query,
        ):
            detected = runner._identify_attached_disk(
                Path("probe.vhdx"),
                before,
                "attached",
                prepare_filesystem=False,
            )

        storage_query.assert_called_once_with(virtual)
        self.assertIs(detected, enriched)

    @staticmethod
    def capability_json(*, can_pool: bool, provider_id: str = "42") -> str:
        return json.dumps(
            {
                "DeviceId": provider_id,
                "FriendlyName": "Microsoft Virtual Disk",
                "UniqueId": "VHDX-STORAGE-42",
                "ObjectId": "Storage Spaces PhysicalDisk:42 Virtual",
                "BusType": "File Backed Virtual",
                "MediaType": "Unspecified",
                "CanPool": can_pool,
                "Size": 512 * 1024**2,
                "HealthStatus": "Healthy",
                "OperationalStatus": "OK",
            }
        )

    def test_get_disk_failure_but_get_physical_disk_success(self) -> None:
        diagnostics: list[str] = []
        runner = DiskPartVhdxRunner(diagnostic_callback=diagnostics.append)
        verified = replace(self.disk(42), can_pool=None)

        def query(script: str) -> str:
            if "Get-Disk -Number" in script:
                raise VhdxProbeError(
                    "Windows storage inspection failed.",
                    details="Get-Disk -Number 42 failed: Invalid property",
                )
            if "$physicalDisks = @(Get-PhysicalDisk" in script:
                return self.capability_json(can_pool=True)
            raise VhdxProbeError("provider unavailable", details="provider unavailable")

        with patch.object(runner, "_run_powershell", side_effect=query):
            result = runner._inspect_attached_storage_disk(verified)

        self.assertTrue(result.can_pool)
        summary = diagnostics[-1]
        self.assertIn("Get-Disk: FAILED", summary)
        self.assertIn("Get-PhysicalDisk: READY", summary)
        self.assertIn("CanPool: True", summary)

    def test_get_disk_failure_but_direct_cim_success(self) -> None:
        diagnostics: list[str] = []
        runner = DiskPartVhdxRunner(diagnostic_callback=diagnostics.append)
        verified = replace(self.disk(42), can_pool=None)

        def query(script: str) -> str:
            if "MSFT_PhysicalDisk" in script:
                return self.capability_json(can_pool=False)
            raise VhdxProbeError(
                "Windows storage inspection failed.",
                details="HRESULT 0x80041031 Invalid property",
            )

        with patch.object(runner, "_run_powershell", side_effect=query):
            result = runner._inspect_attached_storage_disk(verified)

        self.assertFalse(result.can_pool)
        summary = diagnostics[-1]
        self.assertIn("Get-Disk: FAILED", summary)
        self.assertIn("MSFT_PhysicalDisk CIM: READY", summary)
        self.assertIn("CanPool: False", summary)

    def test_all_storage_capability_providers_unavailable_is_unknown(self) -> None:
        diagnostics: list[str] = []
        runner = DiskPartVhdxRunner(diagnostic_callback=diagnostics.append)
        verified = replace(self.disk(42), can_pool=None)
        with patch.object(
            runner,
            "_run_powershell",
            side_effect=VhdxProbeError(
                "Windows storage inspection failed.",
                details="HRESULT 0x80041031 Invalid property",
            ),
        ):
            result = runner._inspect_attached_storage_disk(verified)

        self.assertIsNone(result.can_pool)
        summary = diagnostics[-1]
        self.assertIn("Get-PhysicalDisk: FAILED", summary)
        self.assertIn("Primordial Storage Pool: FAILED", summary)
        self.assertIn("MSFT_PhysicalDisk CIM: FAILED", summary)
        self.assertIn("CanPool: UNKNOWN", summary)

    def test_storage_candidate_must_match_verified_disk_not_array_position(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None)
        wrong = self.capability_json(can_pool=True, provider_id="7")

        def query(script: str) -> str:
            if "Get-Disk -Number" in script:
                raise VhdxProbeError("Get-Disk failed", details="Get-Disk failed")
            return wrong

        with patch.object(runner, "_run_powershell", side_effect=query):
            result = runner._inspect_attached_storage_disk(verified)

        self.assertIsNone(result.can_pool)

    def test_primary_inventory_false_never_accepts_same_number_usb_candidate(self) -> None:
        runner = DiskPartVhdxRunner()
        virtual = replace(self.disk(42), can_pool=False)
        wrong = json.loads(self.capability_json(can_pool=False))
        wrong["BusType"] = "USB"
        # FriendlyName/ObjectId include "Virtual", so a name hint alone is unsafe.
        with (
            patch.object(runner, "_enumerate_disks", return_value={42: virtual}),
            patch.object(runner, "_run_powershell", return_value=json.dumps(wrong)),
        ):
            result = runner._identify_attached_disk(
                Path("probe.vhdx"), {}, "attached", prepare_filesystem=False,
            )
        self.assertIsNone(result.can_pool)
        self.assertIn("IGNORED DeviceId='42'", runner.diagnostic_details)
        self.assertIn("BusType='USB'", runner.diagnostic_details)

    def test_matching_number_cannot_override_conflicting_storage_identity(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None, storage_unique_id="EXPECTED-UID")
        with patch.object(runner, "_run_powershell", return_value=self.capability_json(can_pool=True)):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertIsNone(result.can_pool)

    def test_matching_number_without_storage_identity_also_requires_size(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None)
        wrong = json.loads(self.capability_json(can_pool=True))
        wrong["Size"] = 64 * 1024**3
        with patch.object(runner, "_run_powershell", return_value=json.dumps(wrong)):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertIsNone(result.can_pool)

    def test_get_disk_enriches_rounded_win32_size_before_physical_matching(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None,
                           size_bytes=512 * 1024**2 - 1024**2, size_is_exact=False)
        with patch.object(runner, "_run_powershell", return_value=self.capability_json(can_pool=True)):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertTrue(result.can_pool)
        self.assertTrue(result.size_is_exact)
        self.assertEqual(result.size_bytes, 512 * 1024**2)

    def test_rounded_win32_size_alone_cannot_prove_physical_identity(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None, size_is_exact=False)
        def query(script):
            if "Get-Disk -Number" in script:
                raise VhdxProbeError("Get-Disk unavailable")
            return self.capability_json(can_pool=True)
        with patch.object(runner, "_run_powershell", side_effect=query):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertIsNone(result.can_pool)

    def test_numeric_cim_bus_and_reasons_reach_result_and_diagnostics(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None)
        record = json.loads(self.capability_json(can_pool=False))
        record.update({
            "BusType": 15, "CannotPoolReason": [7, 32768],
            "OtherCannotPoolReasonDescription": "Vendor capacity details",
        })
        with patch.object(runner, "_run_powershell", return_value=json.dumps(record)):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertIs(result.can_pool, False)
        self.assertEqual(result.size_bytes, 512 * 1024**2)
        self.assertEqual(result.cannot_pool_reasons, (
            "Insufficient Capacity (7)", "Unrecognized reason (32768)",
        ))
        self.assertEqual(result.other_cannot_pool_reason_description, "Vendor capacity details")
        self.assertIn("UniqueId='VHDX-STORAGE-42'", runner.diagnostic_details)
        self.assertIn("Insufficient Capacity (7)", runner.diagnostic_details)
        self.assertIn("Vendor capacity details", runner.diagnostic_details)

    def test_conflicting_provider_values_remain_unknown(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None)
        def query(script):
            return self.capability_json(can_pool="MSFT_PhysicalDisk" not in script)
        with patch.object(runner, "_run_powershell", side_effect=query):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertIsNone(result.can_pool)
        self.assertIn("conflicting values", runner.diagnostic_details)

    def test_duplicate_matching_objects_remain_unknown_even_when_values_agree(self) -> None:
        runner = DiskPartVhdxRunner()
        verified = replace(self.disk(42), can_pool=None)
        record = json.loads(self.capability_json(can_pool=True))
        with patch.object(runner, "_run_powershell", return_value=json.dumps([record, record])):
            result = runner._inspect_attached_storage_disk(verified)
        self.assertIsNone(result.can_pool)
        self.assertIn("identity is ambiguous", runner.diagnostic_details)

    def test_empty_storage_inventory_uses_win32_fallback(self) -> None:
        runner = DiskPartVhdxRunner()
        fallback = {0: self.disk(0, "SATA")}
        with (
            patch.object(runner, "_enumerate_storage_disks", return_value={}),
            patch.object(runner, "_enumerate_win32_disks", return_value=fallback),
        ):
            self.assertEqual(runner._enumerate_disks(), fallback)
        self.assertIn("Get-Disk returned no disk objects", runner.diagnostic_details)

    def test_both_inventory_paths_fail_before_diskpart_or_vhdx_creation(self) -> None:
        runner = DiskPartVhdxRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(
                    runner,
                    "_enumerate_storage_disks",
                    side_effect=VhdxProbeError(
                        "Windows storage inspection failed.",
                        details="Get-Disk failed: Invalid property",
                    ),
                ),
                patch.object(
                    runner,
                    "_enumerate_win32_disks",
                    side_effect=VhdxProbeError(
                        "Pre-probe disk inventory failed before any VHDX was created.",
                        details="Win32_DiskDrive failed",
                    ),
                ),
                patch.object(runner, "_run_diskpart") as diskpart,
            ):
                result = probe_vhdx_backend(
                    device(),
                    root=root,
                    settings=VhdxProbeSettings(
                        vhdx_size_bytes=1024,
                        free_space_reserve_bytes=0,
                    ),
                    runner=runner,
                    name_factory=lambda: "inventory-failure",
                    elevation_checker=lambda: True,
                )

            self.assertEqual(result.status, PROBE_FAILED)
            self.assertIn("Pre-probe disk inventory failed", result.explanation)
            self.assertIn("Win32_DiskDrive failed", result.diagnostic_details)
            diskpart.assert_not_called()
            self.assertFalse((root / TEST_FOLDER_NAME).exists())

class VhdxBackendAssessmentTests(unittest.TestCase):
    def test_inconclusive_probe_keeps_production_planning_blocked(self) -> None:
        backend = VhdxStorageSpacesBackend()
        plan = SavedRaid10Plan(((1, 2), (3, 4)), "2026-10-10T10:00:00+00:00")
        devices = [device(slot) for slot in range(1, 5)]
        with tempfile.TemporaryDirectory() as directory:
            result = probe_vhdx_backend(
                devices[0], root=Path(directory),
                settings=VhdxProbeSettings(vhdx_size_bytes=1024, free_space_reserve_bytes=0),
                runner=FakeRunner(can_pool=False, size_bytes=512 * 1024**2),
                elevation_checker=lambda: True,
            )
        backend.record_probe(result)
        assessment = backend.assess(plan, devices)
        self.assertEqual(assessment.status, UNKNOWN)
        self.assertIn("inconclusive", assessment.explanation)
        self.assertTrue(any("successfully" in item for item in assessment.requirements))

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

        self.assertEqual(assessment.status, UNKNOWN)
        self.assertTrue(any("larger than 4 GiB" in item for item in assessment.blockers))
        self.assertIn("BLOCKED", assessment.dry_run_text)


if __name__ == "__main__":
    unittest.main()
