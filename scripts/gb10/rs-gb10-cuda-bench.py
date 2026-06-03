#!/usr/bin/env python3
"""Benchmark librealsense's CUDA processing blocks on-device: time rs.colorizer and
rs.pointcloud over real depth frames. Run this TWICE with the same scene — once against the
CUDA build (build-gb10-full) and once against the CUDA-off build (build-gb10-nocuda), selected
via PYTHONPATH — and compare: speedup = nocuda_ms / cuda_ms. This answers the load-bearing
question 'are librealsense's CUDA ops actually accelerated on GB10?' (the cv2.cuda check only
covered OpenCV). Single depth stream → no controller risk. Prints JSON tagged with the build."""
import json
import os
import sys
import time

import numpy as np

W = int(os.environ.get("LRS_BENCH_W", "848"))
H = int(os.environ.get("LRS_BENCH_H", "480"))
FPS = int(os.environ.get("LRS_BENCH_FPS", "30"))
FRAMES, WARMUP = 200, 30


def pct(xs, p):
    return round(float(np.percentile(np.array(xs), p)), 3) if xs else None


def main():
    import pyrealsense2 as rs
    # detect whether this binding is the CUDA build (best-effort via debug info)
    build = os.environ.get("LRS_BUILD_TAG", "unknown")
    rep = {"build_tag": build, "pyrealsense2": rs.__version__, "ops": {}}

    ctx = rs.context()
    if len(ctx.query_devices()) == 0:
        print("NO_DEVICE", file=sys.stderr); return 2
    pipe = rs.pipeline(ctx); cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, FPS)
    colorizer = rs.colorizer(); pc = rs.pointcloud()
    col_ms, pc_ms = [], []
    try:
        pipe.start(cfg)
        for _ in range(WARMUP):
            pipe.wait_for_frames(3000)
        # pre-warm both ops (exclude first-call CUDA init stall from the steady-state numbers)
        f0 = pipe.wait_for_frames(3000).get_depth_frame()
        _ = colorizer.colorize(f0); _ = pc.calculate(f0)
        for _ in range(FRAMES):
            d = pipe.wait_for_frames(3000).get_depth_frame()
            if not d:
                continue
            t = time.time(); c = colorizer.colorize(d); _ = np.asanyarray(c.get_data())
            col_ms.append((time.time() - t) * 1000)
            t = time.time(); v = pc.calculate(d); _ = np.asanyarray(v.get_vertices())
            pc_ms.append((time.time() - t) * 1000)
    finally:
        try:
            pipe.stop()
        except Exception:
            pass

    for name, xs in (("colorize", col_ms), ("pointcloud", pc_ms)):
        if xs:
            rep["ops"][name] = {"n": len(xs), "mean_ms": round(float(np.mean(xs)), 3),
                                "p50_ms": pct(xs, 50), "p95_ms": pct(xs, 95), "min_ms": round(float(np.min(xs)), 3)}
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
