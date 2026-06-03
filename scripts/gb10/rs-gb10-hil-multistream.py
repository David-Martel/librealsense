#!/usr/bin/env python3
"""GB10 CONCURRENT MULTI-STREAM stress (DANGEROUS — can kill the USB controller).

Brings up dual depth+color 848x480@60 in one context and runs rs.align(depth->color)
per frame (an advanced multi-stream CUDA feature). This is the configuration class that
killed the controller in incident #3. Run only with eyes-open acceptance; a wrapper arms
the journal tripwire and captures forensics. Aborts immediately on the first SDK error
(the controller-death signature) to minimize further Stop-Endpoint traffic.
"""
import json
import sys
import time

import numpy as np

W, H, FPS = 848, 480, 60
WARMUP = 15
FRAMES = 300


def main():
    import pyrealsense2 as rs

    report = {"config": "dual depth+color 848x480@60 + align(depth->color)", "ops": {}, "frames": 0}
    ctx = rs.context()
    if len(ctx.query_devices()) == 0:
        print("NO_DEVICE", file=sys.stderr)
        return 2
    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, FPS)
    cfg.enable_stream(rs.stream.color, W, H, rs.format.bgr8, FPS)   # SECOND stream -> concurrent
    align = rs.align(rs.stream.color)

    align_lat = []
    gaps = 0
    last = None
    try:
        print(f"[{time.strftime('%H:%M:%S')}] starting dual stream...", file=sys.stderr, flush=True)
        pipe.start(cfg)
        for _ in range(WARMUP):
            pipe.wait_for_frames(3000)
        print(f"[{time.strftime('%H:%M:%S')}] warmup done, streaming {FRAMES} aligned frames", file=sys.stderr, flush=True)
        t0 = time.time()
        for i in range(FRAMES):
            fs = pipe.wait_for_frames(3000)
            t = time.time()
            aligned = align.process(fs)                 # multi-stream align (depth->color)
            d = aligned.get_depth_frame()
            c = aligned.get_color_frame()
            if not d or not c:
                report.setdefault("findings", []).append({"frame": i, "issue": "missing aligned frame"})
                continue
            _ = np.asanyarray(d.get_data()); _ = np.asanyarray(c.get_data())
            align_lat.append((time.time() - t) * 1000)
            fn = d.get_frame_number()
            if last is not None and fn != last + 1:
                gaps += 1
            last = fn
            report["frames"] = i + 1
        report["effective_fps"] = round(report["frames"] / (time.time() - t0), 2)
        report["stream_gaps"] = gaps
    except Exception as e:
        report["fatal"] = str(e)
        print(f"[{time.strftime('%H:%M:%S')}] EXCEPTION (likely controller death): {e}", file=sys.stderr, flush=True)
    finally:
        try:
            for _ in range(15):
                try:
                    pipe.poll_for_frames()
                except Exception:
                    break
            pipe.stop()
        except Exception as e:
            print(f"stop error: {e}", file=sys.stderr)
    if align_lat:
        report["ops"]["align"] = {"n": len(align_lat), "mean_ms": round(sum(align_lat) / len(align_lat), 2),
                                  "max_ms": round(max(align_lat), 2)}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
