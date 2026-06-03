#!/usr/bin/env python3
"""Standardized GB10 HIL test suite — one idempotent, stable runner for the hardware tests.

Subcommands (each: pre-flight gate -> run -> continuous controller tripwire -> JSON+artifacts):
  soak              phased single->dual->churn->quad stress + control-feature exercise
  capture-playback  record to .bag, then replay (real-time + max-speed loops) and verify
Both honor --display for NON-HEADLESS on-screen rendering (watch it live) and save an x11grab
proof + result.json under ~/realsense-gb10-validation/hil-runs/<ts>-<test>/.

Usage:
  python rs-gb10-hil-suite.py soak [--display] [--scale 1.0]
  python rs-gb10-hil-suite.py capture-playback [--display] [--secs 20] [--loops 5]

Stable/idempotent: warmup before measuring, unique artifact dir per run, stream always stopped
in finally, pre-flight refuses to start on a dead controller / missing camera / USB-2 link.
SAFETY: multi-stream phases are eyes-open (can kill the controller -> reboot); the tripwire
aborts immediately on the first HC-died.
"""
import argparse
import os
import sys
import time

import numpy as np

from hil_common import HIL, Tripwire, stats

W, H = 848, 480


# ----------------------------------------------------------------------------
def _enable(cfg, rs, spec):
    st, idx, fmt, fps = spec
    if idx is None:
        cfg.enable_stream(st, W, H, fmt, fps)
    else:
        cfg.enable_stream(st, idx, W, H, fmt, fps)


def _exercise_controls(rs, dev, report):
    """Issue control transfers (the -110 trigger class): emitter, laser, presets, AE."""
    res = {}
    try:
        ds = dev.first_depth_sensor()
        if ds.supports(rs.option.emitter_enabled):
            for v in (0, 1):
                ds.set_option(rs.option.emitter_enabled, v); time.sleep(0.15)
            res["emitter_toggle"] = "ok"
        if ds.supports(rs.option.laser_power):
            r = ds.get_option_range(rs.option.laser_power)
            for v in (r.min, r.max, r.default):
                ds.set_option(rs.option.laser_power, v); time.sleep(0.15)
            res["laser_sweep"] = f"{r.min}..{r.max}"
        if ds.supports(rs.option.visual_preset):
            r = ds.get_option_range(rs.option.visual_preset)
            for v in range(int(r.min), int(r.max) + 1):
                ds.set_option(rs.option.visual_preset, v); time.sleep(0.12)
            ds.set_option(rs.option.visual_preset, r.default)
            res["preset_cycle"] = f"{int(r.min)}..{int(r.max)}"
        if ds.supports(rs.option.enable_auto_exposure):
            for v in (0, 1):
                ds.set_option(rs.option.enable_auto_exposure, v); time.sleep(0.15)
            res["auto_exposure_toggle"] = "ok"
    except Exception as e:
        res["error"] = str(e)
    report["features"] = res


def cmd_soak(args):
    import pyrealsense2 as rs
    import cv2  # noqa: F401 (only needed for --display, imported lazily by HIL.show)
    hil = HIL("soak", display=args.display)
    hil.preflight()
    hil.report["scale"] = args.scale
    hil.report["phases"] = {}

    def phase(name, streams, secs, align=False, controls=False):
        ctx = rs.context(); pipe = rs.pipeline(ctx); cfg = rs.config()
        for s in streams:
            _enable(cfg, rs, s)
        al = rs.align(rs.stream.color) if align else None
        col = rs.colorizer()
        n, gaps, last = 0, 0, None
        t0 = time.time(); did_controls = False
        try:
            prof = pipe.start(cfg); dev = prof.get_device()
            while time.time() - t0 < secs * args.scale:
                fs = pipe.wait_for_frames(3000)
                if al:
                    fs = al.process(fs)
                d = fs.get_depth_frame()
                if d:
                    fn = d.get_frame_number()
                    if last is not None and fn != last + 1:
                        gaps += 1
                    last = fn
                    if hil.display and n % 2 == 0:
                        hil.show(np.asanyarray(col.colorize(d).get_data()), f"SOAK {name} f{n}")
                n += 1
                if n % 60 == 0:
                    hil.check_tripwire()
                if controls and not did_controls and n > 90:
                    _exercise_controls(rs, dev, hil.report); did_controls = True
            hil.report["phases"][name] = {"secs": round(time.time() - t0, 1), "frames": n,
                                          "fps": round(n / (time.time() - t0), 1), "gaps": gaps}
        finally:
            try:
                pipe.stop()
            except Exception:
                pass

    try:
        phase("1_single_depth", [(rs.stream.depth, None, rs.format.z16, 60)], 90, controls=True)
        hil.check_tripwire()
        phase("2_dual_align", [(rs.stream.depth, None, rs.format.z16, 60),
                               (rs.stream.color, None, rs.format.bgr8, 60)], 90, align=True)
        hil.check_tripwire()
        ok = True
        for c in range(8):
            phase(f"_churn{c}", [(rs.stream.depth, None, rs.format.z16, 60),
                                 (rs.stream.color, None, rs.format.bgr8, 60)], 5)
            ok = ok and hil.report["phases"].pop(f"_churn{c}", {}).get("frames", 0) > 0
            hil.check_tripwire(); time.sleep(0.4)
        hil.report["phases"]["3_churn_x8"] = {"all_streamed": ok}
        phase("4_quadstream", [(rs.stream.depth, None, rs.format.z16, 60),
                               (rs.stream.color, None, rs.format.bgr8, 60),
                               (rs.stream.infrared, 1, rs.format.y8, 60),
                               (rs.stream.infrared, 2, rs.format.y8, 60)], 60)
        hil.check_tripwire()
        if hil.display:
            hil.report["screen_proof"] = hil.grab_proof("soak-proof")
        hil.report["result"] = "SURVIVED full soak"
        rc = hil.finish(ok=True)
    except Tripwire as t:
        hil.report["result"] = f"ABORTED: {t}"; rc = hil.finish(ok=False)
    finally:
        hil.close_display()
    return rc


