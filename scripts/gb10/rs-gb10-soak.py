#!/usr/bin/env python3
"""GB10 LONG-SOAK HIL (DANGEROUS — eyes-open; can kill the USB controller → reboot).

Sustained, phased stress on the clean USB-3 bus to earn/refute the wider multi-stream
envelope, while exercising D435 control features (laser/emitter power, depth visual presets,
auto-exposure, post-proc filters) — the control transfers that trigger the -110 storms. A
continuous journal tripwire samples for `-110` / `HC died` between every operation and ABORTS
+ records forensics on the first sign of controller death.

Phases (override with env LRS_SOAK_SECS scaling): single-soak → dual+align soak → start/stop
churn → 3-stream soak. Reports per-phase fps/gaps/-110, plus the feature-exercise outcomes.
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

W, H = 848, 480
SCALE = float(os.environ.get("LRS_SOAK_SCALE", "1.0"))
START_TS = subprocess.run("date '+%Y-%m-%d %H:%M:%S'", shell=True, capture_output=True, text=True).stdout.strip()


def journal_counts():
    """Return (minus110_count, hc_died_present) since START_TS."""
    out = subprocess.run(
        ["bash", "-c", f"journalctl -k --since '{START_TS}' --no-pager 2>/dev/null"],
        capture_output=True, text=True).stdout
    m110 = sum(1 for l in out.splitlines() if "-110" in l and ("UVC control" in l or "control" in l.lower()))
    died = any(("HC died" in l) or ("not responding to stop" in l) or ("assume dead" in l) for l in out.splitlines())
    return m110, died


class Tripwire(Exception):
    pass


def check_tripwire(report):
    m110, died = journal_counts()
    report["live_minus110"] = m110
    if died:
        report["controller_death"] = True
        raise Tripwire(f"HC died detected (since {START_TS})")


def main():
    import pyrealsense2 as rs
    report = {"start": START_TS, "scale": SCALE, "phases": {}, "features": {}, "live_minus110": 0,
              "controller_death": False}

    def soak_stream(name, streams, secs, with_align=False, feature_fn=None):
        """Stream `streams` for `secs`, optionally align, optionally exercise features."""
        ctx = rs.context()
        if len(ctx.query_devices()) == 0:
            report["phases"][name] = {"error": "no device"}; return False
        pipe = rs.pipeline(ctx); cfg = rs.config()
        for (st, idx, fmt, fps) in streams:
            cfg.enable_stream(st, idx, W, H, fmt, fps) if idx is not None else cfg.enable_stream(st, W, H, fmt, fps)
        align = rs.align(rs.stream.color) if with_align else None
        n, gaps, last = 0, 0, None
        t0 = time.time(); feat_done = False
        try:
            prof = pipe.start(cfg)
            dev = prof.get_device()
            while time.time() - t0 < secs * SCALE:
                fs = pipe.wait_for_frames(3000)
                if align:
                    fs = align.process(fs)
                d = fs.get_depth_frame()
                if d:
                    _ = np.asanyarray(d.get_data())
                    fn = d.get_frame_number()
                    if last is not None and fn != last + 1:
                        gaps += 1
                    last = fn
                n += 1
                if n % 60 == 0:
                    check_tripwire(report)              # sample tripwire ~1/sec
                if feature_fn and not feat_done and n > 90:
                    feature_fn(dev); feat_done = True    # exercise control features mid-soak
            report["phases"][name] = {"secs": round(time.time() - t0, 1), "frames": n,
                                      "fps": round(n / (time.time() - t0), 1), "gaps": gaps}
            return True
        finally:
            try:
                pipe.stop()
            except Exception:
                pass

    def exercise_features(dev):
        """Issue control transfers: laser power, emitter, presets, auto-exposure (single-stream phase)."""
        try:
            ds = dev.first_depth_sensor()
            res = {}
            if ds.supports(rs.option.emitter_enabled):
                for v in (0, 1):
                    ds.set_option(rs.option.emitter_enabled, v); time.sleep(0.2)
                res["emitter_toggle"] = "ok"
            if ds.supports(rs.option.laser_power):
                rng = ds.get_option_range(rs.option.laser_power)
                for v in (rng.min, rng.max, rng.default):
                    ds.set_option(rs.option.laser_power, v); time.sleep(0.2)
                res["laser_power_sweep"] = f"{rng.min}..{rng.max}"
            if ds.supports(rs.option.visual_preset):
                pr = ds.get_option_range(rs.option.visual_preset)
                for v in range(int(pr.min), int(pr.max) + 1):
                    ds.set_option(rs.option.visual_preset, v); time.sleep(0.15)
                ds.set_option(rs.option.visual_preset, pr.default)
                res["visual_preset_cycle"] = f"{int(pr.min)}..{int(pr.max)}"
            if ds.supports(rs.option.enable_auto_exposure):
                for v in (0, 1):
                    ds.set_option(rs.option.enable_auto_exposure, v); time.sleep(0.2)
                res["auto_exposure_toggle"] = "ok"
            report["features"] = res
        except Exception as e:
            report["features"]["error"] = str(e)

    try:
        # Phase 1: single depth soak + feature/control exercise (control path stress, single stream = safest)
        soak_stream("1_single_depth_60", [(rs.stream.depth, None, rs.format.z16, 60)], 90,
                    feature_fn=exercise_features)
        check_tripwire(report)
        # Phase 2: dual depth+color + align soak
        soak_stream("2_dual_align_60", [(rs.stream.depth, None, rs.format.z16, 60),
                                        (rs.stream.color, None, rs.format.bgr8, 60)], 90, with_align=True)
        check_tripwire(report)
        # Phase 3: start/stop churn (death-#2 pattern), 8 cycles
        churn = []
        for c in range(8):
            ok = soak_stream(f"3_churn_{c}", [(rs.stream.depth, None, rs.format.z16, 60),
                                              (rs.stream.color, None, rs.format.bgr8, 60)], 5)
            churn.append(report["phases"].pop(f"3_churn_{c}", {}))
            check_tripwire(report)
            time.sleep(0.5)
        report["phases"]["3_churn_x8"] = {"cycles": len(churn), "all_streamed": all(c.get("frames", 0) > 0 for c in churn)}
        # Phase 4: 3-stream (depth+color+IR1+IR2) soak
        soak_stream("4_quadstream_60", [(rs.stream.depth, None, rs.format.z16, 60),
                                        (rs.stream.color, None, rs.format.bgr8, 60),
                                        (rs.stream.infrared, 1, rs.format.y8, 60),
                                        (rs.stream.infrared, 2, rs.format.y8, 60)], 60)
        check_tripwire(report)
        report["result"] = "SURVIVED full soak"
    except Tripwire as t:
        report["result"] = f"ABORTED: {t}"
    except Exception as e:
        report["result"] = f"ERROR: {e}"

    m110, died = journal_counts()
    report["final_minus110"] = m110
    report["controller_green"] = not died
    print(json.dumps(report, indent=2))
    return 0 if not died else 1


if __name__ == "__main__":
    sys.exit(main())
