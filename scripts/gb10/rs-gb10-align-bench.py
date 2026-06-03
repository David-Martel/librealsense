#!/usr/bin/env python3
"""Benchmark librealsense's rs.align CUDA path on-device — the LOAD-BEARING op: vigil-spark's
realsensenode aligns depth->color every frame, and align is the one real-CUDA processing block
that was never measured (colorize has NO CUDA path; pointcloud measured 0.57x = CUDA SLOWER).

WHAT IT MEASURES: time of align.process(frameset) + forced materialization of the aligned depth
plane (np.asanyarray(get_data()) forces the GPU->host copy, so we time the whole H2D->kernel->D2H
round trip, not just an async launch). align(rs.stream.color) dispatches kernel_depth_to_other in
cuda-align.cu under the CUDA build; the CPU path under build-gb10-nocuda. Run TWICE with the same
static scene, selecting the build via PYTHONPATH/LD_LIBRARY_PATH, and compare:
    speedup = nocuda_p50_ms / cuda_p50_ms     (>1 = CUDA faster, <1 = CUDA SLOWER)

VALIDITY (verified before writing, see docs/gb10):
  * align HAS real CUDA kernels (src/proc/cuda/cuda-align.cu: kernel_map_depth_to_other,
    kernel_depth_to_other, <<<>>> launches) compiled into build-gb10-full -> the ratio is real.
  * build-gb10-full vs build-gb10-nocuda differ ONLY in BUILD_WITH_CUDA (NEON/CPU_EXTENSIONS/
    OPENMP/-mcpu identical) -> the CPU baseline is NOT crippled, the ratio isolates CUDA.
  * GB10 is unified-memory but this CUDA path uses explicit cudaMalloc/cudaMemcpy, so it pays
    H2D+D2H per frame over what is physically the same RAM. A <1 result is a REAL, decision-
    relevant finding for vigil ("do not enable CUDA align"), not a benchmark failure.

SAFETY: align needs depth AND color -> this is a 2-stream (eyes-open) test. It streams a FIXED
config with NO control-feature changes (emitter/preset/auto-exposure toggles are the -110 trigger
class and are deliberately absent), and arms the hil_common tripwire (aborts + dumps forensics on
the first HC-died). Point at a STATIC rigid scene and run both builds back-to-back so scene jitter
does not masquerade as a CUDA effect.

Usage (prefer the justfile):
  just hil-align-bench                      # runs both builds, prints speedup
  LRS_BUILD_TAG=CUDA   <env> rs-gb10-align-bench.py            # one build
  LRS_ALIGN_TO=depth   ... rs-gb10-align-bench.py             # reverse direction (color->depth)
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
ALIGN_TO = os.environ.get("LRS_ALIGN_TO", "color").lower()   # vigil aligns depth->color
BUILD_TAG = os.environ.get("LRS_BUILD_TAG", "unknown")


def main():
    import pyrealsense2 as rs

    display = "--display" in sys.argv
    hil = H.HIL("align-bench", display=display)
    hil.report.update({"build_tag": BUILD_TAG, "pyrealsense2": rs.__version__,
                       "align_to": ALIGN_TO, "width": W, "height": H_, "fps": FPS})
    hil.preflight()  # raises on dead controller / no camera; records usb link

    align_stream = rs.stream.color if ALIGN_TO == "color" else rs.stream.depth
    align = rs.align(align_stream)

    ctx = rs.context()
    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H_, rs.format.z16, FPS)
    cfg.enable_stream(rs.stream.color, W, H_, rs.format.bgr8, FPS)

    align_ms = []
    ok = True
    try:
        pipe.start(cfg)
        for _ in range(WARMUP):
            pipe.wait_for_frames(5000)
        # pre-warm: first align pays CUDA context/alloc init — exclude it from steady state
        fs0 = pipe.wait_for_frames(5000)
        a0 = align.process(fs0)
        _ = np.asanyarray((a0.get_depth_frame() if ALIGN_TO == "color"
                           else a0.get_color_frame()).get_data())

        for i in range(FRAMES):
            fs = pipe.wait_for_frames(5000)
            t = time.time()
            aligned = align.process(fs)
            # the aligned plane is the depth re-projected onto color (depth->color), or the
            # color re-projected onto depth (color->depth). Materialize it to force the D2H copy.
            out = aligned.get_depth_frame() if ALIGN_TO == "color" else aligned.get_color_frame()
            if not out:
                continue
            buf = np.asanyarray(out.get_data())
            align_ms.append((time.time() - t) * 1000.0)
            if display and i % 3 == 0:
                # colorize the aligned depth (or show aligned color) just for the on-screen proof
                if ALIGN_TO == "color":
                    import cv2
                    vis = cv2.applyColorMap(cv2.convertScaleAbs(buf, alpha=0.03), cv2.COLORMAP_JET)
                else:
                    vis = buf
                hil.show(vis, f"{BUILD_TAG} align->{ALIGN_TO} {align_ms[-1]:.2f}ms")
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
        if display:
            hil.grab_proof(f"align-{BUILD_TAG}")
            hil.close_display()

    hil.report["align_process_ms"] = H.stats(align_ms)
    rc = hil.finish(ok=ok and len(align_ms) > 0)
    # optional flat result copy for the just recipe's speedup comparison (avoids stdout parsing)
    out_json = os.environ.get("LRS_RESULT_JSON")
    if out_json:
        import json
        with open(out_json, "w") as f:
            json.dump(hil.report, f)
    return rc


if __name__ == "__main__":
    sys.exit(main())
