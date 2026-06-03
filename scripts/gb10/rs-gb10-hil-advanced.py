#!/usr/bin/env python3
"""GB10 advanced single-stream HIL: stress the CUDA / OpenCV(cv2.cuda) / Python paths
on ONE depth stream (the proven-safe envelope). Exercises librealsense CUDA colorize +
pointcloud, a cv2.cuda upload/resize/colormap/download pipeline, the post-processing
filter chain, and (best-effort) NVENC encode. Reports per-op latency, stream gaps, and
any exceptions as candidate bugs. Single stream only -> does NOT risk the controller.

Run via `just hil-advanced` (sets LD_LIBRARY_PATH + PYTHONPATH) or with the env from that
recipe. Aborts immediately on a fatal SDK error (the controller-death signature).
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

W, H, FPS = 848, 480, 60
WARMUP = 30
FRAMES = 300            # ~5 s at 60 fps
TIMEOUT_MS = 3000


def jr(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def controller_alive():
    out = jr("journalctl -k --no-pager -n 80 2>/dev/null | grep -iE 'HC died|not responding to stop' | tail -1")
    return out == ""


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(len(s) * p / 100))], 3)


def main():
    import pyrealsense2 as rs
    import cv2

    findings = []
    cuda_dev = cv2.cuda.getCudaEnabledDeviceCount()
    report = {"env": {"pyrealsense2": rs.__version__, "cv2": cv2.__version__,
                      "cv2_cuda_devices": cuda_dev, "numpy": np.__version__},
              "ops": {}, "findings": findings}
    if not controller_alive():
        print("ABORT: controller shows a prior HC-died; reboot required.", file=sys.stderr)
        return 2

    # --- single depth stream, one context (session-stable ownership) ---
    ctx = rs.context()
    if len(ctx.query_devices()) == 0:
        print("ABORT: no device", file=sys.stderr)
        return 2
    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, FPS)

    colorizer = rs.colorizer()
    pc = rs.pointcloud()
    dec, spat, temp, hole = rs.decimation_filter(), rs.spatial_filter(), rs.temporal_filter(), rs.hole_filling_filter()

    lat = {k: [] for k in ("acquire", "rs_colorize", "rs_pointcloud", "cv_cuda_pipeline",
                           "postproc_chain")}
    gaps = 0
    last_fn = None
    nvenc_status = "not-attempted"

    try:
        prof = pipe.start(cfg)
        # warmup
        for _ in range(WARMUP):
            pipe.wait_for_frames(TIMEOUT_MS)

        gpu = cv2.cuda_GpuMat()
        t_run0 = time.time()
        for i in range(FRAMES):
            t0 = time.time()
            fs = pipe.wait_for_frames(TIMEOUT_MS)
            d = fs.get_depth_frame()
            if not d:
                findings.append({"op": "acquire", "issue": "empty depth frame", "frame": i})
                continue
            lat["acquire"].append((time.time() - t0) * 1000)

            fn = d.get_frame_number()
            if last_fn is not None and fn != last_fn + 1:
                gaps += 1
            last_fn = fn

            # 1) librealsense CUDA colorize
            t = time.time()
            cframe = colorizer.colorize(d)
            cimg = np.asanyarray(cframe.get_data())
            lat["rs_colorize"].append((time.time() - t) * 1000)

            # 2) librealsense CUDA pointcloud (depth-only, no texture -> single-stream safe)
            t = time.time()
            pts = pc.calculate(d)
            _ = np.asanyarray(pts.get_vertices())          # force materialization
            lat["rs_pointcloud"].append((time.time() - t) * 1000)

            # 3) cv2.cuda pipeline: upload depth16 -> resize -> 8u normalize -> colormap -> download
            t = time.time()
            depth16 = np.asanyarray(d.get_data())
            gpu.upload(depth16)
            gpu_small = cv2.cuda.resize(gpu, (W // 2, H // 2))
            gpu_8u = gpu_small.convertTo(cv2.CV_8U, alpha=0.03)
            try:
                gpu_color = cv2.cuda.applyColorMap(gpu_8u, cv2.COLORMAP_JET)
                _ = gpu_color.download()
            except Exception:
                # some builds lack cuda.applyColorMap -> fall back to CPU colormap
                _ = cv2.applyColorMap(gpu_8u.download(), cv2.COLORMAP_JET)
                if i == WARMUP:
                    findings.append({"op": "cv_cuda_pipeline", "issue": "cv2.cuda.applyColorMap unavailable; CPU fallback"})
            lat["cv_cuda_pipeline"].append((time.time() - t) * 1000)

            # 4) post-processing filter chain
            t = time.time()
            f = dec.process(d); f = spat.process(f); f = temp.process(f); f = hole.process(f)
            _ = np.asanyarray(f.get_data())
            lat["postproc_chain"].append((time.time() - t) * 1000)

        wall = time.time() - t_run0
        report["effective_fps"] = round(FRAMES / wall, 2)
        report["stream_gaps"] = gaps

        # 5) NVENC availability probe (best-effort, non-fatal)
        try:
            if hasattr(cv2, "cudacodec"):
                vw = cv2.cudacodec.createVideoWriter("/tmp/gb10-nvenc-test.mp4", (W, H), cv2.cudacodec.HEVC, FPS)
                nvenc_status = "cv2.cudacodec available"
                del vw
            else:
                nvenc_status = "cv2.cudacodec NOT in this opencv build"
                findings.append({"op": "nvenc", "issue": "cv2.cudacodec module absent; use ffmpeg NVENC path instead"})
        except Exception as e:
            nvenc_status = f"cv2.cudacodec error: {e}"
            findings.append({"op": "nvenc", "issue": nvenc_status})

    except Exception as e:
        msg = str(e)
        fatal = any(s in msg for s in ("Connection timed out", "failed to set power", "No device connected",
                                       "xioctl", "Stop-Endpoint", "controller"))
        findings.append({"op": "fatal", "issue": msg, "fatal": fatal})
        print(f"EXCEPTION ({'FATAL' if fatal else 'non-fatal'}): {msg}", file=sys.stderr)
    finally:
        try:
            # stop hygiene: drain then stop
            for _ in range(15):
                try:
                    pipe.poll_for_frames()
                except Exception:
                    break
            pipe.stop()
        except Exception:
            pass

    for op, xs in lat.items():
        if xs:
            report["ops"][op] = {"n": len(xs), "mean_ms": round(sum(xs) / len(xs), 3),
                                 "p50_ms": pct(xs, 50), "p95_ms": pct(xs, 95), "max_ms": round(max(xs), 3)}
    report["nvenc"] = nvenc_status
    report["controller_green_after"] = controller_alive()

    print(json.dumps(report, indent=2))
    return 0 if report.get("controller_green_after") else 1


if __name__ == "__main__":
    sys.exit(main())
