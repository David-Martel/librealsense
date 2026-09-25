#!/usr/bin/env python3
"""Unit tests for the GB10 calibration-table backup script.

The script talks to a camera at import time, so each test runs it with
``runpy`` against a fake ``pyrealsense2`` injected into ``sys.modules``. No
hardware is touched.
"""

import io
import runpy
import struct
import sys
import tempfile
import types
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("rs-gb10-calib-backup.py")


def table(table_type, payload=b"\x01\x02\x03\x04" * 8):
    """A well-formed 16-byte table_header followed by its payload."""
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return struct.pack("<HHIII", 0x0200, table_type, len(payload), 0, crc) + payload


def fake_rs(depth_raw, rgb_raw):
    """A minimal pyrealsense2 stand-in serving the given raw table bytes."""
    dev = mock.Mock()
    dev.get_info.side_effect = lambda info: {"sn": "000000000000", "fw": "5.17.3.10"}[
        info
    ]

    rs = types.ModuleType("pyrealsense2")
    rs.camera_info = types.SimpleNamespace(serial_number="sn", firmware_version="fw")
    rs.context = lambda: types.SimpleNamespace(query_devices=lambda: [dev])
    rs.auto_calibrated_device = lambda _dev: types.SimpleNamespace(
        get_calibration_table=lambda: list(depth_raw)
    )
    rs.debug_protocol = lambda _dev: types.SimpleNamespace(
        build_command=lambda *_args: b"cmd",
        send_and_receive_raw_data=lambda _cmd: list(struct.pack("<i", 0x15) + rgb_raw),
    )
    return rs


def run_script(rs, out_dir):
    """Run the script once; return (stdout, files written)."""
    stdout = io.StringIO()
    with (
        mock.patch.dict(sys.modules, {"pyrealsense2": rs}),
        mock.patch.object(sys, "argv", [str(SCRIPT), out_dir]),
        redirect_stdout(stdout),
    ):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    return stdout.getvalue(), sorted(p.name for p in Path(out_dir).iterdir())


class CalibBackupTests(unittest.TestCase):
    def test_both_tables_valid_writes_both(self):
        """Positive control: a clean device yields two backups."""
        with tempfile.TemporaryDirectory() as out:
            text, files = run_script(fake_rs(table(25), table(32)), out)
        self.assertIn("RESULT: 2 of 2 tables backed up", text)
        self.assertEqual(len(files), 2)

    def test_malformed_rgb_still_writes_the_valid_depth_backup(self):
        """An RGB table failing validation must not cost the depth backup."""
        bad_rgb = table(99)  # wrong table_type -> split_and_check refuses it
        with tempfile.TemporaryDirectory() as out:
            text, files = run_script(fake_rs(table(25), bad_rgb), out)
        self.assertIn("rgb (t32): FAILED", text)
        self.assertIn("RESULT: 1 of 2 tables backed up", text)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("_depth_t25.bin"))

    def test_bad_depth_crc_still_refuses_outright(self):
        """Depth is the backup that matters: a bad one must still abort."""
        depth = bytearray(table(25))
        depth[-1] ^= 0xFF  # corrupt the payload so the stored CRC no longer matches
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(SystemExit) as ctx:
                run_script(fake_rs(bytes(depth), table(32)), out)
            self.assertIn("CRC MISMATCH", str(ctx.exception))
            self.assertEqual(list(Path(out).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
