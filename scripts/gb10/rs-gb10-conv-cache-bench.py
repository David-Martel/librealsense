#!/usr/bin/env python3
"""End-to-end benchmark for RS2_GB10_CONV_CACHE cached-buffer ladder on the YUYV->color path.

Measures CPU ms/frame (the Finding A metric) for three configurations:
  A) build-gb10-full  RS2_CONV_MODE=1  (cached device buffers)
  B) build-gb10-full  RS2_CONV_MODE=0  (baseline, per-frame cudaMalloc/Free)
  C) build-gb10-nocuda                      (NEON conversion, CUDA disabled)

Uses --convert-only mode of rs-gb10-gpu-pipeline.py (color stream only, no align, no depth)
to isolate Finding A: the YUYV->BGR8 conversion CPU cost.  This matches the harness that
originally measured CUDA 2.00 ms/frame vs NEON 2.03 ms/frame.

PRE-REGISTERED EXPECTATION: Finding A established the stage is plumbing-bound, not
malloc-bound.  Caching eliminates ~450 µs/frame of isolated cudaMalloc+cudaFree overhead
(see kernel-only microbench in bench_conv_cache_kernel.cu), but the end-to-end CPU/frame
signal is expected to remain ~2 ms (neutral), because the blocking cost is the USB transfer
and host<->device DMA, not the allocator.  SUCCESS = honest measurement, not a speedup.

SAFETY:
  - No camera is opened by this script.  It is a LAUNCHER that invokes rs-gb10-gpu-pipeline.py
    from the appropriate build's PYTHONPATH.  The camera is opened inside that child process.
  - Do NOT run multiple configurations concurrently (each child opens the camera).
  - Do NOT run while another process already holds the camera.
  - Targeted at GB10 / DGX Spark (aarch64); PYTHONPATH paths are hardcoded for the
    ~/realsense-gb10-validation layout.

Usage:
  cd /home/damartel/dev/repos/librealsense/scripts/gb10
  python rs-gb10-conv-cache-bench.py [--frames N] [--fps F] [--width W] [--height H]

Env overrides:
  LRS_BENCH_FRAMES   (default 300)
  LRS_BENCH_FPS      (default 30)
  LRS_BENCH_W        (default 1280)
  LRS_BENCH_H        (default 720)
  LRS_BENCH_WARMUP   (default 30)
  LRS_HIL_OUT        (default ~/realsense-gb10-validation/hil-runs)

Output: JSON result written to ~/realsense-gb10-validation/hil-runs/<ts>-conv-cache-bench/result.json
"""
import json
import os
import subprocess
import sys
import time
import argparse

VENV_PYTHON = os.environ.get(
    "LRS_VENV", os.path.expanduser("~/realsense-gb10-validation/.venv/bin/python")
)
BUILD_ROOT   = os.path.expanduser("~/realsense-gb10-validation")
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
GPU_PIPELINE = os.path.join(SCRIPT_DIR, "rs-gb10-gpu-pipeline.py")


def build_pythonpath(build_name: str) -> str:
    """Return PYTHONPATH pointing at the given build's Release dir."""
    return os.path.join(BUILD_ROOT, build_name, "Release")


