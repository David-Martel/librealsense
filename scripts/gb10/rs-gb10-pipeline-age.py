#!/usr/bin/env python3
"""Measure how OLD a frame is at each hop of the live pipeline.

WHY THIS EXISTS
---------------
Every latency number this fleet has is single-stage, and each was measured for
reasons that made local sense: camera delivery jitter (1.4 ms p99-p50), SAM3
sidecar request-in to response-out, acoustic-window metrics against source
stamps. None of them describes what an operator experiences, which is how three
people can each be locally right and collectively aimed at the wrong layer.

The operator-facing quantity is how stale a frame is when it reaches the glass,
and -- because humans notice randomly timed events far more than slow ones --
specifically the SPREAD of that staleness. So this reports the tail, and the
headline figure is p99 - p50.

WHY AGE, AND NOT A CROSS-NODE JOIN
----------------------------------
The obvious design is to stamp a frame at capture, stamp it again at render,
and subtract. That needs a per-frame join key shared across nodes, and there
isn't one: the display's `content_status` is per-PANE (render_submit_count,
paint_state, _last_render_submit_monotonic), not per-frame.

Age needs no join. Every message already carries its capture time in
`header.stamp`, so `now - header.stamp` at any subscriber IS the accumulated
latency from capture to that point, computed in one process against one clock.
Subscribing to several topics along the chain gives the per-stage breakdown for
free: the stage that owns the variance is the one where the age tail jumps.

WHAT THIS DOES NOT MEASURE
--------------------------
The final hop, render submission to photons. That needs a screen witness, and
on this Wayland fleet X11 capture returns uniform zero rather than failing, so
a naive instrument there reports success while measuring nothing (see
tools/eti/display_witness.py, which carries a positive control for exactly
that). It is deliberately excluded and it is the right thing to exclude for a
JITTER question: the compositor presents on a fixed refresh cadence, so it adds
a roughly constant latency and at most one refresh interval of spread. The
variance lives upstream, which is what this measures.

FAILURE MODES ARE EXPLICIT
--------------------------
A measurement tool that reports a confident number from a broken setup is worse
than one that fails. This refuses rather than guesses:

  no messages          which topic, how long waited, what IS being published
  unstamped messages   counted and reported, never treated as age 0
  negative ages        clock skew between publisher and subscriber host,
                       reported as a defect -- NOT clamped, NOT averaged away
"""

from __future__ import annotations

import argparse
import json
import faulthandler
import math
import os
import statistics
import sys
import time


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank. Never interpolated: at p99.9 an interpolated value blends
    two real observations and can report a latency no frame ever had."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(name: str, ages: list[float]) -> dict:
    if not ages:
        return {"topic": name, "n": 0}
    p50 = percentile(ages, 50)
    p99 = percentile(ages, 99)
    return {
        "topic": name,
        "n": len(ages),
        "p50": p50,
        "p90": percentile(ages, 90),
        "p99": p99,
        "p99_9": percentile(ages, 99.9),
        "max": max(ages),
        "stddev": statistics.pstdev(ages) if len(ages) > 1 else 0.0,
        "jitter_p99_minus_p50": p99 - p50,
    }


