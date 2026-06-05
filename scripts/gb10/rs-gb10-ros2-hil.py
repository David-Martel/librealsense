#!/usr/bin/env python3
"""HIL wrapper for the PROVEN GB10 ROS2 depth-only launch.

Two modes — the camera path requires explicit --live so it can NEVER run by accident:

  --parse-log FILE      Parse an existing node log; print metrics + PASS/FAIL.
                        No hardware. No ROS2. Safe for CI and offline verification.

  --live [--duration N] Run the verified ros2-launch-depth-minimal.sh for N seconds
                        (default 25), tee output to the artifact dir, parse the captured
                        log, write result.json, print a human summary.
                        Requires the operator at the console (one fragile xHCI camera).

  --self-test           Verify the parser against the proven 2026-06-05 HIL log.
                        Must print ~708 frames / ~30 fps / 0 drops / 2 idx768 / 0 fatal.

No arguments: print this usage and exit 0 (never opens the camera).

Proven config (HIL 2026-06-05):
  708 depth frames Z16 848x480 @ 30.03 fps, 0 drops, 2 idx768-EAGAIN, 0 fatal, controller GREEN.
  Log: ~/realsense-gb10-validation/ros2-depth-minimal-20260605-134449.log

PASS iff: frames>0, fps within ±2 of 30, 0 fatal markers, controller GREEN (live only).
idx768-EAGAIN and dropped frames are REPORT-ONLY (benign at <=2; gate does NOT gate on these).
"""
# stdlib only; hil_common is imported only inside cmd_live() to keep --parse-log dep-free.
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCH_SCRIPT = os.path.join(REPO_ROOT, "scripts", "gb10", "ros2-launch-depth-minimal.sh")
VALIDATION_DIR = os.environ.get(
    "LRS_VALIDATION_DIR",
    os.path.expanduser("~/realsense-gb10-validation"),
)
SELF_TEST_LOG = os.path.join(
    VALIDATION_DIR, "ros2-depth-minimal-20260605-134449.log"
)
# Accepted FPS band — generous: the proven run measured 30.03 fps.
FPS_TARGET = 30.0
FPS_TOLERANCE = 2.0  # ±2 fps accepted

# Fatal markers: lines in node output that indicate a hard failure.
_FATAL_PATTERNS = [
    re.compile(r"Hardware Error", re.IGNORECASE),
    re.compile(r"error:\s*-110"),
    re.compile(r"failed to (?:start|open|resolve)", re.IGNORECASE),
    re.compile(r"terminate called"),
    re.compile(r"what\(\):"),
]

# Frame line pattern:  FrameAccepted,Depth,Counter,<N>,Index,0,...,SystemTime,<ms>,...
_FRAME_RE = re.compile(
    r"FrameAccepted,Depth,Counter,(\d+),Index,0,BackEndTS,[^,]+,SystemTime,([\d.]+)"
)

# idx768 benign control warning
_IDX768_RE = re.compile(
    r"control_transfer returned error, index: 768.*number: 11"
)

# Stream-open confirmation
_STREAM_OPEN_RE = re.compile(
    r"Open profile: stream_type: Depth\(0\), Format: Z16, Width: 848, Height: 480, FPS: 30"
)


