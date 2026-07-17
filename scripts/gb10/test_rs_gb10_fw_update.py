#!/usr/bin/env python3
"""Unit tests for the GB10 firmware safety wrapper."""

import hashlib
import importlib.util
import io
import os
import stat
import subprocess
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("rs-gb10-fw-update.py")
SPEC = importlib.util.spec_from_file_location("rs_gb10_fw_update", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
REAL_FSTAT = os.fstat


def root_owned_fstat(fd):
    """Return full real metadata with the production root owner for tests."""
    metadata = REAL_FSTAT(fd)
    values = list(metadata)
    values[4] = 0
    return os.stat_result(values)


def trusted_root_fstat(fd):
    """Model root-owned, non-writable directory ancestry in a temp tree."""
    metadata = root_owned_fstat(fd)
    if stat.S_ISDIR(metadata.st_mode):
        values = list(metadata)
        values[0] &= ~(stat.S_IWGRP | stat.S_IWOTH)
        return os.stat_result(values)
    return metadata


@contextmanager
def trusted_backup_test_environment():
    with (
        mock.patch.object(MODULE, "require_firmware_privileges"),
        mock.patch.object(MODULE.os, "fstat", side_effect=trusted_root_fstat),
    ):
        yield


class FirmwareImageTests(unittest.TestCase):
    def test_rejects_non_target_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "Signed_Image_UVC_5_17_0_10.bin"
            image.write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "does not match target"):
                MODULE.validate_flash_image(image)

    def test_rejects_wrong_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "Signed_Image_UVC_5_17_3_10.bin"
            image.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                ValueError, "does not match published artifact"
            ):
                MODULE.validate_flash_image(image)

    def test_accepts_target_with_expected_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "Signed_Image_UVC_5_17_3_10.bin"
            image.write_bytes(b"official-test-fixture")
            expected = hashlib.sha256(image.read_bytes()).hexdigest()
            with mock.patch.object(MODULE, "EXPECTED_IMAGE_SHA256", expected):
                self.assertEqual(MODULE.validate_flash_image(image), MODULE.TARGET)

    def test_verified_copy_is_unchanged_when_source_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "Signed_Image_UVC_5_17_3_10.bin"
            image.write_bytes(b"official-test-fixture")
            expected = hashlib.sha256(image.read_bytes()).hexdigest()
            with mock.patch.object(MODULE, "EXPECTED_IMAGE_SHA256", expected):
                with MODULE.verified_flash_image(image) as (_version, private_image):
                    image.write_bytes(b"replacement")
                    self.assertEqual(
                        Path(private_image).read_bytes(), b"official-test-fixture"
                    )
                    self.assertNotEqual(Path(private_image), image)