def _exit(code: int) -> None:
    """Terminate immediately with `code`, without waiting on rclpy teardown.

    MEASURED on spark-3066 (2026-09-10): this script produces its report
    correctly and then never exits -- `rclpy.shutdown()` after a Node has been
    created blocks indefinitely under rmw_cyclonedds_cpp, reproducibly, three
    runs out of three, surviving SIGTERM and needing SIGKILL.

    A measurement tool that prints the right answer and then wedges is broken:
    it hangs a CI step, and an operator who sees no prompt cannot tell a hung
    teardown from a hung measurement. This process holds nothing the kernel
    will not reclaim -- no partial file (the JSON is closed before this runs),
    no lock, no remote state -- so exiting hard is the correct trade, and it
    makes the exit code trustworthy, which is the whole contract of this tool.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "topics",
        nargs="+",
        help="topics along the chain, in pipeline order (camera first)",
    )
    ap.add_argument("--duration-s", type=float, default=60.0)
    ap.add_argument("--warmup-s", type=float, default=5.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    # A measurement tool must never hang. If anything blocks -- rclpy teardown
    # under cyclonedds reproducibly does on this fleet -- dump a stack and die
    # rather than sit there, because a hung tool is indistinguishable from a
    # hung pipeline and wedges any CI step that calls it.
    budget = args.warmup_s + args.duration_s + 60.0
    faulthandler.dump_traceback_later(budget, exit=True)

    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from rosidl_runtime_py.utilities import get_message as resolve_msg
    except ImportError as exc:
        # Distinguish "no ROS here" from "ROS is here but this import is wrong".
        # Conflating them sends the reader to re-source a setup file that is
        # already sourced, which is what the first version of this message did.
        missing = getattr(exc, "name", "") or ""
        if missing.split(".")[0] in ("rclpy", "rosidl_runtime_py"):
            print(f"ROS 2 not importable: {exc}", file=sys.stderr)
            print("source /opt/ros/jazzy/setup.bash first", file=sys.stderr)
        else:
            print(f"ROS 2 is present but an import failed: {exc}", file=sys.stderr)
            print(
                "This is a bug in this script or an API change, NOT a missing "
                "ROS install -- do not re-source and retry.",
                file=sys.stderr,
            )
        _exit(2)

    rclpy.init()
    node = Node("gb10_pipeline_age")

    ages: dict[str, list[float]] = {t: [] for t in args.topics}
    unstamped: dict[str, int] = {t: 0 for t in args.topics}
    negative: dict[str, int] = {t: 0 for t in args.topics}
    start = time.monotonic()

    def make_cb(topic: str):
        def cb(msg) -> None:
            if time.monotonic() - start < args.warmup_s:
                return
            hdr = getattr(msg, "header", None)
            stamp = getattr(hdr, "stamp", None) if hdr is not None else None
            if stamp is None:
                unstamped[topic] += 1
                return
            stamp_ns = stamp.sec * 1_000_000_000 + stamp.nanosec
            if stamp_ns == 0:
                unstamped[topic] += 1
                return
            now_ns = node.get_clock().now().nanoseconds
            age_ms = (now_ns - stamp_ns) / 1e6
            if age_ms < 0:
                # Publisher's clock is ahead of ours. Averaging this away would
                # silently understate every number in the report.
                negative[topic] += 1
                return
            ages[topic].append(age_ms)

        return cb

    # Resolve each topic's type from the live graph; a topic nobody publishes
    # is a setup error worth naming, not a silent zero-sample row.
    time.sleep(2.0)
    live = dict(node.get_topic_names_and_types())
    missing = [t for t in args.topics if t not in live]
    if missing:
        print("REFUSING: these topics are not on the graph:", file=sys.stderr)
        for t in missing:
            print(f"  {t}", file=sys.stderr)
        print("\nCurrently published topics:", file=sys.stderr)
        for t in sorted(live):
            print(f"  {t}", file=sys.stderr)
        _exit(3)

    for t in args.topics:
        node.create_subscription(
            resolve_msg(live[t][0]), t, make_cb(t), qos_profile_sensor_data
        )

    deadline = time.monotonic() + args.warmup_s + args.duration_s
    while rclpy.ok() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.2)

    result = {"topics": [summarize(t, ages[t]) for t in args.topics]}
    silent = [t for t in args.topics if not ages[t]]

    print(
        f"{'topic':38s} {'n':>6s} {'p50':>9s} {'p99':>9s} "
        f"{'p99.9':>9s} {'max':>9s} {'jitter':>9s}"
    )
    prev = None
    for row in result["topics"]:
        if not row["n"]:
            print(f"{row['topic']:38s} {'0':>6s}   NO STAMPED MESSAGES RECEIVED")
            continue
        print(
            f"{row['topic']:38s} {row['n']:6d} {row['p50']:9.2f} "
            f"{row['p99']:9.2f} {row['p99_9']:9.2f} {row['max']:9.2f} "
            f"{row['jitter_p99_minus_p50']:9.2f}"
        )
        # The stage that owns the variance is where the tail jumps, not where
        # the absolute age is largest.
        if prev and prev["n"]:
            d50 = row["p50"] - prev["p50"]
            dj = row["jitter_p99_minus_p50"] - prev["jitter_p99_minus_p50"]
            print(
                f"{'  ^ this stage added':38s} {'':6s} {d50:9.2f} "
                f"{'':9s} {'':9s} {'':9s} {dj:9.2f}"
            )
        prev = row

    for t in args.topics:
        if unstamped[t]:
            print(f"WARNING {t}: {unstamped[t]} message(s) had no usable stamp")
        if negative[t]:
            print(
                f"DEFECT  {t}: {negative[t]} message(s) had a NEGATIVE age. "
                "The publisher's clock is ahead of this host's -- cross-host "
                "ages here are not trustworthy until that is fixed."
            )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "topics": result["topics"],
                    "unstamped": unstamped,
                    "negative_age": negative,
                },
                fh,
                indent=2,
            )

    if silent:
        print(f"\nFAIL: {len(silent)} topic(s) produced no stamped messages.")
        _exit(1)
    _exit(0)


if __name__ == "__main__":
    main()