def cmd_capture_playback(args):
    """Stress the record/playback path: record a .bag, then replay it real-time and max-speed."""
    import pyrealsense2 as rs
    import cv2  # noqa: F401
    hil = HIL("capture-playback", display=args.display)
    hil.preflight()
    bag = os.path.join(hil.dir, "capture.db3")   # this build records rosbag2 (SQLite), not legacy .bag

    # --- record phase ---
    rec = {"path": bag, "secs": args.secs}
    ctx = rs.context(); pipe = rs.pipeline(ctx); cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, 30)
    cfg.enable_stream(rs.stream.color, W, H, rs.format.bgr8, 30)
    cfg.enable_record_to_file(bag)
    n = 0; t0 = time.time()
    try:
        pipe.start(cfg)
        while time.time() - t0 < args.secs:
            fs = pipe.wait_for_frames(3000)
            cf = fs.get_color_frame()
            if cf and hil.display and n % 2 == 0:
                hil.show(np.asanyarray(cf.get_data()), f"RECORD f{n}")
            n += 1
            if n % 30 == 0:
                hil.check_tripwire()
        rec["frames_recorded"] = n
    finally:
        try:
            pipe.stop()
        except Exception:
            pass
    rec["bag_bytes"] = os.path.getsize(bag) if os.path.exists(bag) else 0
    hil.report["record"] = rec

    # --- playback phase (real-time + max-speed loops); verify integrity ---
    def play(realtime, loop_idx):
        ctx = rs.context(); pipe = rs.pipeline(ctx); cfg = rs.config()
        cfg.enable_device_from_file(bag, repeat_playback=False)
        frames = 0; gaps = 0; last = None; lat = []
        try:
            prof = pipe.start(cfg)
            pb = prof.get_device().as_playback()
            pb.set_real_time(realtime)
            while True:
                try:
                    fs = pipe.wait_for_frames(2000)
                except RuntimeError:
                    break  # EOF
                d = fs.get_depth_frame()
                if not d:
                    break
                fn = d.get_frame_number()
                if last is not None and fn != last + 1:
                    gaps += 1
                last = fn
                if hil.display and realtime and frames % 2 == 0:
                    cf = fs.get_color_frame()
                    if cf:
                        hil.show(np.asanyarray(cf.get_data()), f"PLAYBACK rt={realtime} f{frames}")
                frames += 1
                if frames > rec.get("frames_recorded", 0) + 50:
                    break  # safety
        finally:
            try:
                pipe.stop()
            except Exception:
                pass
        return {"frames": frames, "gaps": gaps}

    playbacks = {"realtime": play(True, 0)}
    fast = [play(False, i) for i in range(args.loops)]      # max-speed loop stress
    counts = [p["frames"] for p in fast]
    playbacks["maxspeed_loops"] = {"loops": args.loops, "frame_counts": counts,
                                   "consistent": len(set(counts)) == 1,
                                   "matches_recorded": all(c >= rec.get("frames_recorded", 0) * 0.9 for c in counts)}
    hil.report["playback"] = playbacks
    hil.report["verdict"] = {
        "recorded": rec.get("frames_recorded", 0) > 0,
        "replay_frame_count_stable": playbacks["maxspeed_loops"]["consistent"],
        "replay_matches_record": playbacks["maxspeed_loops"]["matches_recorded"],
    }
    if hil.display:
        hil.report["screen_proof"] = hil.grab_proof("playback-proof")
    hil.close_display()
    return hil.finish(ok=all(hil.report["verdict"].values()))


def main():
    p = argparse.ArgumentParser(description="Standardized GB10 HIL suite")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("soak"); s.add_argument("--display", action="store_true"); s.add_argument("--scale", type=float, default=1.0)
    c = sub.add_parser("capture-playback"); c.add_argument("--display", action="store_true")
    c.add_argument("--secs", type=int, default=20); c.add_argument("--loops", type=int, default=5)
    args = p.parse_args()
    try:
        if args.cmd == "soak":
            return cmd_soak(args)
        if args.cmd == "capture-playback":
            return cmd_capture_playback(args)
    except Tripwire as t:
        print(f"PREFLIGHT/TRIPWIRE: {t}", file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
