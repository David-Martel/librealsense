#!/usr/bin/env python3
"""Compare two rs-gb10-gpu-pipeline.py result JSONs (CUDA build vs NOCUDA build) and print the
Finding-A verdict: does per-frame CUDA color conversion offload the decode off the CPU (CPU-ms/frame
drops) or is it a wasteful copy (CPU flat/up, latency up)? Helper for `just hil-gpu-pipeline`."""
import json
import sys


def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def line(tag, r):
    if not r:
        return f"  {tag}: MISSING"
    a = (r.get("align_process_ms") or {}).get("p50")
    c = (r.get("color_access_ms") or {}).get("p50")
    g = (r.get("interframe_gap_ms") or {}).get("p95")
    return (f"  {tag:6s} fps={r.get('delivered_fps')}/{r.get('target_fps')}  "
            f"cpu_ms/frame={r.get('cpu_ms_per_frame')}  align_p50={a}  color_access_p50={c}  "
            f"gap_p95={g}  green={r.get('controller_green')}")


def main():
    cuda, cpu = load(sys.argv[1]), load(sys.argv[2])
    mode = (cuda or {}).get("mode", "full")
    print("\n========  end-to-end GPU pipeline: CUDA build vs NOCUDA (Finding A)  ========")
    print(f"  mode: {mode}" + ("   [conversion ISOLATED — no align]" if mode == "convert-only"
                                else "   [FULL pipeline — CPU delta is align-DOMINATED, NOT conversion]"))
    print(line("CUDA", cuda))
    print(line("NOCUDA", cpu))
    if cuda and cpu and cuda.get("cpu_ms_per_frame") and cpu.get("cpu_ms_per_frame"):
        dc, dn = cuda["cpu_ms_per_frame"], cpu["cpu_ms_per_frame"]
        delta = (dn - dc) / dn * 100.0 if dn else 0.0
        # ONLY attribute the CPU delta to conversion in convert-only mode; in full mode align dominates.
        what = "CUDA color conversion" if mode == "convert-only" else "full pipeline (align-dominated)"
        if dc < dn * 0.95:
            verdict = f"{what}: {delta:+.0f}% CPU/frame (CUDA lower)"
            if mode != "convert-only":
                verdict += " — driven by align, NOT conversion; run --convert-only to isolate conversion"
        elif dc > dn * 1.05:
            verdict = f"{what}: {delta:+.0f}% CPU/frame (CUDA HIGHER" + (
                ") — Finding A regression" if mode == "convert-only" else ")")
        else:
            verdict = (f"{what}: {delta:+.0f}% CPU/frame — "
                       + ("conversion is CPU-NEUTRAL vs NEON (Finding A: no regression)"
                          if mode == "convert-only" else "~equal"))
        print(f"\n  VERDICT: {verdict}")
        # delivered-fps sanity: if both hit target, the cost is hidden in slack (note it)
        if cuda.get("delivered_fps") and cpu.get("delivered_fps"):
            if cuda["delivered_fps"] >= cuda.get("target_fps", 1e9) - 1 and \
               cpu["delivered_fps"] >= cpu.get("target_fps", 1e9) - 1:
                print("  (both builds sustained target FPS — conversion cost shows in CPU/frame, not drops)")
    print("==============================================================================")


if __name__ == "__main__":
    main()
