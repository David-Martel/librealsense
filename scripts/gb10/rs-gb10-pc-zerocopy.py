#!/usr/bin/env python3
"""Pointcloud zero-copy ATTRIBUTION LADDER for GB10 shared memory. The CUDA pointcloud path
(cuda-pointcloud.cu) does per-frame cudaMalloc x3 + cudaMemcpy H2D/D2H + cudaFree x3 — and measured
0.57x SLOWER than NEON. This decomposes WHY, each rung vs the NEON baseline:
  RS2_PC_MODE=0  baseline      : per-frame malloc + memcpy + kernel + free (the shipped path)
  RS2_PC_MODE=1  cached-device : alloc ONCE, reuse; still cudaMemcpy   -> isolates ALLOCATION churn
  RS2_PC_MODE=2  cached-managed: cudaMallocManaged once; plain memcpy on coherent mem, explicit sync
                                 -> isolates the COPY on GB10 unified memory
Requires a build with -DRS2_GB10_PC_ZEROCOPY=1 (the ladder is compiled in; default mode 0 = baseline).
Run the NEON baseline against build-gb10-nocuda (mode arg ignored there).

CORRECTNESS FIRST (advisor mandate): dropping the D2H cudaMemcpy also drops its implicit sync, so a
missing cudaDeviceSynchronize yields FAST GARBAGE. Before any timing we compare the rs.pointcloud
vertices against a pure-numpy CPU deprojection of the SAME depth frame; max-abs-diff must be ~0.
Single depth stream = the conservative-SAFE envelope.

Env: RS2_PC_MODE (0/1/2), LRS_BENCH_W/H/FPS (default 848x480x30), LRS_BUILD_TAG, LRS_RESULT_JSON.
"""
import os
import sys
import time

import numpy as np

import hil_common as H

W = int(os.environ.get("LRS_BENCH_W", "848"))
H_ = int(os.environ.get("LRS_BENCH_H", "480"))
FPS = int(os.environ.get("LRS_BENCH_FPS", "30"))
FRAMES = int(os.environ.get("LRS_BENCH_FRAMES", "200"))
WARMUP = int(os.environ.get("LRS_BENCH_WARMUP", "30"))
MODE = os.environ.get("RS2_PC_MODE", "0")
BUILD_TAG = os.environ.get("LRS_BUILD_TAG", "unknown")


def numpy_deproject(depth_u16, intrin, scale):
    """Pinhole CPU reference (no distortion — D435 depth intrinsics are rectified). Returns Nx3 m."""
    h, w = depth_u16.shape
    ys, xs = np.mgrid[0:h, 0:w]
    z = depth_u16.astype(np.float32) * scale
    x = (xs - intrin.ppx) / intrin.fx * z
    y = (ys - intrin.ppy) / intrin.fy * z
    return np.stack([x, y, z], axis=-1).reshape(-1, 3)


def main():
    import pyrealsense2 as rs
    hil = H.HIL("pc-zerocopy", display=False)
    hil.report.update({"build_tag": BUILD_TAG, "pyrealsense2": rs.__version__,
                       "pc_mode": MODE, "width": W, "height": H_, "fps": FPS})
    hil.preflight()

    pc = rs.pointcloud()
    pipe = rs.pipeline(rs.context())
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H_, rs.format.z16, FPS)

    pc_ms = []
    ok = True
    try:
        prof = pipe.start(cfg)
        depth_prof = prof.get_stream(rs.stream.depth).as_video_stream_profile()
        intrin = depth_prof.get_intrinsics()
        scale = prof.get_device().first_depth_sensor().get_depth_scale()
        hil.report["distortion_model"] = str(intrin.model)
        for _ in range(WARMUP):
            pipe.wait_for_frames(5000)

        # ---- correctness: rs.pointcloud (CUDA mode under test) vs numpy CPU deproject, same frame ----
        df = pipe.wait_for_frames(5000).get_depth_frame()
        depth_np = np.asanyarray(df.get_data())
        pts = pc.calculate(df)
        v = np.asanyarray(pts.get_vertices()).view(np.float32).reshape(-1, 3)
        ref = numpy_deproject(depth_np, intrin, scale)
        # compare only valid (z>0) pixels; vertices order matches row-major pixel order
        zmask = ref[:, 2] > 0
        if zmask.any():
            maxdiff = float(np.max(np.abs(v[zmask] - ref[zmask])))
        else:
            maxdiff = float("nan")
        hil.report["correctness_max_abs_diff_m"] = round(maxdiff, 6)
        hil.report["correct"] = bool(maxdiff < 1e-3)  # < 1 mm => kernel + sync are correct

        # ---- timing ----
        _ = pc.calculate(df)  # pre-warm
        for i in range(FRAMES):
            d = pipe.wait_for_frames(5000).get_depth_frame()
            if not d:
                continue
            t = time.time()
            verts = pc.calculate(d)
            _ = np.asanyarray(verts.get_vertices())   # force D2H / coherent read
            pc_ms.append((time.time() - t) * 1000.0)
            if i % 50 == 49:
                hil.check_tripwire()
    except H.Tripwire:
        ok = False
        raise
    finally:
        try:
            pipe.stop()
        except Exception:
            pass

    hil.report["pointcloud_ms"] = H.stats(pc_ms)
    rc = hil.finish(ok=ok and len(pc_ms) > 0)
    out_json = os.environ.get("LRS_RESULT_JSON")
    if out_json:
        import json
        with open(out_json, "w") as f:
            json.dump(hil.report, f)
    return rc


if __name__ == "__main__":
    sys.exit(main())