# ---------------------------------------------------------------------------
# Pure-stdlib parser — safe for offline/CI; no camera, no ROS2, no numpy.
# ---------------------------------------------------------------------------
def parse_log(path: str) -> dict:
    """Parse a realsense2_camera node log and return a metrics dict.

    Returns:
      {
        "log_path": str,
        "stream_confirmed": bool,     # saw "Open profile: Depth Z16 848x480 30"
        "frame_count": int,
        "fps": float | None,          # first-to-last SystemTime; None if <2 frames
        "dropped_frames": int,        # max(Counter)+1 - received  (>=0)
        "idx768_eagain": int,         # benign startup EAGAIN count
        "fatal_lines": list[str],     # lines matching fatal markers
        "fatal_count": int,
        "pass": bool,                 # frames>0 AND fps in band AND fatal==0
        "fail_reason": str | None,
      }
    """
    counters = []
    sys_times = []
    idx768_count = 0
    fatal_lines = []
    stream_confirmed = False

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _FRAME_RE.search(line)
            if m:
                counters.append(int(m.group(1)))
                sys_times.append(float(m.group(2)))
                continue
            if _IDX768_RE.search(line):
                idx768_count += 1
                continue
            if _STREAM_OPEN_RE.search(line):
                stream_confirmed = True
                continue
            for pat in _FATAL_PATTERNS:
                if pat.search(line):
                    fatal_lines.append(line.rstrip())
                    break

    frame_count = len(counters)

    fps = None
    if frame_count >= 2:
        duration_sec = (sys_times[-1] - sys_times[0]) / 1000.0
        fps = round(frame_count / duration_sec, 2) if duration_sec > 0 else None

    dropped = 0
    if counters:
        expected = max(counters) + 1
        dropped = max(0, expected - frame_count)

    fatal_count = len(fatal_lines)

    # PASS gate: four conditions; idx768 and drops are report-only.
    fail_reason = None
    if frame_count == 0:
        fail_reason = "no depth frames received"
    elif fps is None:
        fail_reason = "could not compute fps (fewer than 2 frames)"
    elif abs(fps - FPS_TARGET) > FPS_TOLERANCE:
        fail_reason = f"fps {fps:.2f} outside {FPS_TARGET}±{FPS_TOLERANCE}"
    elif fatal_count > 0:
        fail_reason = f"{fatal_count} fatal marker(s) in log"

    return {
        "log_path": os.path.abspath(path),
        "stream_confirmed": stream_confirmed,
        "frame_count": frame_count,
        "fps": fps,
        "dropped_frames": dropped,
        "idx768_eagain": idx768_count,
        "fatal_lines": fatal_lines,
        "fatal_count": fatal_count,
        "pass": fail_reason is None,
        "fail_reason": fail_reason,
    }


def _human_summary(m: dict, controller_green: bool | None = None) -> str:
    """One-line human summary from metrics dict."""
    status = "PASS" if m["pass"] and (controller_green is not False) else "FAIL"
    ctrl = "" if controller_green is None else f" | ctrl {'GREEN' if controller_green else 'DEAD'}"
    fps_s = f"{m['fps']:.2f}" if m["fps"] is not None else "n/a"
    reason = f" ({m['fail_reason']})" if m["fail_reason"] else ""
    if not m["pass"] and controller_green is False:
        reason = reason or " (controller death)"
    return (
        f"[ros2-hil] {status}{reason}{ctrl} | "
        f"frames={m['frame_count']} fps={fps_s} "
        f"drops={m['dropped_frames']} idx768={m['idx768_eagain']} "
        f"fatal={m['fatal_count']} "
        f"stream_confirmed={m['stream_confirmed']}"
    )


# ---------------------------------------------------------------------------
# --parse-log mode: offline, CI-safe
# ---------------------------------------------------------------------------
def cmd_parse_log(args):
    if not os.path.isfile(args.parse_log):
        print(f"[ros2-hil] ERROR: log not found: {args.parse_log}", file=sys.stderr)
        return 2

    m = parse_log(args.parse_log)
    print(json.dumps(m, indent=2))
    print(_human_summary(m))
    return 0 if m["pass"] else 1