class FirmwareBackupTests(unittest.TestCase):
    @staticmethod
    def camera():
        return {
            "serial": "123",
            "fw": "5.15.1.55",
            "usb": "3.2",
            "usb3": True,
            "needs_update": True,
            "physical_port": "/sys/devices/test-camera",
            "firmware_update_id": "404543020690",
        }

    def test_existing_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "123-5.15.1.55.bin"
            backup.write_bytes(b"previous-good-backup")
            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(
                    MODULE.SafetyGateError, "will not be overwritten"
                ):
                    MODULE.backup_firmware("rs-fw-update", "123", "5.15.1.55", {}, tmp)

            self.assertEqual(backup.read_bytes(), b"previous-good-backup")
            self.assertEqual(list(Path(tmp).glob("*.partial-*")), [])
            run.assert_not_called()

    def test_successful_nonempty_backup_is_installed_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"current-firmware")
                return subprocess.CompletedProcess(args, 0)

            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE, "EXPECTED_BACKUP_BYTES", 16),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run),
            ):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertEqual(Path(created).read_bytes(), b"current-firmware")
            self.assertEqual(list(Path(tmp).glob("*.partial-*")), [])

    def test_backup_appearing_during_install_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "123-5.15.1.55.bin"

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"current-firmware")
                return subprocess.CompletedProcess(args, 0)

            def concurrent_create(_source, destination, **_kwargs):
                backup.write_bytes(b"concurrent-backup")
                raise FileExistsError(destination)

            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE, "EXPECTED_BACKUP_BYTES", 16),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run),
                mock.patch.object(MODULE.os, "link", side_effect=concurrent_create),
                self.assertRaisesRegex(MODULE.SafetyGateError, "appeared concurrently"),
            ):
                MODULE.backup_firmware("rs-fw-update", "123", "5.15.1.55", {}, tmp)

            self.assertEqual(backup.read_bytes(), b"concurrent-backup")
            self.assertEqual(list(Path(tmp).glob("*.partial-*")), [])

    def test_success_exit_with_truncated_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE, "EXPECTED_BACKUP_BYTES", 16),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run),
            ):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertIsNone(created)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_backup_failure_diagnostic_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"current-firmware")
                return subprocess.CompletedProcess(
                    args, 0, stdout="Creating backup file failed", stderr=""
                )

            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE, "EXPECTED_BACKUP_BYTES", 16),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run),
            ):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertIsNone(created)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_backup_command_launch_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                trusted_backup_test_environment(),
                mock.patch.object(
                    MODULE.subprocess, "run", side_effect=OSError("cannot execute")
                ),
            ):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertIsNone(created)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_missing_backup_return_never_reaches_flash_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                mock.patch.object(MODULE, "require_camera_idle"),
                mock.patch.object(MODULE, "backup_firmware", return_value=None),
                mock.patch.object(MODULE.subprocess, "run") as run,
            ):
                rc = MODULE.flash_cameras(
                    [self.camera()],
                    MODULE.TARGET,
                    "/verified/image.bin",
                    False,
                    lock_path=Path(tmp) / "host.lock",
                )

        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_invalid_backup_return_never_reaches_flash_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            truncated = Path(tmp) / "123-5.15.1.55.bin"
            truncated.write_bytes(b"truncated")
            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                mock.patch.object(MODULE, "require_camera_idle"),
                mock.patch.object(MODULE, "backup_firmware", return_value=truncated),
                mock.patch.object(MODULE.subprocess, "run") as run,
            ):
                rc = MODULE.flash_cameras(
                    [self.camera()],
                    MODULE.TARGET,
                    "/verified/image.bin",
                    False,
                    lock_path=Path(tmp) / "host.lock",
                )

        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_valid_backup_preserves_flash_command_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "123-5.15.1.55.bin"
            backup.write_bytes(b"complete-backup")
            completed = subprocess.CompletedProcess([], 0)
            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                mock.patch.object(
                    MODULE, "EXPECTED_BACKUP_BYTES", len(backup.read_bytes())
                ),
                mock.patch.object(MODULE, "backup_firmware", return_value=backup),
                mock.patch.object(
                    MODULE, "backup_artifact_is_valid", return_value=True
                ),
                mock.patch.object(MODULE, "require_camera_idle") as idle,
                mock.patch.object(MODULE, "RS_FW_UPDATE", "rs-fw-update"),
                mock.patch.object(
                    MODULE.subprocess, "run", return_value=completed
                ) as run,
            ):
                rc = MODULE.flash_cameras(
                    [self.camera()],
                    MODULE.TARGET,
                    "/verified/image.bin",
                    False,
                    lock_path=Path(tmp) / "host.lock",
                )

        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["rs-fw-update", "-s", "123", "-f", "/verified/image.bin"],
        )
        self.assertEqual(idle.call_count, 2)

    def test_maintenance_lock_covers_backup_and_flash(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "123-5.15.1.55.bin"
            backup.write_bytes(b"complete-backup")

            def assert_lock_held(*_args, **_kwargs):
                with self.assertRaises(MODULE.SafetyGateError):
                    with MODULE.firmware_maintenance_lock(
                        lock_path=Path(tmp) / "host.lock"
                    ):
                        self.fail("maintenance lock was not held")
                return backup

            def assert_lock_held_during_flash(*_args, **_kwargs):
                assert_lock_held()
                return subprocess.CompletedProcess([], 0)

            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                mock.patch.object(
                    MODULE, "EXPECTED_BACKUP_BYTES", len(backup.read_bytes())
                ),
                mock.patch.object(
                    MODULE, "backup_firmware", side_effect=assert_lock_held
                ),
                mock.patch.object(
                    MODULE, "backup_artifact_is_valid", return_value=True
                ),
                mock.patch.object(MODULE, "require_camera_idle"),
                mock.patch.object(
                    MODULE.subprocess,
                    "run",
                    side_effect=assert_lock_held_during_flash,
                ),
            ):
                rc = MODULE.flash_cameras(
                    [self.camera()],
                    MODULE.TARGET,
                    "/verified/image.bin",
                    False,
                    lock_path=Path(tmp) / "host.lock",
                )

        self.assertEqual(rc, 0)

    def test_symlink_backup_is_invalid_even_when_target_has_expected_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.bin"
            target.write_bytes(b"complete-backup")
            link = Path(tmp) / "backup.bin"
            link.symlink_to(target)
            with mock.patch.object(
                MODULE, "EXPECTED_BACKUP_BYTES", len(target.read_bytes())
            ):
                self.assertFalse(MODULE.backup_artifact_is_valid(link))

    def test_world_writable_backup_ancestry_fails_before_updater(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_root = Path(tmp) / "backups"
            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                mock.patch.object(MODULE.subprocess, "run") as run,
                self.assertRaisesRegex(
                    MODULE.SafetyGateError, "unsafe firmware backup directory"
                ),
            ):
                MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, backup_root
                )

        run.assert_not_called()

    def test_symlink_backup_root_fails_before_updater(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            backup_root = Path(tmp) / "backup-link"
            backup_root.symlink_to(target, target_is_directory=True)
            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE.subprocess, "run") as run,
                self.assertRaisesRegex(MODULE.SafetyGateError, "cannot securely open"),
            ):
                MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, backup_root
                )

        run.assert_not_called()

    def test_precreated_partial_symlink_is_never_opened_or_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = "deadbeefdeadbeef"
            partial = Path(tmp) / f".123-5.15.1.55.bin.partial-{os.getpid()}-{token}"
            victim = Path(tmp) / "victim"
            victim.write_bytes(b"do-not-truncate")
            partial.symlink_to(victim)
            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE.secrets, "token_hex", return_value=token),
                mock.patch.object(MODULE.subprocess, "run") as run,
                self.assertRaisesRegex(
                    MODULE.SafetyGateError, "securely create exclusive"
                ),
            ):
                MODULE.backup_firmware("rs-fw-update", "123", "5.15.1.55", {}, tmp)

            self.assertEqual(victim.read_bytes(), b"do-not-truncate")
            self.assertTrue(partial.is_symlink())
            run.assert_not_called()

    def test_same_size_partial_replacement_is_rejected_by_identity(self):
        with tempfile.TemporaryDirectory() as tmp:

            def replace_partial(args, **_kwargs):
                partial = Path(args[-1])
                partial.unlink()
                partial.write_bytes(b"forged-firmware")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with (
                trusted_backup_test_environment(),
                mock.patch.object(MODULE, "EXPECTED_BACKUP_BYTES", 15),
                mock.patch.object(
                    MODULE.subprocess, "run", side_effect=replace_partial
                ),
                self.assertRaisesRegex(MODULE.SafetyGateError, "identity changed"),
            ):
                MODULE.backup_firmware("rs-fw-update", "123", "5.15.1.55", {}, tmp)

            self.assertFalse((Path(tmp) / "123-5.15.1.55.bin").exists())
            replacements = list(Path(tmp).glob(".*.partial-*"))
            self.assertEqual(len(replacements), 1)
            self.assertEqual(replacements[0].read_bytes(), b"forged-firmware")


