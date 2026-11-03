# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
"""Exercise the CI USB-enumeration guard without enumerating real devices."""

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/gb10-lint.yaml"
GUARD_STEP = "Refuse to run on a host with a RealSense attached"
ABSENCE = "No matching RealSense device visible to this runner"


def guard_source(workflow_text):
    """Extract exactly one named inline shell step, refusing ambiguous input."""
    lines = workflow_text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line.strip() == f"- name: {GUARD_STEP}"
    ]
    if len(starts) != 1:
        raise ValueError(f"Expected exactly one {GUARD_STEP!r} step")
    start = starts[0]
    step_indent = len(lines[start]) - len(lines[start].lstrip())
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip()
            and len(lines[index]) - len(lines[index].lstrip()) <= step_indent
        ),
        len(lines),
    )
    run_lines = [
        index for index in range(start + 1, end) if lines[index].strip() == "run: |"
    ]
    if len(run_lines) != 1:
        raise ValueError("Expected exactly one inline run block in the guard")
    run_index = run_lines[0]
    shell_indent = len(lines[run_index]) - len(lines[run_index].lstrip()) + 2
    block = []
    for line in lines[run_index + 1 : end]:
        if line.strip() and len(line) - len(line.lstrip()) < shell_indent:
            break
        block.append(line[shell_indent:])
    if not any(line.strip() for line in block):
        raise ValueError("The guard shell block is empty")
    return "\n".join(block).rstrip() + "\n"


class WorkflowGuardTests(unittest.TestCase):
    def test_enumeration_outcomes(self):
        """Protects: fail-closed CI admission in the runner-visible USB namespace.

        Detects: missing/error enumeration admitted as absence, dropped camera
        IDs, matcher-command failures, case sensitivity, SIGPIPE and host claims.
        Needs: Python standard library and Bash; no SDK, camera or GPU.
        Breadcrumb: .github/workflows/gb10-lint.yaml, named GUARD_STEP above.
        Group: missing, error, empty, unrelated, 0b07, 0b3a, 0b64, mixed, long,
        no_matcher and broken_matcher. Real grep supports old-baseline controls.
        """
        bash = shutil.which("bash")
        grep = shutil.which("grep")
        self.assertIsNotNone(bash, "Needs Bash; this is a Linux CI shell test")
        guard = guard_source(WORKFLOW.read_text(encoding="utf-8"))
        camera = "Bus 001 Device 002: ID 8086:0b07 Intel RealSense\n"
        cases = [
            ("missing", None, 0, False, "real"),
            ("error", "", 1, False, "real"),
            ("empty", "", 0, True, "real"),
            ("unrelated", "Bus 001 Device 001: ID 1234:5678 Other\n", 0, True, "real"),
            ("0b07", camera, 0, False, "real"),
            ("0b3a", camera.replace("0b07", "0b3a"), 0, False, "real"),
            ("0b64", camera.replace("0b07", "0b64"), 0, False, "real"),
            ("mixed", camera.replace("0b07", "0B3A"), 0, False, "real"),
            (
                "long",
                camera + "Bus 002 Device 001: ID 1234:5678 Other\n" * 5000,
                0,
                False,
                "real",
            ),
            ("no_matcher", camera, 0, False, "missing"),
            ("broken_matcher", camera, 0, False, "broken"),
        ]
        for name, listing, status, admitted, matcher in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                if matcher == "real" and grep is not None:
                    (root / "grep").symlink_to(grep)
                elif matcher == "broken":
                    broken = root / "grep"
                    broken.write_text(f"#!{bash}\nexit 2\n", encoding="utf-8")
                    broken.chmod(0o755)
                if listing is not None:
                    stub = root / "lsusb"
                    stub.write_text(
                        f"#!{bash}\nprintf '%s' {shlex.quote(listing)}\n"
                        + ("echo enumerator-failed >&2\n" if status else "")
                        + f"exit {status}\n",
                        encoding="utf-8",
                    )
                    stub.chmod(0o755)
                result = subprocess.run(
                    [bash, "--noprofile", "--norc", "-c", guard],
                    cwd=root,
                    env={"PATH": str(root), "HOME": str(root), "LC_ALL": "C"},
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                detail = (result.stdout + result.stderr)[:2000]
                if admitted:
                    self.assertEqual(result.returncode, 0, detail)
                    self.assertIn(ABSENCE, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0, detail)
                    self.assertNotIn(ABSENCE, result.stdout)
                    if name == "error":
                        self.assertIn("enumerator-failed", result.stderr)
                    elif name == "missing":
                        self.assertIn("lsusb", result.stderr)
                    else:
                        self.assertIn("::error::", result.stdout)
                self.assertNotIn("safe to lint", result.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
