#!/usr/bin/env python3
"""Unit tests for the GB10 firmware safety wrapper."""

import hashlib
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("rs-gb10-fw-update.py")
SPEC = importlib.util.spec_from_file_location("rs_gb10_fw_update", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


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
        }

    def test_failed_backup_never_replaces_existing_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "123-5.15.1.55.bin"
            backup.write_bytes(b"previous-good-backup")
            result = subprocess.CompletedProcess([], 1)
            with mock.patch.object(MODULE.subprocess, "run", return_value=result):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertIsNone(created)
            self.assertEqual(backup.read_bytes(), b"previous-good-backup")
            self.assertEqual(list(Path(tmp).glob("*.partial-*")), [])

    def test_successful_nonempty_backup_is_installed_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"current-firmware")
                return subprocess.CompletedProcess(args, 0)

            with (
                mock.patch.object(MODULE, "EXPECTED_BACKUP_BYTES", 16),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run),
            ):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertEqual(Path(created).read_bytes(), b"current-firmware")
            self.assertEqual(list(Path(tmp).glob("*.partial-*")), [])

    def test_success_exit_with_truncated_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with (
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
            with mock.patch.object(
                MODULE.subprocess, "run", side_effect=OSError("cannot execute")
            ):
                created = MODULE.backup_firmware(
                    "rs-fw-update", "123", "5.15.1.55", {}, tmp
                )

            self.assertIsNone(created)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_missing_backup_return_never_reaches_flash_command(self):
        with (
            mock.patch.object(MODULE, "backup_firmware", return_value=None),
            mock.patch.object(MODULE.subprocess, "run") as run,
        ):
            rc = MODULE.flash_cameras(
                [self.camera()], MODULE.TARGET, "/verified/image.bin", False
            )

        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_invalid_backup_return_never_reaches_flash_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            truncated = Path(tmp) / "123-5.15.1.55.bin"
            truncated.write_bytes(b"truncated")
            with (
                mock.patch.object(MODULE, "backup_firmware", return_value=truncated),
                mock.patch.object(MODULE.subprocess, "run") as run,
            ):
                rc = MODULE.flash_cameras(
                    [self.camera()], MODULE.TARGET, "/verified/image.bin", False
                )

        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_valid_backup_preserves_flash_command_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "123-5.15.1.55.bin"
            backup.write_bytes(b"complete-backup")
            completed = subprocess.CompletedProcess([], 0)
            with (
                mock.patch.object(
                    MODULE, "EXPECTED_BACKUP_BYTES", len(backup.read_bytes())
                ),
                mock.patch.object(MODULE, "backup_firmware", return_value=backup),
                mock.patch.object(MODULE, "RS_FW_UPDATE", "rs-fw-update"),
                mock.patch.object(
                    MODULE.subprocess, "run", return_value=completed
                ) as run,
            ):
                rc = MODULE.flash_cameras(
                    [self.camera()], MODULE.TARGET, "/verified/image.bin", False
                )

        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            ["rs-fw-update", "-s", "123", "-f", "/verified/image.bin"],
        )

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