class SerialSelectionTests(unittest.TestCase):
    @staticmethod
    def fake_rs(serials):
        camera_info = types.SimpleNamespace(
            serial_number="serial",
            firmware_version="firmware",
            usb_type_descriptor="usb",
            physical_port="port",
            firmware_update_id="firmware_update_id",
            name="name",
        )

        class Device:
            def __init__(self, serial):
                self.info = {
                    "serial": serial,
                    "firmware": "5.15.1.55",
                    "usb": "3.2",
                    "port": "/sys/devices/test-camera",
                    "firmware_update_id": "404543020690",
                    "name": "Intel RealSense D435",
                }

            def supports(self, _key):
                return True

            def get_info(self, key):
                return self.info[key]

        devices = [Device(serial) for serial in serials]
        return types.SimpleNamespace(
            camera_info=camera_info,
            context=lambda: types.SimpleNamespace(query_devices=lambda: devices),
        )

    def test_requested_serial_must_match_one_camera(self):
        MODULE.validate_serial_selection([{"serial": "123"}], "123")

    def test_requested_serial_matching_zero_cameras_is_rejected(self):
        with self.assertRaisesRegex(MODULE.SafetyGateError, "matched 0 devices"):
            MODULE.validate_serial_selection([], "missing")

    def test_requested_serial_matching_multiple_cameras_is_rejected(self):
        cameras = [{"serial": "123"}, {"serial": "123"}]
        with self.assertRaisesRegex(MODULE.SafetyGateError, "matched 2 devices"):
            MODULE.validate_serial_selection(cameras, "123")

    def test_no_serial_preserves_multi_camera_mode(self):
        MODULE.validate_serial_selection([{"serial": "1"}, {"serial": "2"}], None)

    def test_main_rejects_requested_serial_with_zero_sdk_matches(self):
        fake_rs = self.fake_rs(["other"])
        with (
            mock.patch.dict(MODULE.sys.modules, {"pyrealsense2": fake_rs}),
            mock.patch.object(MODULE.sys, "argv", [str(SCRIPT), "--serial", "missing"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            rc = MODULE.main()

        self.assertEqual(rc, 2)
        self.assertIn("matched 0 devices", stderr.getvalue())

    def test_main_rejects_requested_serial_with_multiple_sdk_matches(self):
        fake_rs = self.fake_rs(["123", "123"])
        with (
            mock.patch.dict(MODULE.sys.modules, {"pyrealsense2": fake_rs}),
            mock.patch.object(MODULE.sys, "argv", [str(SCRIPT), "--serial", "123"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            rc = MODULE.main()

        self.assertEqual(rc, 2)
        self.assertIn("matched 2 devices", stderr.getvalue())


class PrivilegePreflightTests(unittest.TestCase):
    def test_unprivileged_flash_fails_before_lock_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "must-not-exist.lock"
            with (
                mock.patch.object(MODULE.os, "geteuid", return_value=1000),
                self.assertRaisesRegex(MODULE.SafetyGateError, "effective uid 0"),
            ):
                MODULE.flash_cameras(
                    [], MODULE.TARGET, "/absolute/image.bin", False, lock_path
                )

            self.assertFalse(lock_path.exists())

    def test_effective_root_passes_privilege_preflight(self):
        with mock.patch.object(MODULE.os, "geteuid", return_value=0):
            MODULE.require_firmware_privileges()

    def test_privileged_path_creates_and_holds_root_contract_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "host.lock"
            with (
                mock.patch.object(MODULE.os, "geteuid", return_value=0),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                MODULE.firmware_maintenance_lock(lock_path=lock_path),
            ):
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_main_rejects_unprivileged_flash_before_sdk_import(self):
        with (
            mock.patch.object(
                MODULE.sys,
                "argv",
                [str(SCRIPT), "--flash", "--image", "/absolute/image.bin"],
            ),
            mock.patch.object(MODULE.os, "geteuid", return_value=1000),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            rc = MODULE.main()

        self.assertEqual(rc, 2)
        self.assertIn("effective uid 0", stderr.getvalue())

    def test_exact_fleet_sudo_invocation_has_no_implicit_runtime_paths(self):
        command = MODULE.FLEET_SUDO_0060
        self.assertEqual(command[:3], ("/usr/bin/sudo", "/usr/bin/env", "-i"))
        self.assertNotIn("~", " ".join(command))
        self.assertIn("--serial", command)
        self.assertIn("327122076391", command)
        assignments = [token for token in command if "=" in token]
        for assignment in assignments:
            _name, value = assignment.split("=", 1)
            for path in value.split(":"):
                self.assertTrue(Path(path).is_absolute(), assignment)
        python_index = command.index("/usr/bin/python3.12")
        self.assertTrue(Path(command[python_index]).is_absolute())
        self.assertTrue(Path(command[python_index + 1]).is_absolute())
        image_index = command.index("--image") + 1
        self.assertTrue(Path(command[image_index]).is_absolute())
        self.assertTrue(Path(MODULE.RS_FW_UPDATE).is_absolute())
        self.assertTrue(Path(MODULE.BACKUP_ROOT).is_absolute())
        self.assertEqual(
            Path(MODULE.BACKUP_ROOT),
            Path("/var/lib/vigil/realsense-fw-backups"),
        )
        self.assertIn("LRS_FW_BACKUP_ROOT=/var/lib/vigil/realsense-fw-backups", command)
        self.assertTrue(Path(MODULE.JOURNALCTL).is_absolute())


class MaintenanceLockTests(unittest.TestCase):
    def test_default_lock_is_one_host_wide_run_lock(self):
        self.assertEqual(Path(MODULE.LOCK_PATH).parent, Path("/run/lock"))

    def test_second_nonblocking_lock_is_rejected_until_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "host.lock"
            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
            ):
                with MODULE.firmware_maintenance_lock(lock_path=lock_path):
                    with self.assertRaisesRegex(
                        MODULE.SafetyGateError, "already holds the lock"
                    ):
                        with MODULE.firmware_maintenance_lock(lock_path=lock_path):
                            self.fail("contested lock was acquired")

                with MODULE.firmware_maintenance_lock(lock_path=lock_path):
                    pass

    def test_lock_with_unsafe_permissions_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "host.lock"
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o644)
            with (
                mock.patch.object(MODULE, "require_firmware_privileges"),
                mock.patch.object(MODULE.os, "fstat", side_effect=root_owned_fstat),
                self.assertRaisesRegex(MODULE.SafetyGateError, "root-owned 0600"),
            ):
                with MODULE.firmware_maintenance_lock(lock_path=lock_path):
                    self.fail("unsafe lock metadata was accepted")


class CameraHolderTests(unittest.TestCase):
    @staticmethod
    def fake_camera_filesystem(
        tmp,
        sdk_serial="327122076391",
        firmware_update_id="404543020690",
        physical_port="4-3.2-8",
    ):
        root = Path(tmp)
        sys_root = root / "sys"
        dev_root = root / "dev"
        proc_root = root / "proc"
        usb = sys_root / "bus" / "usb" / "devices" / "4-3.2"
        interface = usb / "4-3.2:1.0"
        port = interface / "video4linux" / "video0"
        port.mkdir(parents=True)
        (usb / "serial").write_text(f"{firmware_update_id}\n")
        (usb / "busnum").write_text("4\n")
        (usb / "devnum").write_text("5\n")

        video_class = sys_root / "class" / "video4linux" / "video0"
        video_class.mkdir(parents=True)
        (video_class / "device").symlink_to(interface, target_is_directory=True)

        usb_node = dev_root / "bus" / "usb" / "004" / "005"
        usb_node.parent.mkdir(parents=True)
        usb_node.touch()
        video_node = dev_root / "video0"
        video_node.parent.mkdir(parents=True, exist_ok=True)
        video_node.touch()
        proc_root.mkdir()
        camera = {
            "serial": sdk_serial,
            "firmware_update_id": firmware_update_id,
            "physical_port": physical_port,
        }
        return camera, sys_root, dev_root, proc_root, video_node

    def test_compact_port_maps_mismatched_0060_sdk_and_usb_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, _proc_root, _video_node = (
                self.fake_camera_filesystem(tmp)
            )
            nodes = MODULE.camera_device_nodes(
                camera, sys_root=sys_root, dev_root=dev_root
            )

        self.assertEqual(len(nodes), 2)

    def test_firmware_update_id_maps_mismatched_3066_ids_without_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, _proc_root, _video_node = (
                self.fake_camera_filesystem(
                    tmp,
                    sdk_serial="347622075921",
                    firmware_update_id="344223022564",
                    physical_port="?",
                )
            )
            nodes = MODULE.camera_device_nodes(
                camera, sys_root=sys_root, dev_root=dev_root
            )

        self.assertEqual(len(nodes), 2)

    def test_open_camera_node_reports_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, proc_root, video_node = (
                self.fake_camera_filesystem(tmp)
            )
            process = proc_root / "4242"
            descriptors = process / "fd"
            descriptors.mkdir(parents=True)
            (process / "cmdline").write_bytes(b"camera-agent\0--stream\0")
            (descriptors / "7").symlink_to(video_node)

            nodes, holders = MODULE.camera_holders(
                camera,
                proc_root=proc_root,
                sys_root=sys_root,
                dev_root=dev_root,
            )

        self.assertEqual(len(nodes), 2)
        self.assertEqual(holders, {4242: "camera-agent --stream"})

    def test_no_open_camera_nodes_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, proc_root, _video_node = (
                self.fake_camera_filesystem(tmp)
            )
            nodes, holders = MODULE.camera_holders(
                camera,
                proc_root=proc_root,
                sys_root=sys_root,
                dev_root=dev_root,
            )

        self.assertEqual(len(nodes), 2)
        self.assertEqual(holders, {})

    def test_missing_usb_device_node_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, _proc_root, _video_node = (
                self.fake_camera_filesystem(tmp)
            )
            (dev_root / "bus" / "usb" / "004" / "005").unlink()
            with self.assertRaisesRegex(MODULE.SafetyGateError, "missing device nodes"):
                MODULE.camera_device_nodes(camera, sys_root=sys_root, dev_root=dev_root)

    def test_inaccessible_userspace_fd_table_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, proc_root, _video_node = (
                self.fake_camera_filesystem(tmp)
            )
            process = proc_root / "4242"
            process.mkdir()
            (process / "fd").write_text("not a directory")
            (process / "stat").write_text("4242 (camera-agent) S 1 1 1 0 0 0\n")

            with self.assertRaisesRegex(
                MODULE.SafetyGateError, "descriptor visibility is incomplete"
            ):
                MODULE.camera_holders(
                    camera,
                    proc_root=proc_root,
                    sys_root=sys_root,
                    dev_root=dev_root,
                )

    def test_inaccessible_kernel_thread_fd_table_is_not_userspace_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera, sys_root, dev_root, proc_root, _video_node = (
                self.fake_camera_filesystem(tmp)
            )
            process = proc_root / "2"
            process.mkdir()
            (process / "fd").write_text("not a directory")
            (process / "stat").write_text(
                f"2 (kthreadd) S 0 0 0 0 0 {MODULE.PF_KTHREAD}\n"
            )

            _nodes, holders = MODULE.camera_holders(
                camera,
                proc_root=proc_root,
                sys_root=sys_root,
                dev_root=dev_root,
            )

        self.assertEqual(holders, {})


class ControllerHealthTests(unittest.TestCase):
    def test_fails_closed_when_kernel_log_is_unavailable(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
        with mock.patch.object(MODULE.subprocess, "run", return_value=result):
            self.assertFalse(MODULE.controller_green())

    def test_scans_full_current_boot_for_fatal_controller_markers(self):
        log = "\n".join(["ordinary kernel line"] * 500 + ["xHCI Host halt failed"])
        result = subprocess.CompletedProcess([], 0, stdout=log, stderr="")
        with mock.patch.object(MODULE.subprocess, "run", return_value=result):
            self.assertFalse(MODULE.controller_green())

    def test_accepts_readable_kernel_log_without_fatal_markers(self):
        result = subprocess.CompletedProcess(
            [], 0, stdout="usb device attached at SuperSpeed", stderr=""
        )
        with mock.patch.object(MODULE.subprocess, "run", return_value=result):
            self.assertTrue(MODULE.controller_green())


if __name__ == "__main__":
    unittest.main()
