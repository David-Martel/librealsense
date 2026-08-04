#!/usr/bin/env python3
# ruff: noqa: E501, E702
"""GB10 non-headless display validation + interactive debug viewer for the RealSense.

Renders the LIVE stream to a window on the MAIN display ($DISPLAY) and either validates it (default,
PASS/FAIL) or runs an interactive debug viewer (`--interactive`) with per-frame telemetry + live
controls for stream size/rate/profile and camera features.

Design (kept deliberately simple + testable):
  * PURE helpers (no camera, no display) — argparse, profile clamping, HUD composition, grab validation
    math — are unit-exercised by `--self-test` (offline; CI/smoke safe).
  * I/O — the camera (Cam), the cv2 window + the x11grab/NVENC capture — is isolated.
  * SMOOTH render: the GUI/render loop runs on the MAIN thread (cv2 is not thread-safe); the controller
    tripwire + validation screen-grabs run on a background Monitor thread, and NVENC frames go through a
    bounded queue to a writer thread (dropped, never blocking the render). Verified 0-stutter @30fps.
  * FAIL-SAFE camera lifecycle: validate mode fails fast + clean on a stream error (deterministic); the
    interactive viewer surfaces the error on-screen and re-acquires only on an explicit 'r' keypress
    (one guarded stop/start) — never an auto retry-loop (that churn is what kills the GB10 xHCI).

Validate-mode PASS/FAIL gates (all must hold; report-only items never fail a synthetic edge case):
  rendered_on_screen      screen grab is non-blank (std > 5)
  our_content_on_screen   our green HUD overlay is visible in the grab
  live_video_not_frozen   two grabs differ (the render is live, not a still)
  framerate_ok            steady delivered fps >= profile fps - 3
  smooth_no_stutter       <= 1 inter-frame stutter
  clip_recorded           the NVENC/x264 .mp4 exists and decodes as h264
  controller_green        no xHCI/HC-death tripwire fired
  frames_monotonic        hardware frame numbers strictly increase            [continuity]
  no_dropped_frames       zero missing hw frames between consecutive numbers  [continuity, 0-tolerance]
  arrival_gap_sane        largest inter-frame wall gap within bound           [continuity]
  ts_domain_consistent    all frames share one timestamp domain               [continuity]
  depth_not_all_zero      depth frame has some reading (depth runs only)      [depth integrity]
  depth_not_saturated     depth frame is not (near-)entirely 0xFFFF           [depth integrity]
  depth_not_frozen        consecutive raw depth frames are not identical      [depth integrity]
REPORT-ONLY (logged, never gating): continuity.max_gap_ms / ts_domain, depth_integrity.valid_ratio /
  in_range_ratio / valid_ratio_in_band — scene-dependent (e.g. aimed at sky) so they would false-fail.
The continuity + depth-integrity math are PURE (continuity_checks / depth_checks) and unit-exercised
offline by --self-test with synthetic frame-number / gap / domain sequences and synthetic z16 arrays.

Controls (`--interactive`): 1-9 profile(size x fps)  c/d color/depth  e emitter  a auto-exp  [ ] exposure
  - = gain   l/L laser   p preset   f freeze   r re-acquire after a stream error   s snapshot   h help   q quit
"""
import argparse
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

import hil_common as H

DISPLAY = os.environ.get("DISPLAY", ":1")
FFMPEG = os.environ.get("LRS_FFMPEG", "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg")
WIN = "gb10-display-validate"
WIN_X, WIN_Y = 40, 40
WARMUP = 15
COLOR_PROFILES = [(320, 240, 30), (424, 240, 60), (640, 480, 30), (640, 480, 60),
                  (848, 480, 30), (848, 480, 60), (1280, 720, 30), (1280, 720, 6)]
DEPTH_PROFILES = [(424, 240, 30), (424, 240, 60), (640, 480, 30), (640, 480, 60),
                  (848, 480, 30), (848, 480, 60), (1280, 720, 30), (256, 144, 90)]
