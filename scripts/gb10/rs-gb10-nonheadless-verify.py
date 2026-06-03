#!/usr/bin/env python3
"""GB10 NON-HEADLESS render verification — actually paint RealSense frames on the X
display and screen-grab to PROVE real pixels rendered (don't assume). Renders the live
color stream (CUDA colorized depth optional) to an on-screen cv2 window on $DISPLAY, then
captures the screen via ffmpeg x11grab and checks the grab is non-blank AND structurally
matches the frame that was on screen (SSIM). Single color stream -> no controller risk.
Run via `just hil-nonheadless` (sets DISPLAY + CUDA cv2 env)."""
import json
import os
import subprocess
import sys
import time

import numpy as np

W, H, FPS = 848, 480, 30
FFMPEG = os.environ.get("LRS_FFMPEG", "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg")
DISPLAY = os.environ.get("DISPLAY", ":1")
WIN = "vigil-gb10-render-verify"


def ssim_global(a, b):
    a = a.astype(np.float64); b = b.astype(np.float64)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = a.mean(), b.mean(); va, vb = a.var(), b.var()
    cov = ((a - ma) * (b - mb)).mean()
    return ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma**2 + mb**2 + C1) * (va + vb + C2))


def grab_screen(path):
    """One-frame X11 grab of the whole display."""
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "x11grab", "-video_size", "1920x1080", "-i", DISPLAY,
                           "-frames:v", "1", path], capture_output=True, text=True,
                          env=dict(os.environ)).returncode


def main():
    import pyrealsense2 as rs
    import cv2

    report = {"display": DISPLAY, "cv2": cv2.__version__, "cuda": cv2.cuda.getCudaEnabledDeviceCount()}
    ctx = rs.context()
    if len(ctx.query_devices()) == 0:
        print("NO_DEVICE", file=sys.stderr); return 2
    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.color, W, H, rs.format.bgr8, FPS)

    last_shown = None
    grab_ok = False
    try:
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.moveWindow(WIN, 40, 40)
        cv2.resizeWindow(WIN, W, H)
        pipe.start(cfg)
        for i in range(120):
            fs = pipe.wait_for_frames(3000)
            cf = fs.get_color_frame()
            if not cf:
                continue
            img = np.asanyarray(cf.get_data())
            # draw an on-screen overlay so we can confirm OUR window content in the grab
            cv2.putText(img, f"GB10 RENDER {i}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            cv2.imshow(WIN, img)
            cv2.waitKey(1)             # force the window to paint
            if i == 60:
                last_shown = img.copy()
                time.sleep(0.3)        # let the compositor settle
                if grab_screen("/tmp/gb10_screen.png") == 0 and os.path.exists("/tmp/gb10_screen.png"):
                    grab_ok = True
        report["frames_rendered"] = i + 1
    except Exception as e:
        report["error"] = str(e)
    finally:
        try:
            pipe.stop(); cv2.destroyAllWindows()
        except Exception:
            pass

    # Analyze the screen grab: non-blank + does our window region match the shown frame?
    if grab_ok:
        screen = cv2.imread("/tmp/gb10_screen.png")
        report["screen_grab"] = {"shape": list(screen.shape), "mean": round(float(screen.mean()), 2),
                                 "std": round(float(screen.std()), 2),
                                 "nonblank": bool(screen.std() > 5)}
        # extract our window region (we placed it at ~40,40 + titlebar) and SSIM vs shown frame
        if last_shown is not None and screen.shape[0] >= 40 + H and screen.shape[1] >= 40 + W:
            for dy in (0, 24, 28, 32):    # account for WM titlebar height variance
                region = screen[40 + dy:40 + dy + H, 40:40 + W]
                if region.shape[:2] == (H, W):
                    s = ssim_global(cv2.cvtColor(region, cv2.COLOR_BGR2GRAY),
                                    cv2.cvtColor(last_shown, cv2.COLOR_BGR2GRAY))
                    report["screen_grab"].setdefault("window_match_ssim", round(float(s), 3))
                    if s > report["screen_grab"]["window_match_ssim"]:
                        report["screen_grab"]["window_match_ssim"] = round(float(s), 3)
        report["verdict"] = {
            "rendered_on_screen": report["screen_grab"]["nonblank"],
            "displayed_matches_captured(ssim>0.5)": report["screen_grab"].get("window_match_ssim", 0) > 0.5,
        }
    else:
        report["screen_grab"] = {"error": "x11grab failed (no display access?)"}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