# ---------------------------------------------------------------------------
# --self-test mode: validate parser against the proven 2026-06-05 HIL log
# ---------------------------------------------------------------------------
def cmd_self_test(_args):
    """Run --parse-log against the proven log; assert expected values."""
    log = SELF_TEST_LOG
    if not os.path.isfile(log):
        print(f"[ros2-hil] SELF-TEST SKIP: proven log not found: {log}")
        print("[ros2-hil] (run live HIL first to generate it)")
        return 0  # not a CI failure — log is out-of-tree

    m = parse_log(log)
    print(json.dumps(m, indent=2))
    print()

    # Expected values from the proven 2026-06-05 HIL run
    errs = []
    if m["frame_count"] != 708:
        errs.append(f"frame_count: expected 708, got {m['frame_count']}")
    if m["fps"] is None or not (29.0 <= m["fps"] <= 31.0):
        errs.append(f"fps: expected ~30, got {m['fps']}")
    if m["dropped_frames"] != 0:
        errs.append(f"dropped_frames: expected 0, got {m['dropped_frames']}")
    if m["idx768_eagain"] != 2:
        errs.append(f"idx768_eagain: expected 2, got {m['idx768_eagain']}")
    if m["fatal_count"] != 0:
        errs.append(f"fatal_count: expected 0, got {m['fatal_count']}")
    if not m["pass"]:
        errs.append(f"pass: expected True, got False (reason: {m['fail_reason']})")
    if not m["stream_confirmed"]:
        errs.append("stream_confirmed: expected True")

    if errs:
        print("[ros2-hil] SELF-TEST FAIL:")
        for e in errs:
            print(f"  - {e}")
        return 1

    print(_human_summary(m))
    print("[ros2-hil] SELF-TEST PASS: all metrics match proven 2026-06-05 HIL baseline")
    return 0


# ---------------------------------------------------------------------------
# --live mode: OPERATOR ONLY — requires D435 on a healthy USB-3 port
# ---------------------------------------------------------------------------
def _preflight_ros2(hil):
    """Lightweight ROS2 pre-flight: journalctl + lsusb only; NO pyrealsense2/device open.

    Checks:
      1. Controller not already dead (journalctl -k last 80 lines).
      2. D435 visible on USB bus (lsusb 0x0b07 / Intel RealSense).
      3. Camera not held by another process (pgrep realsense2_camera_node).
    """
    from hil_common import Tripwire  # noqa: import inside live branch only

    j = subprocess.run(
        ["bash", "-c", "journalctl -k --no-pager -n 80 2>/dev/null"],
        capture_output=True, text=True,
    ).stdout
    if any(("HC died" in ln) or ("not responding to stop" in ln) for ln in j.splitlines()):
        raise Tripwire("pre-flight: controller already dead (reboot required)")

    lsusb = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    if "8086:0b07" not in lsusb.lower() and "realsense" not in lsusb.lower() and "0b07" not in lsusb:
        hil.report["preflight_warning"] = "D435 (8086:0b07) not seen in lsusb output"
        print("[ros2-hil] WARNING: D435 not confirmed in lsusb — camera may be absent or on USB-2")

    existing = subprocess.run(
        ["pgrep", "-f", "realsense2_camera_node"],
        capture_output=True, text=True,
    ).returncode
    if existing == 0:
        raise Tripwire(
            "pre-flight: realsense2_camera_node already running; "
            "single-process rule violated — stop existing process first"
        )


