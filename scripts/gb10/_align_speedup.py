#!/usr/bin/env python3
"""Print the rs.align CUDA-vs-CPU speedup from two result JSONs written by rs-gb10-align-bench.py
(via LRS_RESULT_JSON). Helper for the `just hil-align-bench` recipe — kept as a file rather than an
inline heredoc because quoted heredocs inside just shebang-recipes do not strip leading whitespace."""
import json
import sys


def load(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def main():
    cuda, cpu = load(sys.argv[1]), load(sys.argv[2])
    print("\n================  rs.align CUDA-vs-CPU  ================")
    cs = (cuda or {}).get("align_process_ms")
    ns = (cpu or {}).get("align_process_ms")
    if cs and ns:
        c, n = cs["p50"], ns["p50"]
        print(f"  align_to   : {(cuda or {}).get('align_to')}  ({(cuda or {}).get('width')}x"
              f"{(cuda or {}).get('height')}@{(cuda or {}).get('fps')})")
        print(f"  CUDA   p50 : {c:.3f} ms  (mean {cs['mean']:.3f}, p95 {cs['p95']:.3f}, "
              f"green={cuda.get('controller_green')})")
        print(f"  CPU    p50 : {n:.3f} ms  (mean {ns['mean']:.3f}, p95 {ns['p95']:.3f}, "
              f"green={cpu.get('controller_green')})")
        ratio = n / c if c else float("nan")
        verdict = "CUDA faster" if ratio > 1.0 else "CUDA SLOWER — keep CPU align for vigil"
        print(f"  speedup (CPU/CUDA): {ratio:.2f}x   -> {verdict}")
    else:
        print("  could not read align_process_ms from one or both results:")
        print(f"    cuda  : {sys.argv[1]}  ({'ok' if cs else 'MISSING'})")
        print(f"    nocuda: {sys.argv[2]}  ({'ok' if ns else 'MISSING'})")
    print("=======================================================")


if __name__ == "__main__":
    main()
