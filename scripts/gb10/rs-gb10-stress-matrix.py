#!/usr/bin/env python3
"""
rs-gb10-stress-matrix.py — RealSense D435 interface stress test over a matrix of
resolutions and frame rates (emphasis on 60 fps + concurrent depth/color/IR load).

For each matrix entry it configures the requested streams, runs for a fixed duration,
and measures per-stream delivered fps and frame-number gaps (drops). Designed to push
USB-3 bandwidth: concurrent Z16 depth + RGB color + Y8 infrared at 60-90 fps.

Output: a JSON array of per-entry results on stdout (also written to --json PATH).
Optional on-screen rendering via OpenCV (default on if DISPLAY set; --headless to disable).

Run via the GB10 SDK env so the right pyrealsense2 is used:
    realsense-gb10-env python3 rs-gb10-stress-matrix.py [--duration S] [--json PATH] [--headless]

Exit: 0 if every entry met its pass criteria, 1 otherwise, 2 on setup error.
"""
import argparse, json, os, sys, time, subprocess
from datetime import datetime

# Kernel signatures that mean "stop NOW or the xHCI controller may die".
# Observed escalation on GB10: control -110 timeouts -> "Not enough bandwidth" ->
# "Host halt failed -110" -> "HC died". We abort at the FIRST of these.
DANGER_RE = "Host halt|HC died|not responding|Not enough bandwidth|controller not|" \
            "-110 \\(exp|failed to set power|cannot enable|over-current"