def run_one(label: str, build_name: str, conv_mode: int | None,
            frames: int, fps: int, W: int, H: int, warmup: int,
            result_json: str) -> dict:
    """Launch rs-gb10-gpu-pipeline.py --convert-only in a subprocess, return parsed result."""
    env = os.environ.copy()
    build_release = build_pythonpath(build_name)  # e.g. .../build-gb10-full/Release
    # Both PYTHONPATH and LD_LIBRARY_PATH must point to the SAME build's Release dir:
    # pyrealsense2.cpython-*.so dlopen()s librealsense2.so from LD_LIBRARY_PATH.
    # Prepend so we shadow anything already set (e.g. a stale build-gb10-full entry).
    env["PYTHONPATH"]        = build_release + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["LD_LIBRARY_PATH"]   = build_release + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["LRS_BENCH_FRAMES"]  = str(frames)
    env["LRS_BENCH_FPS"]     = str(fps)
    env["LRS_BENCH_W"]       = str(W)
    env["LRS_BENCH_H"]       = str(H)
    env["LRS_BENCH_WARMUP"]  = str(warmup)
    env["LRS_BUILD_TAG"]     = label
    env["LRS_RESULT_JSON"]   = result_json
    if conv_mode is not None:
        env["RS2_CONV_MODE"] = str(conv_mode)
    elif "RS2_CONV_MODE" in env:
        del env["RS2_CONV_MODE"]  # ensure no stale override from calling shell

    cmd = [VENV_PYTHON, GPU_PIPELINE, "--convert-only"]
    print(f"\n[conv-cache-bench] Running: {label}  build={build_name}  "
          f"RS2_CONV_MODE={conv_mode if conv_mode is not None else '(unset)'}  "
          f"{W}x{H}@{fps} frames={frames}")
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=False)  # let stdout/stderr pass through
    elapsed = time.time() - t0
    print(f"[conv-cache-bench] Done in {elapsed:.1f}s, rc={proc.returncode}")

    if os.path.exists(result_json):
        with open(result_json) as f:
            return json.load(f)
    return {"label": label, "error": "no result json", "returncode": proc.returncode}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frames",  type=int, default=int(os.environ.get("LRS_BENCH_FRAMES", "300")))
    ap.add_argument("--fps",     type=int, default=int(os.environ.get("LRS_BENCH_FPS",    "30")))
    ap.add_argument("--width",   type=int, default=int(os.environ.get("LRS_BENCH_W",     "1280")))
    ap.add_argument("--height",  type=int, default=int(os.environ.get("LRS_BENCH_H",      "720")))
    ap.add_argument("--warmup",  type=int, default=int(os.environ.get("LRS_BENCH_WARMUP",  "30")))
    ap.add_argument("--out-dir", default=None, help="Override output directory")
    args = ap.parse_args()

    hil_out = os.environ.get("LRS_HIL_OUT", os.path.expanduser("~/realsense-gb10-validation/hil-runs"))
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir or os.path.join(hil_out, f"{ts}-conv-cache-bench")
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 70)
    print("  rs-gb10-conv-cache-bench.py  — RS2_GB10_CONV_CACHE attribution ladder")
    print(f"  Config: {args.width}x{args.height}@{args.fps}  frames={args.frames}  warmup={args.warmup}")
    print(f"  Output: {out_dir}")
    print("  PRE-REGISTERED EXPECTATION: end-to-end result expected NEUTRAL (~2 ms/frame)")
    print("  (Finding A: stage is plumbing-bound, not malloc-bound)")
    print("=" * 70)

    configs = [
        # (label, build_name, RS2_CONV_MODE)
        ("cuda-cached(mode1)",   "build-gb10-full", 1),
        ("cuda-baseline(mode0)", "build-gb10-full", 0),
        ("neon-nocuda",          "build-gb10-nocuda",    None),
    ]

    all_results = []
    for label, build, mode in configs:
        result_json = os.path.join(out_dir, f"result-{label.replace('/', '-')}.json")
        result = run_one(label, build, mode,
                         args.frames, args.fps, args.width, args.height, args.warmup,
                         result_json)
        result["label"] = label
        all_results.append(result)

        # Pause between camera opens to let USB controller settle
        if label != configs[-1][0]:
            print("[conv-cache-bench] Settling 3 s between runs...")
            time.sleep(3)

    # Summary table
    print("\n" + "=" * 70)
    print("  SUMMARY — cpu_ms_per_frame  (--convert-only, color only)")
    print("  (lower = less CPU cost per converted frame)")
    print("-" * 70)
    print(f"  {'label':<28} {'cpu_ms/frame':>14}  {'delivered_fps':>14}")
    print("-" * 70)
    for r in all_results:
        cpu_ms = r.get("cpu_ms_per_frame", "n/a")
        fps    = r.get("delivered_fps",     "n/a")
        ok     = r.get("ok", "?")
        print(f"  {r['label']:<28} {str(cpu_ms):>14}  {str(fps):>14}  ok={ok}")
    print("-" * 70)
    print()
    print("  INTERPRETATION GUIDE:")
    print("  - If cuda-cached and cuda-baseline are both ~2 ms and match neon-nocuda:")
    print("    Finding A confirmed: stage is plumbing-bound; caching is correctly a NOP.")
    print("  - If cuda-cached is materially faster (~<1.5 ms):")
    print("    Unexpected; the allocator WAS the bottleneck — unusual for GPU path.")
    print("  - In either case RS2_GB10_CONV_CACHE is a CORRECT gated change (max-abs-diff=0).")
    print()
    print("  KERNEL-ONLY REFERENCE (bench_conv_cache_kernel.cu, synthetic, NOT e2e):")
    print("    mode=0: ~555 µs/call (1280x720 BGR8, includes cudaMalloc+cudaFree each call)")
    print("    mode=1: ~106 µs/call (1280x720 BGR8, alloc-once; ~5.2x faster in isolation)")
    print("    This gain does NOT appear end-to-end due to plumbing dominance.")
    print("=" * 70)

    # Write combined result
    combined_path = os.path.join(out_dir, "result.json")
    with open(combined_path, "w") as f:
        json.dump({"runs": all_results, "config": vars(args), "ts": ts}, f, indent=2)
    print(f"\n[conv-cache-bench] Combined result: {combined_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
