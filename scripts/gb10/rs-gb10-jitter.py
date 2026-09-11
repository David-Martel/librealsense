#!/usr/bin/env python3
"""Measure frame-delivery JITTER, not throughput.

The operator-facing requirement is determinism: humans notice a frame that
arrives late far more than they notice a slightly lower mean rate. Every other
harness in this directory reports frame COUNTS and gap detection, which cannot
distinguish a stream delivering a clean 33.3 ms cadence from one alternating
20 ms / 47 ms at the same average rate. Both pass a count-based soak
identically; only one of them is watchable.

So this reports the tail, and deliberately never reports a best-of-N:

    inter-arrival  host receipt[n] - host receipt[n-1]  ideal 1000/fps, flat
    frame age      host receipt    - sensor timestamp   camera-to-host latency

for each, p50 / p90 / p99 / p99.9 / max / stddev. The jitter figure quoted in
any conclusion drawn from this data is p99 - p50: how much worse a bad frame is
than a typical one. p50 alone is a throughput statistic and answers a question
nobody is asking here.

Host receipt is time.perf_counter() at the instant wait_for_frames() returns,
i.e. the moment a downstream consumer could first touch the data. That is the
number a ROS node's callback actually experiences, and it includes SDK-internal
queueing that a camera-side timestamp does not show.

Frame age needs both clocks on the same base. RS2_OPTION_GLOBAL_TIME_ENABLED
(default on) makes the SDK report frame timestamps in host wall-clock ms via
time_service::get_time(), which is std::chrono::system_clock -- so the
comparison base is CLOCK_REALTIME, and the script pairs each perf_counter
reading with a CLOCK_REALTIME reading rather than assuming the two agree.

--queue-size and --no-global-time exist to A/B the two levers that sound like
jitter wins. Both were measured on this fleet and both were rejected; read
docs/gb10/benchmarks.md 18.2 before re-running either.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time

try:
    import pyrealsense2 as rs
except ImportError as exc:  # pragma: no cover - environment, not logic
    sys.exit(f"pyrealsense2 unavailable: {exc}")


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile.

    Deliberately not interpolated: at p99.9 over a few thousand samples an
    interpolated value blends two real observations and can report a latency
    that no frame ever had. Nearest-rank always returns a number the hardware
    actually produced.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(values: list[float], *, label: str, ideal: float | None = None) -> dict:
    """Tail-first summary. Mean is kept only because its gap from p50 flags skew."""
    if not values:
        return {"label": label, "n": 0}
    p50 = percentile(values, 50)
    p99 = percentile(values, 99)
    out = {
        "label": label,
        "n": len(values),
        "mean": statistics.fmean(values),
        "stddev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "p50": p50,
        "p90": percentile(values, 90),
        "p99": p99,
        "p99_9": percentile(values, 99.9),
        "max": max(values),
        # The headline number, and not derivable from a mean -- which is
        # precisely why a mean is not enough for this question.
        "jitter_p99_minus_p50": p99 - p50,
    }
    if ideal is not None:
        out["ideal"] = ideal
        out["worst_deviation_from_ideal"] = max(abs(v - ideal) for v in values)
    return out


def _apply_options(profile, args: argparse.Namespace) -> dict | None:
    """Apply the A/B options to every sensor the profile opened.

    Per-sensor, not per-stream: setting queue size on depth alone would leave
    colour buffering 16 deep, and the measurement would then describe a
    configuration that does not exist.
    """
    device = profile.get_device()

    if args.no_global_time:
        # The SDK maintains a running linear fit between the device and host
        # clocks and refreshes it with a hardware-monitor command. On D400 over
        # the RSUSB backend that command shares the USB control path with
        # streaming setup, which made it a candidate cause of the periodic
        # stall. Turning it off costs comparable cross-host stamps, and frame
        # age becomes device-domain and stops being reportable -- inter-arrival
        # stays perf_counter-based and stays valid.
        for sensor in device.query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 0.0)

    if args.queue_size is None:
        return None

    applied = {}
    for sensor in device.query_sensors():
        if sensor.supports(rs.option.frames_queue_size):
            sensor.set_option(rs.option.frames_queue_size, float(args.queue_size))
            name = sensor.get_info(rs.camera_info.name)
            applied[name] = sensor.get_option(rs.option.frames_queue_size)
    return applied


def _domain(frame) -> str:
    """Short timestamp-domain name, or the reason it could not be read."""
    try:
        return str(frame.get_frame_timestamp_domain()).rsplit(".", 1)[-1].lower()
    except Exception as exc:  # pragma: no cover - SDK surface, not logic
        return f"unreadable:{type(exc).__name__}"


def describe_negotiated(profile) -> dict:
    """Report what the device ACTUALLY opened, not what was requested.

    Requested and negotiated are not the same thing, and on this fleet the gap
    is load-bearing: 18.4 records depth modes the SDK advertises but cannot
    open. A harness that prints only its arguments cannot tell a substitution
    from a success.
    """
    out = {}
    for stream in profile.get_streams():
        try:
            video = stream.as_video_stream_profile()
            out[str(stream.stream_type()).rsplit(".", 1)[-1]] = (
                f"{video.width()}x{video.height()}@{stream.fps()}"
                f" {str(stream.format()).rsplit('.', 1)[-1]}"
            )
        except Exception:  # pragma: no cover - non-video streams
            continue
    return out


def assert_device_present(serial: str | None) -> None:
    """Fail fast and loudly when no camera is attached.

    `pipeline.start()` BLOCKS INDEFINITELY when the device is absent rather than
    raising, so a harness that goes straight to start() presents a hardware
    failure as a benchmark that never finishes. That is exactly how the
    spark-0060 xHCI controller death was first seen: a 120 s run still going
    after seven minutes, with an empty output file and nothing to indicate the
    camera had vanished. Checking the context first turns an indefinite hang
    into a one-line diagnosis.
    """
    devices = rs.context().devices
    if len(devices) == 0:
        raise SystemExit(
            "no RealSense device present. pipeline.start() would block forever "
            "rather than raise, so this harness refuses to start.\n"
            "If a capture recently died here, check `dmesg` for "
            "'xHCI host controller not responding' - a dead host controller "
            "needs a power cycle and cannot be rebound."
        )
    if serial and not any(
        d.get_info(rs.camera_info.serial_number) == serial for d in devices
    ):
        found = ", ".join(d.get_info(rs.camera_info.serial_number) for d in devices)
        raise SystemExit(f"serial {serial} not attached; present: {found}")


def run(args: argparse.Namespace) -> dict:
    assert_device_present(args.serial)
    pipeline = rs.pipeline()
    config = rs.config()
    if args.serial:
        config.enable_device(args.serial)
    config.enable_stream(
        rs.stream.depth, args.width, args.height, rs.format.z16, args.fps
    )
    if not args.depth_only:
        config.enable_stream(
            rs.stream.color, args.width, args.height, rs.format.bgr8, args.fps
        )

    profile = pipeline.start(config)
    try:
        negotiated = describe_negotiated(profile)
        applied_queue_size = _apply_options(profile, args)

        # Discard the first frames: pipeline start includes sensor power-up,
        # auto-exposure convergence and first-allocation costs that are real but
        # happen once. Letting them into the sample makes every max and p99.9 a
        # measurement of startup rather than of streaming.
        deadline = time.perf_counter() + args.warmup_s
        while time.perf_counter() < deadline:
            pipeline.wait_for_frames(int(args.timeout_ms))

        domains: dict[str, set[str]] = {"depth": set(), "color": set()}
        inter_arrival: list[float] = []
        frame_age: list[float] = []
        skew: list[float] = []
        stalls: list[dict] = []
        previous_receipt: float | None = None
        frames = 0
        missing_metadata = 0

        end = time.perf_counter() + args.duration_s
        while time.perf_counter() < end:
            frameset = pipeline.wait_for_frames(int(args.timeout_ms))
            receipt = time.perf_counter()
            # Paired reading: perf_counter is monotonic and correct for
            # intervals, while global-time frame stamps sit on the realtime
            # clock. Sampling both at the same instant lets frame age cross that
            # boundary without assuming the two clocks agree.
            receipt_realtime_ms = time.clock_gettime(time.CLOCK_REALTIME) * 1000.0
            frames += 1

            if previous_receipt is not None:
                gap = (receipt - previous_receipt) * 1000.0
                inter_arrival.append(gap)
                # Record WHEN a stall happened, not merely that one did. A tail
                # number alone cannot be correlated with anything; a wall-clock
                # stamp lines up against dmesg, so a cause becomes attributable
                # rather than speculative. The frame index distinguishes a
                # periodic event from a once-per-start one.
                if gap > args.stall_threshold_ms:
                    stalls.append(
                        {
                            "wall_clock": time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(receipt_realtime_ms / 1000.0),
                            ),
                            "gap_ms": gap,
                            "frame_index": frames,
                        }
                    )
            previous_receipt = receipt

            depth = frameset.get_depth_frame()
            if not depth:
                continue
            stamp = depth.get_timestamp()  # ms; wall clock under global time
            # Record the DOMAIN, not just the value. A skew computed from two
            # `system_time` stamps is the difference between two host dequeue
            # instants and says nothing about the imagers; only `global_time`
            # (or `hardware_clock`) stamps make a sub-millisecond skew a claim
            # about the sensors. Reporting one without the other is how a 24 us
            # number gets quoted as a hardware-sync property it may not be.
            domains["depth"].add(_domain(depth))
            if stamp > 0:
                frame_age.append(receipt_realtime_ms - stamp)
            else:
                missing_metadata += 1
            if not args.depth_only:
                color = frameset.get_color_frame()
                if color:
                    domains["color"].add(_domain(color))
                if color and color.get_timestamp() > 0:
                    # Depth and colour are separate imagers. If this skew is
                    # large or unstable, anything fusing them (align, coloured
                    # pointcloud) is combining different moments.
                    skew.append(abs(color.get_timestamp() - stamp))

        return {
            "frames": frames,
            "timestamp_domains": {k: sorted(v) for k, v in domains.items()},
            "negotiated": negotiated,
            "missing_timestamp_frames": missing_metadata,
            "stalls": stalls,
            "applied_queue_size": applied_queue_size,
            "inter_arrival_ms": summarize(
                inter_arrival, label="inter-arrival", ideal=1000.0 / args.fps
            ),
            "frame_age_ms": summarize(frame_age, label="frame age (receipt - sensor)"),
            "depth_color_skew_ms": summarize(skew, label="depth/colour skew"),
        }
    finally:
        pipeline.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument("--warmup-s", type=float, default=3.0)
    parser.add_argument("--timeout-ms", type=float, default=5000.0)
    parser.add_argument("--depth-only", action="store_true")
    parser.add_argument(
        "--queue-size",
        type=int,
        default=None,
        help="RS2_OPTION_FRAMES_QUEUE_SIZE (SDK default 16); 1 drops, not buffers",
    )
    parser.add_argument(
        "--no-global-time",
        action="store_true",
        help="disable RS2_OPTION_GLOBAL_TIME_ENABLED; frame age becomes "
        "device-domain, so only inter-arrival is interpretable on that leg",
    )
    parser.add_argument("--stall-threshold-ms", type=float, default=100.0)
    parser.add_argument("--label", default="")
    parser.add_argument("--json", default=None, help="write the full result here")
    args = parser.parse_args()

    result = {
        "label": args.label,
        "config": {
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "depth_only": args.depth_only,
            "queue_size_requested": args.queue_size,
            "global_time_disabled": args.no_global_time,
            "duration_s": args.duration_s,
        },
        **run(args),
    }

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)

    for key in ("inter_arrival_ms", "frame_age_ms", "depth_color_skew_ms"):
        block = result[key]
        if not block.get("n"):
            continue
        print(
            f"{block['label']:34s} n={block['n']:6d} "
            f"p50={block['p50']:8.3f} p99={block['p99']:8.3f} "
            f"p99.9={block['p99_9']:8.3f} max={block['max']:9.3f} "
            f"sd={block['stddev']:7.3f} "
            f"jitter={block['jitter_p99_minus_p50']:8.3f}"
        )
    for stall in result["stalls"]:
        print(
            f"STALL {stall['wall_clock']} gap={stall['gap_ms']:.1f}ms "
            f"frame={stall['frame_index']}"
        )
    print(f"frames={result['frames']} queue_size={result['applied_queue_size']}")
    print(f"negotiated={result['negotiated']}")
    print(f"timestamp_domains={result['timestamp_domains']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
