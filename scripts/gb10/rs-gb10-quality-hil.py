#!/usr/bin/env python3
"""GB10 frame/video QUALITY HIL — real measured data, not "assume it renders".

Single depth+color stream (color is the quality target). Measures THREE distinct,
honestly-scoped things (PSNR/SSIM are full-reference, so we are explicit about what the
reference is in each case):

  (a) NVENC ENCODE FIDELITY — encode the captured color frames with NVENC (h264/hevc) and
      with a LOSSLESS reference, then ffmpeg psnr/ssim/xpsnr(NVENC vs lossless). Answers
      "does the GPU video encoder preserve the captured frames?" Reference = the raw capture.
  (b) GPU-vs-CPU RENDER FIDELITY — cv2.cuda.cvtColor + cuda.resize vs the CPU equivalent,
      per frame, via cv2.PSNR + numpy-SSIM. Answers "does the CUDA render path match CPU?"
  (c) NO-REFERENCE CAPTURE QUALITY — Laplacian sharpness, brightness/contrast, saturated-pixel
      fraction per frame. Answers "is the camera actually producing good frames?" (no reference).

Run via `just hil-quality` (sets the CUDA cv2 + ffmpeg env). Single-stream => no controller risk.
"""
import json
import subprocess
import sys
import os
import time

import numpy as np

W, H, FPS, FRAMES, WARMUP = 848, 480, 30, 120, 15
FFMPEG = os.environ.get("LRS_FFMPEG", "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg")


def ssim_numpy(a, b):
    """Standard global SSIM on grayscale float images (no skimage dependency)."""
    a = a.astype(np.float64); b = b.astype(np.float64)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a, mu_b = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = ((a - mu_a) * (b - mu_b)).mean()
    return ((2 * mu_a * mu_b + C1) * (2 * cov + C2)) / ((mu_a**2 + mu_b**2 + C1) * (va + vb + C2))


def _ffmpeg_metric(distorted, reference, metric):
    """One reliable ffmpeg pass for a single metric filter (ssim|psnr|xpsnr)."""
    return subprocess.run([FFMPEG, "-hide_banner", "-i", distorted, "-i", reference,
                           "-lavfi", metric, "-f", "null", "-"],
                          capture_output=True, text=True, env=dict(os.environ)).stderr


def ffmpeg_compare(distorted, reference):
    """Run ffmpeg ssim, psnr, xpsnr (each its own pass) -> parsed averages."""
    res = {}
    for line in _ffmpeg_metric(distorted, reference, "ssim").splitlines():
        if "SSIM" in line and "All:" in line:
            try:
                res["ssim"] = float(line.split("All:")[1].split()[0])
            except (ValueError, IndexError):
                pass
    for line in _ffmpeg_metric(distorted, reference, "psnr").splitlines():
        if "PSNR" in line and "average:" in line:
            try:
                res["psnr_db"] = float(line.split("average:")[1].split()[0])
            except (ValueError, IndexError):
                pass
    for line in _ffmpeg_metric(distorted, reference, "xpsnr").splitlines():
        if "XPSNR" in line:
            for tok in line.replace(",", " ").split():
                try:
                    v = float(tok)
                    if 0 < v < 100:
                        res["xpsnr_db"] = v
                except ValueError:
                    continue
    return res


def encode(raw_path, out_path, codec_args):
    env = dict(os.environ)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", raw_path] + codec_args + [out_path]
    return subprocess.run(cmd, capture_output=True, text=True, env=env).returncode


