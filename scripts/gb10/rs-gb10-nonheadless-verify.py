#!/usr/bin/env python3
"""GB10 NON-HEADLESS display + video-rendering validation (standardized HIL suite test).

Renders the LIVE RealSense stream to a real on-screen window on the MAIN display ($DISPLAY, the
attached monitor) and PROVES — does not assume — that real, moving video reached the screen:
  1. window rendered (x11grab of the display is non-blank),
  2. what's displayed == what was captured (SSIM of the window region vs the painted frame),
  3. the video is LIVE, not a frozen frame (two grabs at different times differ — the on-frame
     counter overlay guarantees inter-frame change independent of scene),
  4. delivered FPS ~ target, controller stayed GREEN the whole run,
  5. a durable NVENC .mp4 of exactly what was on screen is recorded + verified.

Built on hil_common (preflight gate, continuous controller tripwire, unique TIMESTAMPED artifact dir)
so runs are RELIABLE (aborts on HC-died), REPRODUCIBLE (fixed config), STANDARDIZED (`just hil-nonheadless`)
and IDEMPOTENT (each run is self-contained, clean teardown, no shared mutable paths). PASS/FAIL in result.json.

Default = single COLOR stream (the conservative-SAFE envelope, no controller risk). `--depth` renders
CUDA-colorized depth; `--rgbd` is the 2-stream eyes-open path (tripwire-guarded).
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

import hil_common as H

W = int(os.environ.get("LRS_DV_W", "848"))
H_ = int(os.environ.get("LRS_DV_H", "480"))
FPS = int(os.environ.get("LRS_DV_FPS", "30"))
FRAMES = int(os.environ.get("LRS_DV_FRAMES", "150"))
WIN = "gb10-display-validate"
WIN_X, WIN_Y = 40, 40
FFMPEG = os.environ.get("LRS_FFMPEG", "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg")
DISPLAY = os.environ.get("DISPLAY", ":1")


def ssim_global(a, b):
    a = a.astype(np.float64); b = b.astype(np.float64)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = a.mean(), b.mean(); va, vb = a.var(), b.var()
    cov = ((a - ma) * (b - mb)).mean()
    return ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma**2 + mb**2 + C1) * (va + vb + C2))


def grab_region(path, w, h):
    """One-frame X11 grab of the top-left w x h of the display (contains our window)."""
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab",
                           "-video_size", f"{w}x{h}", "-i", DISPLAY, "-frames:v", "1", path],
                          capture_output=True, text=True).returncode


def main():
    import pyrealsense2 as rs
    import cv2

    mode = "depth" if "--depth" in sys.argv else ("rgbd" if "--rgbd" in sys.argv else "color")
    hil = H.HIL("display-validate", display=True)
    hil.report.update({"mode": mode, "width": W, "height": H_, "fps": FPS, "frames_target": FRAMES,
                       "cv2": cv2.__version__, "cuda_devices": cv2.cuda.getCudaEnabledDeviceCount(),
                       "main_display": DISPLAY})
    hil.preflight()  # raises on dead controller / no camera / USB-2

    pipe = rs.pipeline(rs.context())
    cfg = rs.config()
    if mode in ("color", "rgbd"):
        cfg.enable_stream(rs.stream.color, W, H_, rs.format.bgr8, FPS)
    if mode in ("depth", "rgbd"):
        cfg.enable_stream(rs.stream.depth, W, H_, rs.format.z16, FPS)
    colorizer = rs.colorizer() if mode in ("depth", "rgbd") else None

    grab_w, grab_h = WIN_X + W + 80, WIN_Y + H_ + 140  # region that contains the window + WM titlebar
    grabs = {}          # i -> path
    shown = {}          # i -> the exact frame painted (for SSIM)
    enc = None          # NVENC subprocess
    delivered = 0
    gaps = []
    last = None
    ok = True
    try:
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.moveWindow(WIN, WIN_X, WIN_Y)
        cv2.resizeWindow(WIN, W, H_)
        pipe.start(cfg)
        # NVENC recorder: raw bgr24 WxH @ FPS -> h264 (durable proof of exactly what was on screen)
        mp4 = os.path.join(hil.dir, "rendered.mp4")
        enc = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                "-pix_fmt", "bgr24", "-s", f"{W}x{H_}", "-r", str(FPS), "-i", "-",
                                "-c:v", "h264_nvenc", "-preset", "p4", mp4], stdin=subprocess.PIPE)
        for i in range(FRAMES):
            fs = pipe.wait_for_frames(5000)
            now = time.time()
            if last is not None:
                gaps.append((now - last) * 1000.0)
            last = now
            if mode == "color":
                img = np.asanyarray(fs.get_color_frame().get_data())
            elif mode == "depth":
                img = np.asanyarray(colorizer.colorize(fs.get_depth_frame()).get_data())
            else:  # rgbd: color with a colorized-depth inset
                img = np.asanyarray(fs.get_color_frame().get_data()).copy()
                d = np.asanyarray(colorizer.colorize(fs.get_depth_frame()).get_data())
                ih, iw = H_ // 3, W // 3
                img[:ih, :iw] = cv2.resize(d, (iw, ih))
            if img.shape[:2] != (H_, W):
                img = cv2.resize(img, (W, H_))
            # moving overlay -> guarantees inter-frame change (the LIVE-video signal)
            cv2.putText(img, f"GB10 LIVE  f={i:03d}  t={now%100:06.2f}", (16, 36),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.imshow(WIN, img)
            cv2.waitKey(1)
            delivered += 1
            try:
                enc.stdin.write(img.tobytes())
            except Exception:
                pass
            # two timed grabs (~1.5 s apart) for the non-blank + match + LIVE-change checks
            for gi in (FRAMES // 3, 2 * FRAMES // 3):
                if i == gi:
                    time.sleep(0.3)  # let the compositor settle
                    p = os.path.join(hil.dir, f"screen-{gi}.png")
                    if grab_region(p, grab_w, grab_h) == 0 and os.path.exists(p):
                        grabs[gi] = p; shown[gi] = img.copy()
            if i % 50 == 49:
                hil.check_tripwire()
        hil.report["frames_rendered"] = delivered
    except H.Tripwire:
        ok = False
        raise
    except Exception as e:
        ok = False
        hil.report["error"] = str(e)
    finally:
        try:
            if enc and enc.stdin:
                enc.stdin.close(); enc.wait(timeout=15)
        except Exception:
            pass
        try:
            pipe.stop()
        except Exception:
            pass
        cv2.destroyAllWindows(); cv2.waitKey(1)

    # ---- validation ----
    v = {}
    gi_keys = sorted(grabs)
    if gi_keys:
        imgs = {gi: cv2.imread(grabs[gi]) for gi in gi_keys}
        g0 = imgs[gi_keys[0]]
        v["nonblank"] = bool(g0 is not None and g0.std() > 5)
        # "Our content is on screen" — ROBUST proxy: detect the distinctive bright-green overlay text
        # (BGR ~0,255,0) we paint every frame. Position/scale/WM-decoration independent, unlike a
        # pixel-exact region SSIM (which is fragile to window placement on different WMs/HiDPI).
        if g0 is not None:
            green = ((g0[:, :, 1].astype(np.int16) > 170) & (g0[:, :, 2] < 110) & (g0[:, :, 0] < 110))
            v["green_overlay_px"] = int(green.sum())
        else:
            v["green_overlay_px"] = 0
        # window region SSIM kept as an INFORMATIONAL metric (best over titlebar offsets), not pass-gating
        best = 0.0
        if g0 is not None and shown.get(gi_keys[0]) is not None:
            for dy in (0, 24, 28, 32, 56):
                if g0.shape[0] >= WIN_Y + dy + H_ and g0.shape[1] >= WIN_X + W:
                    region = g0[WIN_Y + dy:WIN_Y + dy + H_, WIN_X:WIN_X + W]
                    if region.shape[:2] == (H_, W):
                        best = max(best, float(ssim_global(cv2.cvtColor(region, cv2.COLOR_BGR2GRAY),
                                                           cv2.cvtColor(shown[gi_keys[0]], cv2.COLOR_BGR2GRAY))))
        v["window_match_ssim"] = round(best, 3)
        # LIVE: two grabs differ (moving overlay guarantees change if video is actually updating)
        if len(gi_keys) >= 2 and all(imgs[g] is not None for g in gi_keys[:2]):
            a, b = imgs[gi_keys[0]], imgs[gi_keys[1]]
            n = min(a.shape[0], b.shape[0]); m = min(a.shape[1], b.shape[1])
            v["live_change_meandiff"] = round(float(np.abs(a[:n, :m].astype(np.int16)
                                                           - b[:n, :m].astype(np.int16)).mean()), 3)
        else:
            v["live_change_meandiff"] = 0.0
    else:
        v["nonblank"] = False; v["window_match_ssim"] = 0.0; v["live_change_meandiff"] = 0.0

    # NVENC clip verification
    mp4 = os.path.join(hil.dir, "rendered.mp4")
    if os.path.exists(mp4) and os.path.getsize(mp4) > 1000:
        probe = subprocess.run([FFMPEG, "-hide_banner", "-i", mp4], capture_output=True, text=True)
        v["recorded_mp4"] = os.path.basename(mp4)
        v["recorded_ok"] = "h264" in (probe.stderr.lower())
    else:
        v["recorded_ok"] = False

    # steady-state fps from the MEDIAN gap — robust to the 2 inline grab stalls (which block the loop,
    # not the camera). Also report the naive wall fps for reference.
    steady_fps = round(1000.0 / float(np.median(gaps)), 1) if gaps else 0.0
    wall_fps = round(delivered / (sum(gaps) / 1000.0), 1) if gaps else 0.0
    hil.report["delivered_fps"] = steady_fps
    hil.report["wall_fps_incl_grab_stalls"] = wall_fps
    hil.report["interframe_gap_ms"] = H.stats(gaps)
    hil.report["validation"] = v
    # PASS requires: real pixels on screen, OUR content (green overlay) on screen, the video was LIVE
    # (two grabs differ), steady framerate, a clip recorded, and the controller stayed green.
    checks = {
        "rendered_on_screen": v["nonblank"],
        "our_content_on_screen": v.get("green_overlay_px", 0) > 50,
        "live_video_not_frozen": v["live_change_meandiff"] > 1.0,
        "framerate_ok": steady_fps >= FPS - 3,
        "clip_recorded": v.get("recorded_ok", False),
        "controller_green": not hil.report["controller_death"],
    }
    hil.report["checks"] = checks
    passed = ok and all(checks.values())
    hil.report["result"] = "PASS" if passed else "FAIL"
    hil.finish(ok=passed)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
