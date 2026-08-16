#!/usr/bin/env python3
"""Offline safety tests for the dangerous GB10 multi-stream helper."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("rs-gb10-hil-multistream.py")
SPEC = importlib.util.spec_from_file_location("rs_gb10_hil_multistream", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeFrame:
    def __init__(self, number=1):
        self._number = number

    def get_data(self):
        return b"frame"

    def get_frame_number(self):
        return self._number


class FakeFrameSet:
    def __init__(self, *, complete=True, number=1):
        self.depth = FakeFrame(number)
        self.color = FakeFrame(number) if complete else None

    def get_depth_frame(self):
        return self.depth

    def get_color_frame(self):
        return self.color


class FakeDevice:
    def __init__(self, rs, serial, usb):
        self._rs = rs
        self._serial = serial
        self._usb = usb

    def get_info(self, key):
        if key == self._rs.camera_info.serial_number:
            return self._serial
        if key == self._rs.camera_info.usb_type_descriptor:
            return self._usb
        raise AssertionError(f"unexpected camera info key: {key}")


class FakeProfile:
    def __init__(self, device):
        self._device = device

    def get_device(self):
        return self._device


class FakeConfig:
    def __init__(self):
        self.serial = None
        self.streams = []

    def enable_device(self, serial):
        self.serial = serial

    def enable_stream(self, *args):
        self.streams.append(args)


class FakePipeline:
    def __init__(self, profile, *, frames=None, capture_error=None, stop_error=None):
        self.profile = profile
        self.frames = list(frames or [])
        self.capture_error = capture_error
        self.stop_error = stop_error
        self.started_config = None
        self.stop_calls = 0

    def start(self, config):
        self.started_config = config
        return self.profile

    def wait_for_frames(self, _timeout_ms):
        if self.capture_error is not None:
            raise self.capture_error
        if not self.frames:
            raise AssertionError("fake frame queue exhausted")
        return self.frames.pop(0)

    def poll_for_frames(self):
        return False

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error


def fake_rs(
    *,
    actual_serial="SERIAL",
    usb="3.2",
    frames=None,
    capture_error=None,
    stop_error=None,
):
    rs = types.SimpleNamespace()
    rs.camera_info = types.SimpleNamespace(
        serial_number=object(), usb_type_descriptor=object()
    )
    rs.stream = types.SimpleNamespace(depth=object(), color=object())
    rs.format = types.SimpleNamespace(z16=object(), bgr8=object())
    config = FakeConfig()
    device = FakeDevice(rs, actual_serial, usb)
    pipeline = FakePipeline(
        FakeProfile(device),
        frames=frames,
        capture_error=capture_error,
        stop_error=stop_error,
    )
    rs.context = lambda: object()
    rs.config = lambda: config
    rs.pipeline = lambda _context: pipeline
    rs.align = lambda _stream: types.SimpleNamespace(
        process=lambda frame_set: frame_set
    )
    return rs, config, pipeline


def run_main(rs, serial="SERIAL"):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        mock.patch.dict(sys.modules, {"pyrealsense2": rs}),
        mock.patch.object(MODULE, "WARMUP", 0),
        mock.patch.object(MODULE, "FRAMES", 1),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        rc = MODULE.main(["--serial", serial])
    return rc, json.loads(stdout.getvalue()), stderr.getvalue()


class CliSafetyTests(unittest.TestCase):
    def run_without_realsense_import(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pyrealsense2.py").write_text(
                "raise RuntimeError('pyrealsense2 import attempted')\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = tmp
            return subprocess.run(
                [sys.executable, str(SCRIPT), *args],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

    def test_help_does_not_import_or_open_hardware(self):
        result = self.run_without_realsense_import("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--serial", result.stdout)
        self.assertNotIn("import attempted", result.stderr)

    def test_missing_serial_does_not_import_or_open_hardware(self):
        result = self.run_without_realsense_import()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--serial", result.stderr)
        self.assertNotIn("import attempted", result.stderr)

    def test_unknown_option_does_not_import_or_open_hardware(self):
        result = self.run_without_realsense_import("--serial", "SERIAL", "--unknown")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertNotIn("import attempted", result.stderr)


class RuntimeSafetyTests(unittest.TestCase):
    def test_binds_exact_serial_and_reports_actual_usb(self):
        rs, config, pipeline = fake_rs(frames=[FakeFrameSet()])
        rc, report, _stderr = run_main(rs)
        self.assertEqual(rc, 0, report)
        self.assertEqual(config.serial, "SERIAL")
        self.assertIs(pipeline.started_config, config)
        self.assertEqual(
            report["device"],
            {"requested_serial": "SERIAL", "serial": "SERIAL", "usb": "3.2"},
        )
        self.assertEqual(report["frames"], 1)
        self.assertTrue(report["complete"])

    def test_rejects_actual_serial_mismatch(self):
        rs, _config, pipeline = fake_rs(actual_serial="OTHER")
        rc, report, _stderr = run_main(rs)
        self.assertNotEqual(rc, 0)
        self.assertIn("serial mismatch", report["fatal"]["message"])
        self.assertEqual(pipeline.stop_calls, 1)

    def test_rejects_degraded_usb_link(self):
        rs, _config, pipeline = fake_rs(usb="2.1")
        rc, report, _stderr = run_main(rs)
        self.assertNotEqual(rc, 0)
        self.assertIn("USB 3", report["fatal"]["message"])
        self.assertEqual(pipeline.stop_calls, 1)

    def test_capture_exception_returns_nonzero(self):
        rs, _config, pipeline = fake_rs(capture_error=RuntimeError("capture failed"))
        rc, report, _stderr = run_main(rs)
        self.assertNotEqual(rc, 0)
        self.assertEqual(report["fatal"]["message"], "capture failed")
        self.assertEqual(pipeline.stop_calls, 1)

    def test_incomplete_frame_returns_nonzero(self):
        rs, _config, _pipeline = fake_rs(frames=[FakeFrameSet(complete=False)])
        rc, report, _stderr = run_main(rs)
        self.assertNotEqual(rc, 0)
        self.assertEqual(report["frames"], 0)
        self.assertFalse(report["complete"])

    def test_teardown_exception_returns_nonzero(self):
        rs, _config, pipeline = fake_rs(
            frames=[FakeFrameSet()], stop_error=RuntimeError("stop failed")
        )
        rc, report, _stderr = run_main(rs)
        self.assertNotEqual(rc, 0)
        self.assertEqual(report["teardown_error"]["message"], "stop failed")
        self.assertEqual(pipeline.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