def cmd_live(args):
    """Launch ros2-launch-depth-minimal.sh for --duration seconds, tee output, parse + emit result.

    Extra ros2 launch params (args.launch_args) are forwarded to the launch script verbatim — this is
    how the single-variable A/B is driven, e.g. `--live depth_module.exposure:=8500` re-introduces one
    suspect on top of the proven minimal config. args.label tags the artifact dir for easy comparison.
    """
    # Import hil_common only here (needs numpy in live env, not in parse-log/CI)
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "gb10"))
    from hil_common import HIL, Tripwire  # noqa: late import

    launch_args = list(args.launch_args or [])
    hil = HIL(args.label or "ros2-depth-only")
    hil.report["launch_script"] = LAUNCH_SCRIPT
    hil.report["launch_args"] = launch_args
    hil.report["duration_secs"] = args.duration

    try:
        _preflight_ros2(hil)
    except Tripwire as t:
        hil.report["result"] = f"PREFLIGHT FAIL: {t}"
        rc = hil.finish(ok=False)
        print(f"[ros2-hil] PREFLIGHT FAIL: {t}", file=sys.stderr)
        return rc

    node_log = os.path.join(hil.dir, "node.log")
    hil.report["node_log"] = node_log

    cmd = ["bash", LAUNCH_SCRIPT] + launch_args
    print(f"[ros2-hil] Launching {LAUNCH_SCRIPT} (duration={args.duration}s)")
    if launch_args:
        print(f"[ros2-hil] Extra params (A/B): {' '.join(launch_args)}")
    print(f"[ros2-hil] Node log: {node_log}")
    print("[ros2-hil] Tripwire armed. SIGINT to the process group at the end of the bounded window.")

    rc = 1
    try:
        # Launch in its OWN session/process group so the SIGINT reaches the whole ros2 launch tree
        # (bash -> ros2 launch -> node), not just the bash PID — robust teardown that actually
        # releases the camera. SIGINT (Ctrl-C equivalent) triggers ros2 launch's clean shutdown.
        with open(node_log, "w") as log_fh:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            t0 = time.monotonic()
            tripwire_interval = 5  # poll journal every 5 s
            last_tripwire = t0

            def _signal_group(sig):
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

            try:
                for line in proc.stdout:
                    log_fh.write(line)
                    log_fh.flush()
                    now = time.monotonic()
                    elapsed = now - t0

                    if now - last_tripwire >= tripwire_interval:
                        hil.check_tripwire(minus110_too=False)
                        last_tripwire = now

                    if elapsed >= args.duration:
                        # Send SIGINT to the whole group for clean ROS2 shutdown (matches proven run)
                        print(f"\n[ros2-hil] Duration {args.duration}s reached; SIGINT -> process group...")
                        _signal_group(signal.SIGINT)
                        break
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _signal_group(signal.SIGKILL)
                    proc.wait()

        # Final tripwire check
        hil.check_tripwire(minus110_too=True)
        controller_green = True

    except Tripwire as t:
        hil.report["tripwire"] = str(t)
        hil.report["result"] = f"ABORTED: {t}"
        controller_green = False
        print(f"[ros2-hil] TRIPWIRE: {t}", file=sys.stderr)

    # Parse the captured node log
    if os.path.isfile(node_log) and os.path.getsize(node_log) > 0:
        m = parse_log(node_log)
        hil.report.update({
            "stream_confirmed": m["stream_confirmed"],
            "frame_count": m["frame_count"],
            "fps": m["fps"],
            "dropped_frames": m["dropped_frames"],
            "idx768_eagain": m["idx768_eagain"],
            "fatal_count": m["fatal_count"],
            "fatal_lines": m["fatal_lines"],
        })
        overall_pass = m["pass"] and controller_green and not hil.report.get("controller_death")
        hil.report["result"] = "PASS" if overall_pass else f"FAIL: {m['fail_reason'] or 'controller/tripwire'}"
        print(_human_summary(m, controller_green=controller_green))
        rc = 0 if overall_pass else 1
    else:
        hil.report["result"] = "FAIL: no node log captured"
        print("[ros2-hil] FAIL: no node log captured", file=sys.stderr)
        rc = 1

    hil.finish(ok=(rc == 0))
    return rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        prog="rs-gb10-ros2-hil.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--parse-log",
        metavar="FILE",
        help="Parse an existing node log and print metrics/PASS/FAIL. No camera.",
    )
    g.add_argument(
        "--live",
        action="store_true",
        help="Launch ros2-launch-depth-minimal.sh (OPERATOR; requires camera + ROS2).",
    )
    g.add_argument(
        "--self-test",
        action="store_true",
        help="Run parser self-test against the proven 2026-06-05 HIL log.",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=25,
        metavar="N",
        help="Bounded run duration in seconds for --live (default: 25).",
    )
    p.add_argument(
        "--label",
        metavar="TAG",
        help="Tag the --live artifact dir (e.g. 'ab-exposure8500') for A/B comparison.",
    )
    p.add_argument(
        "launch_args",
        nargs="*",
        metavar="ROS2_PARAM",
        help="Extra ros2 launch params forwarded to the launch script for --live, e.g. "
        "depth_module.exposure:=8500 (drives the single-variable A/B on the proven minimal config).",
    )

    if len(sys.argv) == 1:
        p.print_help()
        return 0

    args = p.parse_args()

    if args.parse_log:
        return cmd_parse_log(args)
    if args.self_test:
        return cmd_self_test(args)
    if args.live:
        return cmd_live(args)

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