DEFAULT_IDX = 4
METADATA_KEYS = ("frame_counter", "actual_fps", "actual_exposure", "gain_level",
                 "sensor_timestamp", "time_of_arrival", "backend_timestamp")


# ----------------------------- PURE helpers (no camera / no display) -----------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="rs-gb10-display-validate",
        description="Non-headless RealSense display validation + interactive debug viewer (GB10).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--interactive", action="store_true",
                   help="live debug viewer with keyboard controls (runs until 'q')")
    p.add_argument("--stream", choices=("color", "depth"), default="color", help="stream family")
    p.add_argument("--depth", action="store_true", help="alias for --stream depth")
    p.add_argument("--rgbd", action="store_true", help=argparse.SUPPRESS)  # accepted, mapped to color
    p.add_argument("--profile", type=int, default=DEFAULT_IDX, metavar="IDX",
                   help="initial profile index into the family's size/fps list")
    p.add_argument("--serial", default=os.environ.get("LRS_DEVICE_SERIAL", ""), metavar="SERIAL",
                   help="bind the SDK pipeline to one camera (or set LRS_DEVICE_SERIAL)")
    p.add_argument("--duration", type=float, default=None, metavar="S",
                   help="run for S seconds (default: validate runs --frames frames)")
    p.add_argument("--frames", type=int, default=150, help="validate-mode frame count")
    p.add_argument("--record", action=argparse.BooleanOptionalAction, default=None,
                   help="record an NVENC .mp4 of the rendered output (default: on for validate)")
    p.add_argument("--self-test", action="store_true",
                   help="run offline self-tests (no camera, no display) and exit")
    return p


def resolve_args(argv):
    args = build_parser().parse_args(argv)
    if args.depth:
        args.stream = "depth"
    if args.record is None:
        args.record = not args.interactive
    return args


def clamp_idx(idx, family):
    n = len(COLOR_PROFILES if family == "color" else DEPTH_PROFILES)
    return max(0, min(int(idx), n - 1))


def profile_for(family, idx):
    return (COLOR_PROFILES if family == "color" else DEPTH_PROFILES)[clamp_idx(idx, family)]