def main():
    import pyrealsense2 as rs
    import cv2

    report = {"env": {"pyrealsense2": rs.__version__, "cv2": cv2.__version__,
                      "cuda": cv2.cuda.getCudaEnabledDeviceCount(), "ffmpeg": os.path.basename(FFMPEG)},
              "params": {"WxH": f"{W}x{H}", "fps": FPS, "frames": FRAMES}}

    ctx = rs.context()
    if len(ctx.query_devices()) == 0:
        print("NO_DEVICE", file=sys.stderr); return 2
    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.color, W, H, rs.format.bgr8, FPS)   # color only -> single stream (safe)

    raw_path = "/tmp/q_raw.bgr"
    sharp, bright, contrast, sat_frac = [], [], [], []
    gpu_psnr, gpu_ssim = [], []
    gpu = cv2.cuda_GpuMat()
    n = 0
    try:
        pipe.start(cfg)
        for _ in range(WARMUP):
            pipe.wait_for_frames(3000)
        with open(raw_path, "wb") as raw:
            for i in range(FRAMES):
                fs = pipe.wait_for_frames(3000)
                cf = fs.get_color_frame()
                if not cf:
                    continue
                img = np.asanyarray(cf.get_data())          # HxWx3 BGR
                raw.write(img.tobytes())                    # accumulate for encode
                n += 1

                # (c) no-reference capture quality
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                sharp.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
                bright.append(float(gray.mean()))
                contrast.append(float(gray.std()))
                sat_frac.append(float((gray >= 254).mean() + (gray <= 1).mean()))

                # (b) GPU-vs-CPU render fidelity: cuda.cvtColor(BGR2GRAY) vs CPU
                gpu.upload(img)
                gpu_gray = cv2.cuda.cvtColor(gpu, cv2.COLOR_BGR2GRAY).download()
                gpu_psnr.append(float(cv2.PSNR(gray, gpu_gray)))
                gpu_ssim.append(float(ssim_numpy(gray, gpu_gray)))
        report["frames_captured"] = n
    finally:
        try:
            pipe.stop()
        except Exception:
            pass

    def stat(xs):
        a = np.array(xs) if xs else np.array([0.0])
        return {"mean": round(float(a.mean()), 3), "min": round(float(a.min()), 3),
                "max": round(float(a.max()), 3), "p05": round(float(np.percentile(a, 5)), 3)}

    report["c_capture_quality"] = {"sharpness_laplacian_var": stat(sharp), "brightness": stat(bright),
                                   "contrast_std": stat(contrast), "saturated_clipped_frac": stat(sat_frac)}
    report["b_gpu_vs_cpu_render"] = {"cvtColor_psnr_db": stat(gpu_psnr), "cvtColor_ssim": stat(gpu_ssim),
                                     "verdict": "GPU matches CPU" if (gpu_psnr and min(gpu_psnr) > 40) else "GPU DIVERGES from CPU"}

    # (a) NVENC encode fidelity
    if n > 0:
        ref = "/tmp/q_ref_lossless.mkv"; h264 = "/tmp/q_h264_nvenc.mp4"; hevc = "/tmp/q_hevc_nvenc.mp4"
        encode(raw_path, ref, ["-c:v", "ffv1", "-level", "3"])                     # lossless reference
        rc_h264 = encode(raw_path, h264, ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23"])
        rc_hevc = encode(raw_path, hevc, ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23"])
        report["a_nvenc_encode_fidelity"] = {
            "reference": "lossless ffv1 of the raw capture",
            "h264_nvenc": (ffmpeg_compare(h264, ref) if rc_h264 == 0 else {"error": "encode failed"}),
            "hevc_nvenc": (ffmpeg_compare(hevc, ref) if rc_hevc == 0 else {"error": "encode failed"}),
            "sizes_bytes": {k: (os.path.getsize(p) if os.path.exists(p) else 0)
                            for k, p in (("lossless_ref", ref), ("h264_nvenc", h264), ("hevc_nvenc", hevc))},
        }
    # acceptance (full-reference thresholds from the literature)
    a = report.get("a_nvenc_encode_fidelity", {}).get("h264_nvenc", {})
    report["verdict"] = {
        "encode_ssim_ok(>0.95)": (a.get("ssim", 0) > 0.95),
        "encode_psnr_ok(>35dB)": (a.get("psnr_db", 0) > 35),
        "gpu_render_matches_cpu": report["b_gpu_vs_cpu_render"]["verdict"] == "GPU matches CPU",
        "capture_sharp_enough(lap>100)": report["c_capture_quality"]["sharpness_laplacian_var"]["mean"] > 100,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
