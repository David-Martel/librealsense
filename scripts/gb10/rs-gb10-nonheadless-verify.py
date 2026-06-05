#!/usr/bin/env python3
"""GB10 NON-HEADLESS display + video-rendering validation AND interactive debug viewer.

Renders the LIVE RealSense stream to a real on-screen window on the MAIN display ($DISPLAY) and either:
  * validates it (default) — PROVES real, moving video reached the screen and PASS/FAILs, or
  * `--interactive` — a live debug viewer with on-screen per-frame telemetry + keyboard hooks to switch
    stream size/rate/profile and the camera's features on the fly.

SMOOTHNESS: the render loop is TIGHT — `wait_for_frames -> draw -> imshow -> waitKey(1)`. All blocking
work (the x11grab validation captures and the journalctl controller tripwire) runs on a BACKGROUND
monitor thread, so the on-screen video is smooth and flicker-free (the earlier per-few-seconds pause was
inline grabs stalling the loop). NVENC recording (validation mode) writes best-effort and never blocks.

DEBUG HOOKS (always-on HUD): wall fps + render dt, device frame number, hardware/sensor timestamp +
domain, and live frame METADATA (actual_exposure, gain, actual_fps, frame_counter, sensor/arrival
timestamps) when the device exposes them.

INTERACTIVE CONTROLS (`--interactive`, keys go to the focused window on the display):
  1..9  select stream profile (size x fps) from the current family    c/d  color / depth family
  e  emitter on/off      a  auto-exposure on/off      [ / ]  exposure - / +      - / =  gain - / +
  l / L  laser power - / +      p  cycle visual preset      f  freeze/unfreeze      h  toggle help
  s  snapshot PNG to the artifact dir      q  quit
Default & interactive both default to a SINGLE stream (the conservative-safe envelope); multi-stream is
not entered automatically. hil_common gives preflight + continuous tripwire + a timestamped artifact dir.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

import hil_common as H

WARMUP = 15  # frames to skip before measuring smoothness (pipeline/NVENC startup jitter)

DISPLAY = os.environ.get("DISPLAY", ":1")
FFMPEG = os.environ.get("LRS_FFMPEG", "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg")
WIN = "gb10-display-validate"
WIN_X, WIN_Y = 40, 40
# single-stream-safe profiles (w, h, fps) per family
COLOR_PROFILES = [(320, 240, 30), (424, 240, 60), (640, 480, 30), (640, 480, 60),
                  (848, 480, 30), (848, 480, 60), (1280, 720, 30), (1280, 720, 6)]
DEPTH_PROFILES = [(424, 240, 30), (424, 240, 60), (640, 480, 30), (640, 480, 60),
                  (848, 480, 30), (848, 480, 60), (1280, 720, 30), (256, 144, 90)]
DEFAULT_IDX = 4  # 848x480x30


def ssim_global(a, b):
    a = a.astype(np.float64); b = b.astype(np.float64)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = a.mean(), b.mean(); va, vb = a.var(), b.var()
    cov = ((a - ma) * (b - mb)).mean()
    return ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma ** 2 + mb ** 2 + C1) * (va + vb + C2))


class Cam:
    """Live single-stream RealSense wrapper with hot profile-switch + camera-feature controls."""
    def __init__(self, rs, family="color", idx=DEFAULT_IDX):
        self.rs = rs; self.ctx = rs.context()
        self.family = family; self.idx = idx
        self.pipe = None; self.profile = None; self.whf = None
        self.depth_sensor = None; self.color_sensor = None

    def _profiles(self):
        return COLOR_PROFILES if self.family == "color" else DEPTH_PROFILES

    def start(self):
        rs = self.rs
        w, h, fps = self._profiles()[self.idx]
        cfg = rs.config()
        if self.family == "color":
            cfg.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
        else:
            cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
        self.pipe = rs.pipeline(self.ctx)
        self.profile = self.pipe.start(cfg)
        self.whf = (w, h, fps)
        dev = self.profile.get_device()
        try:
            self.depth_sensor = dev.first_depth_sensor()
        except Exception:
            self.depth_sensor = None
        self.color_sensor = None
        for s in dev.query_sensors():
            try:
                if "RGB" in s.get_info(rs.camera_info.name):
                    self.color_sensor = s
            except Exception:
                pass

    def stop(self):
        try:
            if self.pipe:
                self.pipe.stop()
        except Exception:
            pass
        self.pipe = None

    def switch(self, family=None, idx=None):
        if family is not None:
            self.family = family
            self.idx = min(self.idx, len(self._profiles()) - 1)
        if idx is not None and 0 <= idx < len(self._profiles()):
            self.idx = idx
        self.stop(); self.start()

    def opt_sensor(self, name):
        # which sensor owns an option
        rs = self.rs
        if name in ("emitter_enabled", "laser_power") and self.depth_sensor:
            return self.depth_sensor
        return self.color_sensor or self.depth_sensor

    def bump(self, optname, delta=None, toggle=False):
        rs = self.rs
        opt = getattr(rs.option, optname, None)
        s = self.opt_sensor(optname)
        if opt is None or s is None or not s.supports(opt):
            return None
        try:
            rng = s.get_option_range(opt)
            cur = s.get_option(opt)
            if toggle:
                val = rng.min if cur > rng.min else rng.max
            else:
                step = rng.step if rng.step else (rng.max - rng.min) / 20.0
                val = max(rng.min, min(rng.max, cur + delta * step * 8))
            s.set_option(opt, val)
            return s.get_option(opt)
        except Exception:
            return None


def md_lines(rs, frame):
    """Per-frame device telemetry lines (timestamp + metadata), defensively."""
    out = []
    try:
        ts = frame.get_timestamp(); dom = str(frame.get_frame_timestamp_domain()).split(".")[-1]
        out.append(f"fnum={frame.get_frame_number()}  ts={ts:.1f}ms [{dom}]")
    except Exception:
        pass
    md = []
    for key in ("frame_counter", "actual_fps", "actual_exposure", "gain_level",
                "sensor_timestamp", "time_of_arrival", "backend_timestamp"):
        mv = getattr(rs.frame_metadata_value, key, None)
        try:
            if mv is not None and frame.supports_frame_metadata(mv):
                v = frame.get_frame_metadata(mv)
                md.append(f"{key}={v}")
        except Exception:
            pass
    # chunk metadata onto two lines
    for i in range(0, len(md), 3):
        out.append("  ".join(md[i:i + 3]))
    return out


def main():
    import pyrealsense2 as rs
    import cv2

    interactive = "--interactive" in sys.argv
    family = "depth" if "--depth" in sys.argv else "color"
    duration = float(next((a.split("=")[1] for a in sys.argv if a.startswith("--duration=")), "0")) or None
    frames_target = int(os.environ.get("LRS_DV_FRAMES", "150"))
    record = (not interactive) or ("--record" in sys.argv)

    hil = H.HIL("display-validate", display=True)
    hil.report.update({"mode": "interactive" if interactive else "validate", "family": family,
                       "main_display": DISPLAY, "cv2": cv2.__version__})
    hil.preflight()

    cam = Cam(rs, family=family)
    cam.start()
    colorizer = rs.colorizer()

    # ---- background monitor: tripwire (always) + timed validation grabs (validate mode only) ----
    shared = {"img": None, "stop": False}
    grabs = {}
    grab_lock = threading.Lock()

    def monitor():
        t0 = time.time()
        did = set()
        while not shared["stop"]:
            try:
                hil.check_tripwire()
            except H.Tripwire:
                shared["stop"] = True
                shared["controller_dead"] = True
                break
            if not interactive:
                el = time.time() - t0
                for tag, when in (("a", 2.0), ("b", 4.0)):
                    if tag not in did and el >= when:
                        did.add(tag)
                        with grab_lock:
                            snap = None if shared["img"] is None else shared["img"].copy()
                        p = os.path.join(hil.dir, f"screen-{tag}.png")
                        rc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f",
                                             "x11grab", "-video_size", "1100x760", "-i", DISPLAY,
                                             "-frames:v", "1", p], capture_output=True, text=True).returncode
                        if rc == 0 and os.path.exists(p):
                            grabs[tag] = (p, snap)
            time.sleep(1.0)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()

    enc = None
    frame_q = queue.Queue(maxsize=4)   # bounded: if NVENC falls behind, DROP frames (never stall render)
    wr = None
    if record:
        w, h, fps = cam.whf
        mp4 = os.path.join(hil.dir, "rendered.mp4")
        enc = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                                "-c:v", "h264_nvenc", "-preset", "p4", mp4], stdin=subprocess.PIPE)

        def _writer():
            while True:
                item = frame_q.get()
                if item is None:
                    break
                try:
                    enc.stdin.write(item)
                except Exception:
                    pass
        wr = threading.Thread(target=_writer, daemon=True); wr.start()

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL); cv2.moveWindow(WIN, WIN_X, WIN_Y)
    show_help = interactive
    frozen = False
    last = None; gaps = []; delivered = 0; i = 0; ok = True
    status = ""
    HELP = ["[1-9] profile  c/d color/depth  e emitter  a auto-exp  [ ] exp  - = gain",
            "l/L laser  p preset  f freeze  s snapshot  h help  q quit"]
    try:
        cv2.resizeWindow(WIN, cam.whf[0], cam.whf[1])
        t_start = time.time()
        while True:
            if not frozen:
                try:
                    fs = cam.pipe.wait_for_frames(5000)
                except Exception as e:
                    status = f"wait_for_frames: {e}"; continue
                frame = fs.get_color_frame() if cam.family == "color" else fs.get_depth_frame()
                if cam.family == "color":
                    img = np.asanyarray(frame.get_data())
                else:
                    img = np.asanyarray(colorizer.colorize(frame).get_data())
                now = time.time()
                if last is not None and i > WARMUP:   # skip startup jitter from the smoothness metric
                    gaps.append((now - last) * 1000.0)
                last = now
                delivered += 1; i += 1
                meta = md_lines(rs, frame)
                cur_img = img
            # ---- HUD overlay (drawn on a copy so the recorded/raw frame is what we measure) ----
            disp = cur_img.copy()
            w, h, fps = cam.whf
            dt = gaps[-1] if gaps else 0.0
            wall_fps = 1000.0 / dt if dt > 0 else 0.0
            cv2.putText(disp, f"GB10 LIVE f={i:04d} {w}x{h}@{fps} {cam.family}  {wall_fps:4.1f}fps dt={dt:4.1f}ms",
                        (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            for li, line in enumerate(meta):
                cv2.putText(disp, line, (12, 52 + li * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            if status:
                cv2.putText(disp, status, (12, h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
            if show_help:
                for li, line in enumerate(HELP):
                    cv2.putText(disp, line, (12, h - 60 + li * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
            cv2.imshow(WIN, disp)
            with grab_lock:
                shared["img"] = disp
            # hand the frame to the NVENC writer thread (non-blocking; drop if the encoder is behind)
            if enc and not frozen and img.shape[1] == cam.whf[0] and img.shape[0] == cam.whf[1]:
                try:
                    frame_q.put_nowait(np.ascontiguousarray(disp).tobytes())
                except queue.Full:
                    pass

            key = cv2.waitKey(1) & 0xFF
            if shared.get("stop"):
                ok = not shared.get("controller_dead", False); break
            if interactive:
                if key == ord('q'):
                    break
                elif key in [ord(str(n)) for n in range(1, 10)]:
                    idx = key - ord('1')
                    if idx < len(cam._profiles()):
                        cam.switch(idx=idx); cv2.resizeWindow(WIN, cam.whf[0], cam.whf[1])
                        status = f"profile -> {cam.whf}"; last = None
                elif key in (ord('c'), ord('d')):
                    cam.switch(family=("color" if key == ord('c') else "depth"))
                    cv2.resizeWindow(WIN, cam.whf[0], cam.whf[1]); status = f"family -> {cam.family} {cam.whf}"; last = None
                elif key == ord('e'):
                    v = cam.bump("emitter_enabled", toggle=True); status = f"emitter -> {v}"
                elif key == ord('a'):
                    v = cam.bump("enable_auto_exposure", toggle=True); status = f"auto_exposure -> {v}"
                elif key == ord('['):
                    cam.bump("enable_auto_exposure", delta=0); v = cam.bump("exposure", delta=-1); status = f"exposure -> {v}"
                elif key == ord(']'):
                    v = cam.bump("exposure", delta=+1); status = f"exposure -> {v}"
                elif key == ord('-'):
                    v = cam.bump("gain", delta=-1); status = f"gain -> {v}"
                elif key in (ord('='), ord('+')):
                    v = cam.bump("gain", delta=+1); status = f"gain -> {v}"
                elif key == ord('l'):
                    v = cam.bump("laser_power", delta=-1); status = f"laser -> {v}"
                elif key == ord('L'):
                    v = cam.bump("laser_power", delta=+1); status = f"laser -> {v}"
                elif key == ord('p'):
                    v = cam.bump("visual_preset", delta=+1); status = f"preset -> {v}"
                elif key == ord('f'):
                    frozen = not frozen; status = "FROZEN" if frozen else "live"
                elif key == ord('h'):
                    show_help = not show_help
                elif key == ord('s'):
                    p = os.path.join(hil.dir, f"snapshot-{i:04d}.png"); cv2.imwrite(p, disp); status = f"saved {os.path.basename(p)}"
            # exit conditions
            if duration and (time.time() - t_start) >= duration:
                break
            if not interactive and i >= frames_target:
                break
    except H.Tripwire:
        ok = False
    finally:
        shared["stop"] = True
        try:
            if wr:
                frame_q.put(None); wr.join(timeout=5)
            if enc and enc.stdin:
                enc.stdin.close(); enc.wait(timeout=15)
        except Exception:
            pass
        cam.stop()
        cv2.destroyAllWindows(); cv2.waitKey(1)
        mon.join(timeout=2)

    # ---- steady fps + smoothness (stutter) + (validate) PASS/FAIL ----
    steady_fps = round(1000.0 / float(np.median(gaps)), 1) if gaps else 0.0
    med = float(np.median(gaps)) if gaps else 0.0
    stutters = int(sum(1 for g in gaps if g > max(66.0, 1.8 * med))) if gaps else 0  # post-warmup gaps > ~2 frames
    hil.report["frames_rendered"] = delivered
    hil.report["delivered_fps"] = steady_fps
    hil.report["interframe_gap_ms"] = H.stats(gaps)
    hil.report["stutter_count"] = stutters   # post-warmup frame gaps > ~2x interval (visible pauses)
    hil.report["final_profile"] = list(cam.whf) if cam.whf else None

    if interactive:
        hil.report["result"] = "PASS" if (ok and not hil.report["controller_death"]) else "FAIL"
        hil.finish(ok=ok)
        return 0

    v = {}
    keys = sorted(grabs)
    if keys:
        imgs = {k: __import__("cv2").imread(grabs[k][0]) for k in keys}
        g0 = imgs[keys[0]]
        v["nonblank"] = bool(g0 is not None and g0.std() > 5)
        if g0 is not None:
            green = ((g0[:, :, 1].astype(np.int16) > 170) & (g0[:, :, 2] < 110) & (g0[:, :, 0] < 110))
            v["green_overlay_px"] = int(green.sum())
        else:
            v["green_overlay_px"] = 0
        if len(keys) >= 2 and all(imgs[k] is not None for k in keys[:2]):
            a, b = imgs[keys[0]], imgs[keys[1]]
            n = min(a.shape[0], b.shape[0]); m = min(a.shape[1], b.shape[1])
            v["live_change_meandiff"] = round(float(np.abs(a[:n, :m].astype(np.int16) - b[:n, :m].astype(np.int16)).mean()), 3)
        else:
            v["live_change_meandiff"] = 0.0
    else:
        v.update({"nonblank": False, "green_overlay_px": 0, "live_change_meandiff": 0.0})

    mp4 = os.path.join(hil.dir, "rendered.mp4")
    if os.path.exists(mp4) and os.path.getsize(mp4) > 1000:
        probe = subprocess.run([FFMPEG, "-hide_banner", "-i", mp4], capture_output=True, text=True)
        v["recorded_ok"] = "h264" in probe.stderr.lower()
    else:
        v["recorded_ok"] = False

    hil.report["validation"] = v
    checks = {
        "rendered_on_screen": v["nonblank"],
        "our_content_on_screen": v.get("green_overlay_px", 0) > 50,
        "live_video_not_frozen": v["live_change_meandiff"] > 1.0,
        "framerate_ok": steady_fps >= cam.whf[2] - 3,
        "smooth_no_stutter": stutters <= 1,   # smooth & flicker-free: at most 1 post-warmup hiccup
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
