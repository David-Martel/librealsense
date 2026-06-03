#!/usr/bin/env python3
"""P7 in-situ churn test: reproduce the controller-death #2 pattern — destroy and recreate
the whole context/pipeline between streams — and show P7 catches it. Run under
RS2_GB10_REFUSE_REACQUIRE=1: cycle 0 should stream; cycle 1+ re-acquires the device after a
full release, so P7 must REFUSE (throw) at device construction, halting the lethal loop
BEFORE repeated teardown storms. This converts the death-#2 pattern into a safe early refusal.
"""
import gc
import sys
import time

import numpy as np

W, H, FPS = 848, 480, 60
CYCLES = 4


def one_cycle(rs, i):
    ctx = rs.context()
    if len(ctx.query_devices()) == 0:
        return "no-device"
    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, W, H, rs.format.z16, FPS)
    cfg.enable_stream(rs.stream.color, W, H, rs.format.bgr8, FPS)
    pipe.start(cfg)
    for _ in range(20):
        fs = pipe.wait_for_frames(3000)
        _ = np.asanyarray(fs.get_depth_frame().get_data())
    pipe.stop()
    del pipe, cfg, ctx
    gc.collect()
    return "streamed"


def main():
    import pyrealsense2 as rs
    results = []
    for i in range(CYCLES):
        try:
            r = one_cycle(rs, i)
            results.append({"cycle": i, "result": r})
            print(f"[{time.strftime('%H:%M:%S')}] cycle {i}: {r}", file=sys.stderr, flush=True)
        except Exception as e:
            msg = str(e)
            refused = "re-acquired" in msg
            results.append({"cycle": i, "result": "REFUSED-by-P7" if refused else "error", "msg": msg})
            print(f"[{time.strftime('%H:%M:%S')}] cycle {i}: {'P7-REFUSED' if refused else 'ERROR'}: {msg}",
                  file=sys.stderr, flush=True)
            gc.collect()
        time.sleep(1.0)   # let the kernel settle between cycles
    import json
    print(json.dumps({"cycles": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
