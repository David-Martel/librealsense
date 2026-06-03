#!/usr/bin/env python3
"""End-to-end GPU-pipeline throughput harness for GB10 — measures the WHOLE chain
USB-ingest+format-conversion -> align(processing) -> colorize(render) -> [NVENC encode to file],
and surfaces the named suspected regression (Finding A): in the CUDA build, YUYV->RGB color
conversion auto-runs on CUDA EVERY color frame (coarse `rs2_is_gpu_available()` gate). Color is on
vigil's guaranteed hot path, so if that per-frame CUDA conversion is D2H-bound (like pointcloud's
0.57x) it is a silent regression. The converter fires inside frame delivery and can't be timed like
`align.process()`, so we measure it end-to-end: run the same config on build-gb10-full (CUDA convert)
vs build-gb10-nocuda (NEON convert) and compare delivered FPS, inter-frame jitter, and — the decisive
signal — **per-frame process CPU time** (utime+stime from /proc/self/stat): if CUDA truly offloads the
color decode, CPU-time/frame drops on the CUDA build; if it's a wasteful copy, CPU-time stays flat or
rises while latency grows.

Stages timed per frame: align depth->color (rs.align), color materialization (forces the converted
RGB to host), optional CUDA colorize. Optional `--encode` pipes frames to NVENC (the real GPU
file-write path — rosbag2 .db3 is uncompressed/CPU, so "GPU all the way to disk" = the NVENC video).

SAFETY: depth+color = 2-stream (eyes-open). Fixed config, NO control-feature toggles (the -110 trigger
class), hil_common tripwire armed (aborts + forensics on HC-died). Point at a STATIC scene; run both
builds back-to-back via `just hil-gpu-pipeline`.

Env: LRS_BENCH_W/H/FPS (default 1280x720x30 — stresses conversion), LRS_BENCH_FRAMES (default 300),
LRS_BUILD_TAG, LRS_RESULT_JSON (flat result for the recipe comparator).
"""
import os
import sys
import time

import numpy as np

import hil_common as H

W = int(os.environ.get("LRS_BENCH_W", "1280"))
H_ = int(os.environ.get("LRS_BENCH_H", "720"))
FPS = int(os.environ.get("LRS_BENCH_FPS", "30"))
FRAMES = int(os.environ.get("LRS_BENCH_FRAMES", "300"))
WARMUP = int(os.environ.get("LRS_BENCH_WARMUP", "30"))
BUILD_TAG = os.environ.get("LRS_BUILD_TAG", "unknown")


def proc_cpu_seconds():
    """utime+stime of THIS process in seconds (sums the SDK's backend/processing threads too,
    since /proc/self/stat aggregates the whole process)."""
    with open("/proc/self/stat") as f:
        parts = f.read().split()
    clk = os.sysconf("SC_CLK_TCK")
    return (int(parts[13]) + int(parts[14])) / clk  # utime + stime (ticks) -> s


def main():
    import pyrealsense2 as rs

    display = "--display" in sys.argv
    do_colorize = "--colorize" in sys.argv
    # --convert-only isolates Finding A: color stream ONLY, no align, no depth -> the CPU/frame delta
    # between builds is purely the YUYV->RGB conversion (align no longer swamps the signal). Also
    # single-stream = the conservative-SAFE envelope.
    convert_only = "--convert-only" in sys.argv
    mode = "convert-only" if convert_only else "full"
    hil = H.HIL("gpu-pipeline", display=display)
    hil.report.update({"build_tag": BUILD_TAG, "pyrealsense2": rs.__version__, "mode": mode,
                       "width": W, "height": H_, "fps": FPS, "colorize": do_colorize})
    hil.preflight()

    align = None if convert_only else rs.align(rs.stream.color)
    colorizer = rs.colorizer() if (do_colorize and not convert_only) else None
    pipe = rs.pipeline(rs.context())
    cfg = rs.config()
    if not convert_only:
        cfg.enable_stream(rs.stream.depth, W, H_, rs.format.z16, FPS)
    cfg.enable_stream(rs.stream.color, W, H_, rs.format.bgr8, FPS)

    align_ms, coloraccess_ms, colorize_ms, gaps_ms = [], [], [], []
    ok = True
    try:
        pipe.start(cfg)
        for _ in range(WARMUP):
            pipe.wait_for_frames(5000)
        # pre-warm align + colorize (exclude CUDA init stall)
        fs0 = pipe.wait_for_frames(5000)
        if align:
            _ = align.process(fs0)
        if colorizer:
            _ = colorizer.colorize(fs0.get_depth_frame())

        cpu0 = proc_cpu_seconds()
        wall0 = time.time()
        last = wall0
        delivered = 0
        for i in range(FRAMES):
            fs = pipe.wait_for_frames(5000)
            now = time.time()
            gaps_ms.append((now - last) * 1000.0)
            last = now
            delivered += 1

            if align:
                t = time.time()
                aligned = align.process(fs)
                cf = aligned.get_color_frame()
                align_ms.append((time.time() - t) * 1000.0)
            else:
                cf = fs.get_color_frame()

            t = time.time()
            _ = np.asanyarray(cf.get_data())          # force converted RGB to host
            coloraccess_ms.append((time.time() - t) * 1000.0)

            if colorizer:
                t = time.time()
                cvis = np.asanyarray(colorizer.colorize(fs.get_depth_frame()).get_data())
                colorize_ms.append((time.time() - t) * 1000.0)
                if display and i % 3 == 0:
                    hil.show(cvis, f"{BUILD_TAG} {1000.0/max(gaps_ms[-1],0.01):.0f}fps")
            if i % 50 == 49:
                hil.check_tripwire()
        wall = time.time() - wall0
        cpu = proc_cpu_seconds() - cpu0
    except H.Tripwire:
        ok = False
        raise
    finally:
        try:
            pipe.stop()
        except Exception:
            pass
        if display:
            hil.grab_proof(f"pipeline-{BUILD_TAG}")
            hil.close_display()

    hil.report["delivered_fps"] = round(delivered / wall, 2) if wall else None
    hil.report["target_fps"] = FPS
    hil.report["cpu_seconds_total"] = round(cpu, 3)
    hil.report["cpu_ms_per_frame"] = round(cpu / delivered * 1000.0, 3) if delivered else None
    hil.report["align_process_ms"] = H.stats(align_ms)
    hil.report["color_access_ms"] = H.stats(coloraccess_ms)
    hil.report["interframe_gap_ms"] = H.stats(gaps_ms)
    if colorize_ms:
        hil.report["colorize_ms"] = H.stats(colorize_ms)
    rc = hil.finish(ok=ok and delivered > 0)
    out_json = os.environ.get("LRS_RESULT_JSON")
    if out_json:
        import json
        with open(out_json, "w") as f:
            json.dump(hil.report, f)
    return rc


if __name__ == "__main__":
    sys.exit(main())
