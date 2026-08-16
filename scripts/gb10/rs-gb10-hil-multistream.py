#!/usr/bin/env python3
"""GB10 CONCURRENT MULTI-STREAM stress (DANGEROUS — can kill the USB controller).

Brings up dual depth+color 848x480@60 in one context and runs rs.align(depth->color)
per frame (an advanced multi-stream CUDA feature). This is the configuration class that
killed the controller in incident #3. Run only with eyes-open acceptance; a wrapper arms
the journal tripwire and captures forensics. Aborts immediately on the first SDK error
(the controller-death signature) to minimize further Stop-Endpoint traffic.
"""

import argparse
import json
import sys
import time

import numpy as np

W, H, FPS = 848, 480, 60
WARMUP = 15
FRAMES = 300


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run the dangerous GB10 dual-stream stress against one explicitly "
            "selected USB 3 RealSense device."
        )
    )
    parser.add_argument(
        "--serial",
        required=True,
        help="exact RealSense serial to open (USB 3 link required)",
    )
    return parser.parse_args(argv)


def _error(exc):
    return {"type": type(exc).__name__, "message": str(exc)}


def main(argv=None):
    args = parse_args(argv)

    # Argument validation must finish before loading the hardware-facing SDK.
    import pyrealsense2 as rs

    report = {
        "config": "dual depth+color 848x480@60 + align(depth->color)",
        "device": {"requested_serial": args.serial},
        "expected_frames": FRAMES,
        "ops": {},
        "frames": 0,
        "stream_gaps": 0,
    }
    align_lat = []
    gaps = 0
    last = None
    pipe = None
    started = False
    t0 = None
    try:
        ctx = rs.context()
        pipe = rs.pipeline(ctx)
        cfg = rs.config()
        cfg.enable_device(args.serial)
        cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, FPS)
        cfg.enable_stream(
            rs.stream.color, W, H, rs.format.bgr8, FPS
        )  # SECOND stream -> concurrent
        align = rs.align(rs.stream.color)

        print(
            f"[{time.strftime('%H:%M:%S')}] starting dual stream...",
            file=sys.stderr,
            flush=True,
        )
        profile = pipe.start(cfg)
        started = True
        device = profile.get_device()
        actual_serial = device.get_info(rs.camera_info.serial_number)
        usb = device.get_info(rs.camera_info.usb_type_descriptor)
        report["device"].update({"serial": actual_serial, "usb": usb})
        if actual_serial != args.serial:
            raise RuntimeError(
                f"serial mismatch: requested {args.serial}, opened {actual_serial}"
            )
        if not str(usb).strip().startswith("3"):
            raise RuntimeError(
                f"USB 3 link required for 848x480@60 dual-stream stress; got {usb!r}"
            )

        for _ in range(WARMUP):
            pipe.wait_for_frames(3000)
        print(
            f"[{time.strftime('%H:%M:%S')}] warmup done, streaming "
            f"{FRAMES} aligned frames",
            file=sys.stderr,
            flush=True,
        )
        t0 = time.time()
        for i in range(FRAMES):
            fs = pipe.wait_for_frames(3000)
            t = time.time()
            aligned = align.process(fs)  # multi-stream align (depth->color)
            d = aligned.get_depth_frame()
            c = aligned.get_color_frame()
            if not d or not c:
                report.setdefault("findings", []).append(
                    {"frame": i, "issue": "missing aligned frame"}
                )
                continue
            _ = np.asanyarray(d.get_data())
            _ = np.asanyarray(c.get_data())
            align_lat.append((time.time() - t) * 1000)
            fn = d.get_frame_number()
            if last is not None and fn != last + 1:
                gaps += 1
            last = fn
            report["frames"] += 1
        elapsed = time.time() - t0
        report["effective_fps"] = (
            round(report["frames"] / elapsed, 2) if elapsed > 0 else 0.0
        )
        report["stream_gaps"] = gaps
    except Exception as e:
        report["fatal"] = _error(e)
        print(
            f"[{time.strftime('%H:%M:%S')}] EXCEPTION (likely controller death): {e}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        if started:
            try:
                for _ in range(15):
                    try:
                        pipe.poll_for_frames()
                    except Exception:
                        break
                pipe.stop()
            except Exception as e:
                report["teardown_error"] = _error(e)
                print(f"stop error: {e}", file=sys.stderr)
    if align_lat:
        report["ops"]["align"] = {
            "n": len(align_lat),
            "mean_ms": round(sum(align_lat) / len(align_lat), 2),
            "max_ms": round(max(align_lat), 2),
        }
    report["complete"] = bool(
        report["frames"] == FRAMES
        and report["stream_gaps"] == 0
        and not report.get("findings")
        and "fatal" not in report
        and "teardown_error" not in report
    )
    print(json.dumps(report, indent=2))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