def kernel_danger_since(since_iso):
    """Return matching kernel danger lines since the given timestamp, else ''."""
    try:
        out = subprocess.run(["journalctl", "-k", "--since", since_iso, "--no-pager"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return ""
    hits = [ln for ln in out.splitlines()
            if any(p and __import__("re").search(p, ln) for p in DANGER_RE.split("|"))
            and "981ae2" not in ln and "USBDEVFS_CLEAR_HALT for active endpoint" not in ln]
    return "\n".join(hits[-12:])

try:
    import pyrealsense2 as rs
except Exception as e:  # pragma: no cover
    print(json.dumps({"error": f"pyrealsense2 import failed: {e}"}))
    sys.exit(2)

import numpy as np

# ---- matrix: each entry = list of streams (kind, w, h, fps, fmt) ----
D, C, I = "depth", "color", "infrared"
def Z(w, h, fps):  return (D, w, h, fps, rs.format.z16)
def RGB(w, h, fps): return (C, w, h, fps, rs.format.bgr8)
def IR(w, h, fps):  return (I, w, h, fps, rs.format.y8)

# Ordered LIGHTEST -> HEAVIEST so the kernel tripwire finds the safe ceiling before the
# known-dangerous heavy configs run. The 3x848x480@60 entry crashed the GB10 xHCI on
# 2026-06-02; it is intentionally placed near the end and guarded by the tripwire.
MATRIX = [
    ("60fps_424x240_depth",       [Z(424,240,60)]),
    ("60fps_640x480_depth",       [Z(640,480,60)]),
    ("60fps_640x480_D+C",         [Z(640,480,60),  RGB(640,480,60)]),
    ("60fps_848x480_D+IR",        [Z(848,480,60),  IR(848,480,60)]),
    ("60fps_848x480_D+C",         [Z(848,480,60),  RGB(848,480,60)]),
    ("60fps_424x240_D+C+IR",      [Z(424,240,60),  RGB(424,240,60), IR(424,240,60)]),
    ("60fps_640x360_D+C+IR",      [Z(640,360,60),  RGB(640,360,60), IR(640,360,60)]),
    ("60fps_960x540color_+848D",  [Z(848,480,60),  RGB(960,540,60)]),
    ("90fps_848x480_D+IR",        [Z(848,480,90),  IR(848,480,90)]),
    ("60fps_640x480_D+C+IR",      [Z(640,480,60),  RGB(640,480,60), IR(640,480,60)]),
    ("HEAVY_60fps_848x480_D+C+IR",[Z(848,480,60),  RGB(848,480,60), IR(848,480,60)]),  # crashed GB10 xHCI 2026-06-02
    ("300fps_848x100_depth",      [Z(848,100,300)]),
]

def build_config(streams):
    cfg = rs.config()
    ir_idx = 0
    for (kind, w, h, fps, fmt) in streams:
        if kind == D:
            cfg.enable_stream(rs.stream.depth, w, h, fmt, fps)
        elif kind == C:
            cfg.enable_stream(rs.stream.color, w, h, fmt, fps)
        elif kind == I:
            ir_idx += 1
            cfg.enable_stream(rs.stream.infrared, ir_idx, w, h, fmt, fps)
    return cfg

def run_entry(name, streams, duration, render):
    pipe = rs.pipeline()
    cfg = build_config(streams)
    requested_fps = max(s[3] for s in streams)
    counts, first_n, last_n, gaps = {}, {}, {}, {}
    t0 = None
    started = False
    err = ""
    aborted_on_fault = ""
    entry_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last_trip = 0.0
    try:
        prof = pipe.start(cfg)
        started = True
        # warm-up: discard first frames + let auto-exposure settle
        for _ in range(5):
            try: pipe.wait_for_frames(2000)
            except Exception: pass
        t0 = time.time()
        deadline = t0 + duration
        timeouts = 0
        last_draw = 0.0
        DRAW_INTERVAL = 1.0 / 30.0  # throttle on-screen render to ~30 Hz so it does NOT
                                    # distort the interface throughput measurement at 60-300 fps
        while time.time() < deadline:
            try:
                fs = pipe.wait_for_frames(2000)
            except Exception:
                timeouts += 1
                continue
            for fr in fs:
                key = fr.get_profile().stream_name()
                n = fr.get_frame_number()
                counts[key] = counts.get(key, 0) + 1
                if key not in first_n:
                    first_n[key] = n
                else:
                    d = n - last_n[key]
                    if d > 1:
                        gaps[key] = gaps.get(key, 0) + (d - 1)
                last_n[key] = n
            now = time.time()
            # SAFETY TRIPWIRE: poll the kernel ~every 0.5s; bail out immediately on the
            # first controller-danger signature, before it escalates to "HC died".
            if (now - last_trip) >= 0.5:
                last_trip = now
                d = kernel_danger_since(entry_since)
                if d:
                    aborted_on_fault = d
                    break
            if render and (now - last_draw) >= DRAW_INTERVAL:
                last_draw = now
                try:
                    import cv2
                    imgs = []
                    for fr in fs:
                        if fr.is_depth_frame():
                            a = np.asanyarray(fr.get_data())
                            a = cv2.convertScaleAbs(a, alpha=0.03)
                            a = cv2.applyColorMap(a, cv2.COLORMAP_JET)
                        else:
                            a = np.asanyarray(fr.get_data())
                            if a.ndim == 2:
                                a = cv2.cvtColor(a, cv2.COLOR_GRAY2BGR)
                        a = cv2.resize(a, (424, 240))
                        imgs.append(a)
                    if imgs:
                        cv2.imshow(f"rs-stress", np.hstack(imgs))
                        cv2.setWindowTitle("rs-stress", f"{name}")
                        cv2.waitKey(1)
                except Exception:
                    pass
        elapsed = time.time() - t0
    except Exception as e:
        elapsed = (time.time() - t0) if t0 else 0.0
        err = str(e)
        timeouts = -1
    finally:
        if started:
            try: pipe.stop()
            except Exception: pass
        if render:
            try:
                import cv2; cv2.destroyAllWindows(); cv2.waitKey(1)
            except Exception: pass

    per_stream = []
    ok = (err == "")
    for key, c in sorted(counts.items()):
        fps = c / elapsed if elapsed > 0 else 0.0
        ratio = fps / requested_fps if requested_fps else 0.0
        g = gaps.get(key, 0)
        # pass criteria: started, delivered >=70% of requested fps, drops < 5% of frames
        stream_ok = (c > 0 and ratio >= 0.70 and g <= max(2, 0.05 * c))
        ok = ok and stream_ok
        per_stream.append({"stream": key, "frames": c, "fps": round(fps, 2),
                           "requested_fps": requested_fps, "fps_ratio": round(ratio, 2),
                           "gaps": g, "ok": stream_ok})
    if aborted_on_fault:
        ok = False
    return {"entry": name, "requested_fps": requested_fps, "duration_s": round(elapsed, 1),
            "streams": per_stream, "error": err, "started": started,
            "aborted_on_fault": aborted_on_fault, "ok": ok and started and not aborted_on_fault}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=12.0, help="seconds per matrix entry")
    ap.add_argument("--json", default="")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--serial", default="")
    args = ap.parse_args()
    render = (not args.headless) and bool(os.environ.get("DISPLAY"))

    ctx = rs.context()
    devs = ctx.query_devices()
    if len(devs) == 0:
        print(json.dumps([{"error": "no RealSense device"}]))
        sys.exit(2)
    dev = devs[0]
    info = {k: _safe(dev, k) for k in ("name", "serial_number", "firmware_version", "usb_type_descriptor", "physical_port")}

    results = []
    aborted = False
    for (name, streams) in MATRIX:
        r = run_entry(name, streams, args.duration, render)
        results.append(r)
        s = " ".join(f"{x['stream']}={x['fps']}/{x['requested_fps']}fps(g{x['gaps']})" for x in r["streams"])
        tag = "PASS" if r["ok"] else "FAIL"
        extra = ("ERR:" + r["error"]) if r["error"] else ""
        if r.get("aborted_on_fault"):
            tag = "ABORT"; extra = "TRIPWIRE: " + r["aborted_on_fault"].splitlines()[-1][-90:]
        print(f"[{tag}] {name:30s} {s} {extra}", file=sys.stderr, flush=True)
        # Stop the whole sweep if the controller showed danger or vanished — do NOT keep
        # driving heavier configs into a dying/dead controller.
        if r.get("aborted_on_fault") or "No device connected" in (r.get("error") or "") \
           or kernel_danger_since((datetime.now()).strftime("%Y-%m-%d %H:%M:%S")):
            aborted = True
            print(f"[STOP] sweep halted after '{name}' to protect the xHCI controller.",
                  file=sys.stderr, flush=True)
            break
        time.sleep(2)  # inter-entry cooldown lets the controller settle

    out = {"device": info, "duration_s": args.duration, "render": render, "sweep_aborted": aborted,
           "results": results,
           "summary": {"entries": len(results), "passed": sum(1 for r in results if r["ok"]),
                       "failed": sum(1 for r in results if not r["ok"]),
                       "aborted": aborted}}
    js = json.dumps(out, indent=2)
    if args.json:
        with open(args.json, "w") as f: f.write(js)
    print(js)
    sys.exit(0 if out["summary"]["failed"] == 0 else 1)

def _safe(dev, k):
    try:
        cam = getattr(rs.camera_info, k)
        return dev.get_info(cam) if dev.supports(cam) else ""
    except Exception:
        return ""

if __name__ == "__main__":
    main()