def compose_hud(img, fps_line, meta_lines, status="", help_lines=None):
    """Draw the debug HUD onto a frame copy. cv2 drawing is headless-safe (no display needed)."""
    import cv2
    disp = img.copy()
    h = disp.shape[0]
    cv2.putText(disp, fps_line, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    for li, line in enumerate(meta_lines):
        cv2.putText(disp, line, (12, 52 + li * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    if status:
        cv2.putText(disp, status, (12, h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
    for li, line in enumerate(help_lines or []):
        cv2.putText(disp, line, (12, h - 60 + li * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return disp


def grab_checks(img_a, img_b):
    """Validation math over two screen grabs (numpy BGR arrays). Pure; used by --self-test too."""
    out = {"nonblank": False, "green_overlay_px": 0, "live_change_meandiff": 0.0}
    if img_a is None:
        return out
    out["nonblank"] = bool(img_a.std() > 5)
    green = ((img_a[:, :, 1].astype(np.int16) > 170) & (img_a[:, :, 2] < 110) & (img_a[:, :, 0] < 110))
    out["green_overlay_px"] = int(green.sum())
    if img_b is not None:
        n = min(img_a.shape[0], img_b.shape[0]); m = min(img_a.shape[1], img_b.shape[1])
        out["live_change_meandiff"] = round(
            float(np.abs(img_a[:n, :m].astype(np.int16) - img_b[:n, :m].astype(np.int16)).mean()), 3)
    return out


def depth_checks(depth_u16, depth_scale, min_m, max_m, prev_u16=None,
                 valid_lo=0.05, valid_hi=0.999):
    """Depth-frame integrity over a raw z16 numpy array (uint16). Pure; used by --self-test too.

    `depth_u16` is the RAW sensor frame (0 == no-reading, 0xFFFF == saturated); meters = raw*depth_scale.
    Returns:
      valid_ratio        fraction of non-zero (has-a-reading) pixels
      in_range_ratio     fraction of valid pixels whose metric depth lies in [min_m, max_m]
      not_all_zero       frame has SOME reading (gate)            — fails a dead/all-zero stream
      not_saturated      frame is not (near-)entirely 0xFFFF (gate)
      valid_ratio_in_band  valid_ratio within [valid_lo, valid_hi]  — REPORT-ONLY (scene-dependent)
      frozen             True iff exactly identical to prev_u16    — gate (true freeze only; exact equality)
    """
    out = {"valid_ratio": 0.0, "in_range_ratio": 0.0, "not_all_zero": False,
           "not_saturated": False, "valid_ratio_in_band": False, "frozen": None}
    if depth_u16 is None:
        return out
    d = np.asarray(depth_u16)
    total = int(d.size)
    if total == 0:
        return out
    valid = d != 0
    nvalid = int(valid.sum())
    out["valid_ratio"] = round(nvalid / total, 4)
    out["not_all_zero"] = nvalid > 0
    # saturated == max z16; a frame that is (almost) entirely saturated is a fault
    sat = int((d == 0xFFFF).sum())
    out["not_saturated"] = (sat / total) < 0.99
    if nvalid > 0:
        meters = d[valid].astype(np.float64) * float(depth_scale)
        in_range = int(((meters >= min_m) & (meters <= max_m)).sum())
        out["in_range_ratio"] = round(in_range / nvalid, 4)
    out["valid_ratio_in_band"] = bool(valid_lo <= out["valid_ratio"] <= valid_hi)
    if prev_u16 is not None:
        p = np.asarray(prev_u16)
        # exact equality == a true freeze (real depth is noisy frame-to-frame); shape-mismatch != frozen
        out["frozen"] = bool(p.shape == d.shape and np.array_equal(p, d))
    return out


def continuity_checks(frame_numbers, gaps_ms=None, domains=None, gap_max_ms=None):
    """Frame continuity / metadata sanity over the run. Pure; used by --self-test too.

    frame_numbers   list of per-frame hardware frame numbers (in arrival order)
    gaps_ms         list of inter-frame wall-clock gaps in ms (the run's existing `gaps`)
    domains         list of per-frame timestamp-domain strings (e.g. "Global Time"/"System Time")
    gap_max_ms      sane upper bound for a single arrival gap (default: 5x the median gap, floor 250ms)
    Returns:
      delivered        count of frame numbers seen
      monotonic        frame numbers strictly increase (gate)
      dropped_frames   sum of (gap-1) over consecutive increasing pairs == missing hw frames (gate, 0-tol)
      max_gap_ms       largest inter-frame wall gap (report)
      gap_sane         max_gap_ms <= gap_max_ms (gate)
      ts_domain        the (last) timestamp domain string seen (report)
      ts_domain_consistent  all frames share one domain (gate)
    """
    fnums = [int(x) for x in (frame_numbers or [])]
    out = {"delivered": len(fnums), "monotonic": True, "dropped_frames": 0,
           "max_gap_ms": 0.0, "gap_sane": True, "ts_domain": None, "ts_domain_consistent": True}
    dropped = 0
    for prev, cur in zip(fnums, fnums[1:]):
        if cur <= prev:
            out["monotonic"] = False
        else:
            dropped += (cur - prev) - 1
    out["dropped_frames"] = int(dropped)
    g = [float(x) for x in (gaps_ms or [])]
    if g:
        out["max_gap_ms"] = round(max(g), 3)
        if gap_max_ms is None:
            gap_max_ms = max(250.0, 5.0 * float(np.median(g)))
        out["gap_sane"] = bool(out["max_gap_ms"] <= gap_max_ms)
    doms = [d for d in (domains or []) if d is not None]
    if doms:
        out["ts_domain"] = doms[-1]
        out["ts_domain_consistent"] = (len(set(doms)) == 1)
    return out


# ----------------------------- camera I/O (fail-safe lifecycle) -----------------------------
class Cam:
    """Single-stream RealSense wrapper: hot profile-switch, feature controls, fail-safe re-acquire."""
    def __init__(self, rs, family="color", idx=DEFAULT_IDX, serial=""):
        self.rs = rs; self.ctx = rs.context()
        self.family = family; self.idx = clamp_idx(idx, family)
        self.serial = serial
        self.device_serial = None
        self.pipe = None; self.whf = None
        self.depth_sensor = None; self.color_sensor = None

    def start(self):
        rs = self.rs
        w, h, fps = profile_for(self.family, self.idx)
        cfg = rs.config()
        if self.serial:
            cfg.enable_device(self.serial)
        if self.family == "color":
            cfg.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
        else:
            cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
        self.pipe = rs.pipeline(self.ctx)
        prof = self.pipe.start(cfg)
        self.whf = (w, h, fps)
        dev = prof.get_device()
        try:
            self.device_serial = dev.get_info(rs.camera_info.serial_number)
        except Exception:
            self.device_serial = None
        try:
            self.depth_sensor = dev.first_depth_sensor()
        except Exception:
            self.depth_sensor = None
        self.color_sensor = next((s for s in dev.query_sensors()
                                  if "RGB" in s.get_info(rs.camera_info.name)), None)

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
        if idx is not None:
            self.idx = clamp_idx(idx, self.family)
        else:
            self.idx = clamp_idx(self.idx, self.family)
        self.stop(); self.start()

    def reacquire(self, backoff=0.5):
        """ONE guarded stop+start (caller-driven). Never an auto retry-loop (xHCI churn safety)."""
        self.stop()
        time.sleep(backoff)
        self.start()

    def read(self, timeout_ms=5000):
        """Return the frame for the active family, or None on a (caught) stream error."""
        try:
            fs = self.pipe.wait_for_frames(timeout_ms)
        except Exception:
            return None
        return fs.get_color_frame() if self.family == "color" else fs.get_depth_frame()

    def opt_sensor(self, name):
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
            rng = s.get_option_range(opt); cur = s.get_option(opt)
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
    out = []
    try:
        ts = frame.get_timestamp(); dom = str(frame.get_frame_timestamp_domain()).split(".")[-1]
        out.append(f"fnum={frame.get_frame_number()}  ts={ts:.1f}ms [{dom}]")
    except Exception:
        pass
    md = []
    for key in METADATA_KEYS:
        mv = getattr(rs.frame_metadata_value, key, None)
        try:
            if mv is not None and frame.supports_frame_metadata(mv):
                md.append(f"{key}={frame.get_frame_metadata(mv)}")
        except Exception:
            pass
    for i in range(0, len(md), 3):
        out.append("  ".join(md[i:i + 3]))
    return out


def x11grab(path, w, h):
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab",
                           "-video_size", f"{w}x{h}", "-i", DISPLAY, "-frames:v", "1", path],
                          capture_output=True, text=True).returncode


# ----------------------------- offline self-test (no camera / no display) -----------------------------
def run_self_test():
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # argparse: good args parse; aliases + defaults resolve; bad args exit 2
    a = resolve_args(["--interactive", "--depth", "--duration", "5", "--serial", "1234"])
    check("argparse: --depth->stream=depth", a.stream == "depth")
    check("argparse: --interactive default record off", a.record is False)
    check("argparse: --serial retained", a.serial == "1234")
    check("argparse: validate default record on", resolve_args([]).record is True)
    try:
        build_parser().parse_args(["--stream", "bogus"]); bad_ok = False
    except SystemExit as e:
        bad_ok = (e.code == 2)
    check("argparse: invalid choice exits 2", bad_ok)

    # profile clamping at both bounds + family sizes
    check("clamp low", clamp_idx(-5, "color") == 0)
    check("clamp high", clamp_idx(999, "color") == len(COLOR_PROFILES) - 1)
    check("profile_for valid", profile_for("depth", 0) == DEPTH_PROFILES[0])

    # validation math on synthetic grabs
    blank = np.zeros((200, 300, 3), np.uint8)
    a_img = blank.copy(); a_img[20:40, 20:120] = (0, 255, 0)   # green overlay block
    b_img = a_img.copy(); b_img[100:150, 100:200] = (50, 60, 200)  # changed region -> live
    ck = grab_checks(a_img, b_img)
    check("grab_checks nonblank", ck["nonblank"])
    check("grab_checks green detected", ck["green_overlay_px"] > 50)
    check("grab_checks live change", ck["live_change_meandiff"] > 1.0)
    check("grab_checks blank-frame safe", grab_checks(None, None)["nonblank"] is False)

    # HUD composition is headless-safe and paints the overlay colors
    hud = compose_hud(np.zeros((200, 400, 3), np.uint8), "GB10 LIVE f=0001 848x480@30 color 30fps",
                      ["fnum=1 ts=1.0ms [hw]", "actual_exposure=100 gain_level=16"], status="ok",
                      help_lines=["help"])
    check("compose_hud returns frame", hud is not None and hud.shape == (200, 400, 3))
    check("compose_hud paints green line", int(((hud[:, :, 1] > 170) & (hud[:, :, 2] < 110) & (hud[:, :, 0] < 110)).sum()) > 50)

    # depth-frame integrity math on synthetic raw z16 arrays (scale 0.001 m/unit; band [0.1,10]m)
    scale = 0.001
    good = np.full((48, 64), 1500, np.uint16); good[:8, :] = 0          # ~83% valid @ 1.5m
    nxt = good.copy(); nxt[20:30, 20:30] = 1600                          # a region changed -> not frozen
    dg = depth_checks(good, scale, 0.1, 10.0, prev_u16=nxt)
    check("depth: valid_ratio sane", 0.7 < dg["valid_ratio"] < 0.9)
    check("depth: in-range pixels", dg["in_range_ratio"] > 0.99)
    check("depth: not_all_zero true", dg["not_all_zero"] is True)
    check("depth: not_saturated true", dg["not_saturated"] is True)
    check("depth: differing pair -> not frozen", dg["frozen"] is False)
    check("depth: identical pair -> frozen", depth_checks(good, scale, 0.1, 10.0, prev_u16=good)["frozen"] is True)
    zero = np.zeros((48, 64), np.uint16)
    check("depth: all-zero fails not_all_zero", depth_checks(zero, scale, 0.1, 10.0)["not_all_zero"] is False)
    sat = np.full((48, 64), 0xFFFF, np.uint16)
    check("depth: all-saturated fails not_saturated", depth_checks(sat, scale, 0.1, 10.0)["not_saturated"] is False)
    far = np.full((48, 64), 50000, np.uint16)                            # 50m -> out of [0.1,10] band
    check("depth: out-of-range -> low in_range_ratio", depth_checks(far, scale, 0.1, 10.0)["in_range_ratio"] < 0.01)
    check("depth: None-frame safe", depth_checks(None, scale, 0.1, 10.0)["not_all_zero"] is False)

    # frame continuity / metadata math on synthetic frame-number / gap / domain sequences
    cc = continuity_checks([10, 11, 12, 13, 14], gaps_ms=[33.0, 34.0, 33.0],
                           domains=["Global Time"] * 5)
    check("cont: clean seq dropped==0", cc["dropped_frames"] == 0)
    check("cont: clean seq monotonic", cc["monotonic"] is True)
    check("cont: clean seq gap_sane", cc["gap_sane"] is True)
    check("cont: clean seq domain consistent", cc["ts_domain_consistent"] is True)
    cc2 = continuity_checks([10, 11, 14, 15], gaps_ms=[33.0, 99.0, 33.0])  # gap 11->14 == 2 dropped
    check("cont: gap seq dropped==2", cc2["dropped_frames"] == 2)
    check("cont: non-increasing -> not monotonic", continuity_checks([10, 11, 11, 12])["monotonic"] is False)
    cc3 = continuity_checks([1, 2, 3], gaps_ms=[1000.0], gap_max_ms=250.0)  # huge gap -> not sane
    check("cont: huge gap -> gap_sane False", cc3["gap_sane"] is False)
    cc4 = continuity_checks([1, 2], domains=["Global Time", "System Time"])
    check("cont: mixed domains -> inconsistent", cc4["ts_domain_consistent"] is False)
    check("cont: empty seq safe", continuity_checks([])["dropped_frames"] == 0)

    print(f"\nself-test: {'PASS' if not fails else 'FAIL ' + str(fails)}  ({len(fails)} failures)")
    return 0 if not fails else 1


# ----------------------------- live run (validate + interactive) -----------------------------
def run(args):
    import pyrealsense2 as rs
    import cv2

    hil = H.HIL("display-validate", display=True)
    hil.report.update({"mode": "interactive" if args.interactive else "validate",
                       "stream": args.stream, "main_display": DISPLAY, "cv2": cv2.__version__})
    hil.preflight()

    cam = Cam(rs, family=args.stream, idx=args.profile, serial=args.serial)
    cam.start()
    hil.report.update({"requested_device_serial": args.serial or None,
                       "device_serial": cam.device_serial})
    colorizer = rs.colorizer()

    shared = {"img": None, "stop": False, "controller_dead": False}
    grabs = {}
    glock = threading.Lock()

    def monitor():
        t0 = time.time(); did = set()
        while not shared["stop"]:
            try:
                hil.check_tripwire()
            except H.Tripwire:
                shared["stop"] = True; shared["controller_dead"] = True; break
            if not args.interactive:
                el = time.time() - t0
                for tag, when in (("a", 2.0), ("b", 4.0)):
                    if tag not in did and el >= when:
                        did.add(tag)
                        with glock:
                            snap = None if shared["img"] is None else shared["img"].copy()
                        p = os.path.join(hil.dir, f"screen-{tag}.png")
                        if x11grab(p, 1100, 760) == 0 and os.path.exists(p):
                            grabs[tag] = (p, snap)
            time.sleep(1.0)

    mon = threading.Thread(target=monitor, daemon=True); mon.start()

    enc = None; frame_q = queue.Queue(maxsize=4); wr = None
    if args.record:
        w, h, fps = cam.whf
        enc = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                                "-c:v", "h264_nvenc", "-preset", "p4", os.path.join(hil.dir, "rendered.mp4")],
                               stdin=subprocess.PIPE)

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
    cv2.resizeWindow(WIN, cam.whf[0], cam.whf[1])
    HELP = ["[1-9] profile  c/d color/depth  e emitter  a auto-exp  [ ] exp  - = gain",
            "l/L laser  p preset  f freeze  r re-acquire  s snapshot  h help  q quit"]
    show_help = args.interactive; frozen = False
    last = None; gaps = []; delivered = 0; i = 0; ok = True; status = ""; cur_img = None; stream_err = 0
    frame_nums = []; domains = []                      # continuity / metadata accumulators
    depth_scale = None; prev_depth_u16 = None; depth_pair = None   # depth-integrity (raw z16)
    if cam.family == "depth" and cam.depth_sensor is not None:
        try:
            depth_scale = float(cam.depth_sensor.get_depth_scale())
        except Exception:
            depth_scale = None
    t_start = time.time()
    try:
        while True:
            if not frozen:
                frame = cam.read()
                if frame is None:
                    stream_err += 1
                    if not args.interactive:
                        status = "stream error — failing fast"; ok = False; break
                    status = f"stream error #{stream_err} — press 'r' to re-acquire, 'q' to quit"
                else:
                    if cam.family == "color":
                        img = np.asanyarray(frame.get_data())
                    else:
                        depth_u16 = np.asanyarray(frame.get_data())   # RAW z16 (integrity), not colorized
                        img = np.asanyarray(colorizer.colorize(frame).get_data())
                        if i > WARMUP and prev_depth_u16 is not None:
                            depth_pair = (prev_depth_u16, depth_u16)  # keep latest consecutive raw pair
                        prev_depth_u16 = depth_u16
                    now = time.time()
                    if last is not None and i > WARMUP:
                        gaps.append((now - last) * 1000.0)
                    last = now; delivered += 1; i += 1
                    cur_img = img
                    meta = md_lines(rs, frame)
                    try:
                        frame_nums.append(int(frame.get_frame_number()))
                    except Exception:
                        pass
                    try:
                        domains.append(str(frame.get_frame_timestamp_domain()).split(".")[-1])
                    except Exception:
                        pass
            if cur_img is None:
                if cv2.waitKey(30) & 0xFF == ord('q'):
                    break
                continue
            w, h, fps = cam.whf
            dt = gaps[-1] if gaps else 0.0
            wall_fps = 1000.0 / dt if dt > 0 else 0.0
            fps_line = f"GB10 LIVE f={i:04d} {w}x{h}@{fps} {cam.family}  {wall_fps:4.1f}fps dt={dt:4.1f}ms"
            disp = compose_hud(cur_img, fps_line, meta if not frozen else [], status,
                               HELP if show_help else None)
            cv2.imshow(WIN, disp)
            with glock:
                shared["img"] = disp
            if enc and not frozen and cur_img.shape[1] == cam.whf[0] and cur_img.shape[0] == cam.whf[1]:
                try:
                    frame_q.put_nowait(np.ascontiguousarray(disp).tobytes())
                except queue.Full:
                    pass

            key = cv2.waitKey(1) & 0xFF
            if shared["stop"]:
                ok = not shared["controller_dead"]; break
            if args.interactive and key != 255:
                if key == ord('q'):
                    break
                elif key == ord('f'):
                    frozen = not frozen; status = "FROZEN" if frozen else "live"
                elif key == ord('h'):
                    show_help = not show_help
                elif key == ord('s'):
                    cv2.imwrite(os.path.join(hil.dir, f"snapshot-{i:04d}.png"), disp); status = "snapshot saved"
                else:
                    status = _camera_key(key, cam, cv2) or status
            if args.duration and (time.time() - t_start) >= args.duration:
                break
            if not args.interactive and i >= args.frames:
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

    dchecks = (depth_checks(depth_pair[1], depth_scale, 0.1, 10.0, prev_u16=depth_pair[0])
               if (cam.family == "depth" and depth_pair is not None and depth_scale) else None)
    cchecks = continuity_checks(frame_nums, gaps_ms=gaps, domains=domains)
    return _finish(hil, args, cam, gaps, delivered, grabs, ok, dchecks, cchecks)


def _camera_key(key, cam, cv2):
    """Handle a camera/profile/feature key. Returns a status string, or None if the key is unbound."""
    if key in [ord(str(n)) for n in range(1, 10)]:
        cam.switch(idx=key - ord('1')); cv2.resizeWindow(WIN, cam.whf[0], cam.whf[1])
        return f"profile -> {cam.whf}"
    if key in (ord('c'), ord('d')):
        cam.switch(family="color" if key == ord('c') else "depth")
        cv2.resizeWindow(WIN, cam.whf[0], cam.whf[1])
        return f"family -> {cam.family} {cam.whf}"
    if key == ord('r'):
        try:
            cam.reacquire(); return "re-acquired"
        except Exception as e:
            return f"re-acquire failed: {e}"
    table = {ord('e'): ("emitter_enabled", dict(toggle=True)), ord('a'): ("enable_auto_exposure", dict(toggle=True)),
             ord(']'): ("exposure", dict(delta=+1)), ord('-'): ("gain", dict(delta=-1)),
             ord('='): ("gain", dict(delta=+1)), ord('+'): ("gain", dict(delta=+1)),
             ord('l'): ("laser_power", dict(delta=-1)), ord('L'): ("laser_power", dict(delta=+1)),
             ord('p'): ("visual_preset", dict(delta=+1))}
    if key == ord('['):
        cam.bump("enable_auto_exposure", delta=0); return f"exposure -> {cam.bump('exposure', delta=-1)}"
    if key in table:
        opt, kw = table[key]; return f"{opt} -> {cam.bump(opt, **kw)}"
    return None


def _finish(hil, args, cam, gaps, delivered, grabs, ok, dchecks=None, cchecks=None):
    import cv2
    steady_fps = round(1000.0 / float(np.median(gaps)), 1) if gaps else 0.0
    med = float(np.median(gaps)) if gaps else 0.0
    stutters = int(sum(1 for g in gaps if g > max(66.0, 1.8 * med))) if gaps else 0
    hil.report.update({"frames_rendered": delivered, "delivered_fps": steady_fps,
                       "interframe_gap_ms": H.stats(gaps), "stutter_count": stutters,
                       "final_profile": list(cam.whf) if cam.whf else None})
    if cchecks is not None:
        hil.report["continuity"] = cchecks
    if dchecks is not None:
        hil.report["depth_integrity"] = dchecks
    if args.interactive:
        hil.report["result"] = "PASS" if (ok and not hil.report["controller_death"]) else "FAIL"
        hil.finish(ok=ok)
        return 0

    keys = sorted(grabs)
    imgs = {k: cv2.imread(grabs[k][0]) for k in keys}
    v = grab_checks(imgs.get(keys[0]) if keys else None,
                    imgs.get(keys[1]) if len(keys) > 1 else None)
    mp4 = os.path.join(hil.dir, "rendered.mp4")
    v["recorded_ok"] = bool(os.path.exists(mp4) and os.path.getsize(mp4) > 1000
                            and "h264" in subprocess.run([FFMPEG, "-hide_banner", "-i", mp4],
                                                         capture_output=True, text=True).stderr.lower())
    hil.report["validation"] = v
    checks = {
        "rendered_on_screen": v["nonblank"],
        "our_content_on_screen": v["green_overlay_px"] > 50,
        "live_video_not_frozen": v["live_change_meandiff"] > 1.0,
        "framerate_ok": steady_fps >= cam.whf[2] - 3,
        "smooth_no_stutter": stutters <= 1,
        "clip_recorded": v["recorded_ok"],
        "controller_green": not hil.report["controller_death"],
    }
    # Frame continuity / metadata gates (apply to every run; valid_ratio band stays report-only).
    if cchecks is not None:
        checks["frames_monotonic"] = cchecks["monotonic"]
        checks["no_dropped_frames"] = cchecks["dropped_frames"] == 0
        checks["arrival_gap_sane"] = cchecks["gap_sane"]
        checks["ts_domain_consistent"] = cchecks["ts_domain_consistent"]
    # Depth-frame integrity gates (depth runs only; valid_ratio_in_band reported, not gated).
    if dchecks is not None:
        checks["depth_not_all_zero"] = dchecks["not_all_zero"]
        checks["depth_not_saturated"] = dchecks["not_saturated"]
        checks["depth_not_frozen"] = (dchecks["frozen"] is False)
    hil.report["checks"] = checks
    passed = ok and all(checks.values())
    hil.report["result"] = "PASS" if passed else "FAIL"
    hil.finish(ok=passed)
    return 0 if passed else 1


def main(argv=None):
    args = resolve_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return run_self_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
