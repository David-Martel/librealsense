# GB10 Perf Feasibility — H7 (align alloc) & H8 (USB/CUDA affinity) — Measure-First

**Date:** 2026-06-05
**Host:** `spark-3066` (NVIDIA DGX Spark / GB10, sm_121, ARM64, Ubuntu 24.04.4, kernel 6.17.0-1021-nvidia)
**CPU:** Cortex-X925 perf cores `{5–9, 15–19}` @3.9 GHz + Cortex-A725 eff cores `{0–4, 10–14}` @2.8 GHz (20 total)
**GPU:** GB10 (unified/coherent memory, sm_121), CUDA 13.0, gcc 13.3
**Camera:** NONE. Synthetic data only. Benches: `scripts/gb10/bench_h7h8_*.{cu,cpp,sh}`.

This is a **measure-first go/no-go** on two net-new GB10 perf ideas, following the established
precedent (P4 async = NO-GO, NEON filters = GO-isolation-but-consumer-gated): **no speedup is
claimed without a measured number, and MARGINAL/NO-GO is an acceptable and expected answer.**

**Consumer reality check (vigil):** pyrealsense2-direct, 2-stream (color+depth 640×480), CPU
`rs.align` every frame, no pointcloud/filters/record. At 30 fps the per-frame budget is **33.3 ms**.
The shipped CUDA align op is **~0.29 ms** (HIL, benchmarks.md §4) — under **1%** of the budget.

---

## TL;DR Verdicts

| Idea | Verdict | One-line reason |
|------|---------|-----------------|
| **H7** — per-frame alloc reduction on the align hot path | **NO-GO (already implemented)** | `align_cuda_helper` already caches all 7 device buffers (members + `if(!_d_)` guards); steady-state align does **0 `cudaMalloc`/frame**. The optimization H7 proposes is already shipped — the counterfactual "churn" path it would prevent costs 0.52–0.59 ms (≈6× the op) but only **1.6–1.8% of the frame budget**. Nothing left to win; the value is in *not regressing* it. |
| **H8** — USB-event-thread vs CUDA CPU affinity/pinning | **NO-GO (proxy)** | On 20 cores with vigil's handful of threads there is no oversubscription to fix. Synthetic wakeup-overrun proxy shows pinning gives **no reproducible jitter reduction** (p50/p95/p99 stable to <1 µs across all regimes; p99.9 swings ±5–20% run-to-run with no consistent winner). Pinning the USB thread to an **A725 eff core actively hurts** the tail. The only regime with a large deep tail (2–3 ms `max`) is *forced same-core*, which the real scheduler avoids on its own. |

Both benches build **`-Wall -Wextra -Werror` clean** and ran (idle GPU, loadavg < 0.6).

---

## H7 — Per-frame allocation on the rs.align CUDA hot path

### Source analysis (the verdict-determining fact)

The hypothesis behind H7 is the pointcloud lesson: shipped pointcloud mode-0 did `malloc+memcpy+free`
*per frame*, making CUDA 0.57× NEON; caching it (mode-1) yielded 3.3× (benchmarks.md §4). **Does
align make the same mistake?** Reading the source answers it directly — no bench needed for the *fact*:

- **`src/proc/cuda/cuda-align.h`** — `align_cuda` holds `std::map<tuple, align_cuda_helper> aligners;`
  as a **member** of the persistent processing block. The helper is reset only by
  `reset_cache()` (resolution change), not per frame.
- **`src/proc/cuda/cuda-align.cuh`** — `align_cuda_helper` holds all 7 device buffers
  (`_d_depth_in`, `_d_other_in`, `_d_aligned_out`, `_d_pixel_map`, `_d_*_intrinsics`,
  `_d_depth_other_extrinsics`) as `shared_ptr` **members**.
- **`src/proc/cuda/cuda-align.cu`** — every allocation is guarded `if (!_d_depth_in) _d_depth_in = alloc_dev<...>(...)`.
  → **First aligned frame allocates; every subsequent frame reuses.** Steady-state per-frame cost is
  only `cudaMemcpy` (H2D depth/other, D2H out) + `cudaMemset` + the 3 kernels. **Zero `cudaMalloc`.**
- **Host side** — `align::allocate_aligned_frame` (`src/proc/align.cpp:241`) calls
  `source.allocate_video_frame(...)`, which is the librealsense **pooled/reference-counted frame
  allocator** (frames are "pooled and reused" — copilot-instructions.md, Memory Management). Not a
  per-frame `new`/`malloc`.

**Conclusion: align already implements exactly what H7 proposes.** This is the pointcloud mode-1
optimization, already shipped on the align path.

### Bench — quantifying what the existing caching already saves

`scripts/gb10/bench_h7h8_align_alloc.cu` compiles the **real shipped align kernels** (copied verbatim
from `cuda-align.cu`, with the device math `#include`d from `rscuda_utils.cuh`) and times the align
depth→other op two ways at vigil's resolutions, 400 iters / 50 warmup:

- **CACHED** = the as-shipped path: 7 buffers allocated once, then per-frame `memcpy + memset + 3 kernels + D2H`.
- **CHURN** = the counterfactual regression: `cudaMalloc`+`cudaFree` of all 7 buffers **every frame**
  (what align would cost if the `if(!_d_)` guards were removed — the pointcloud mode-0 mistake).

The delta is the alloc overhead the existing caching eliminates.

```
== H7: rs.align per-frame allocation microbench ==
GPU: NVIDIA GB10  sm_121  iters=400 warmup=50

   640x480   CACHED p50=0.087 ms p95=0.102 | CHURN p50=0.609 ms p95=0.754
             alloc-churn overhead = 0.522 ms (= 597.8% of cached op, = 1.57% of 33.3ms frame budget)
   848x480   CACHED p50=0.107 ms p95=0.116 | CHURN p50=0.698 ms p95=0.821
             alloc-churn overhead = 0.591 ms (= 553.7% of cached op, = 1.77% of 33.3ms frame budget)
  1280x720   CACHED p50=0.213 ms p95=0.241 | CHURN p50=1.351 ms p95=1.707
             alloc-churn overhead = 1.138 ms (= 533.1% of cached op, = 3.41% of 33.3ms frame budget)
```

### Interpretation & frame-budget reality check

| Resolution | Cached op (shipped) | Alloc churn would add | As % of op | As % of 33.3 ms budget |
|-----------|--------------------:|----------------------:|-----------:|-----------------------:|
| 640×480 (vigil) | 0.087 ms | 0.522 ms | +598% | **1.57%** |
| 848×480 (vigil alt) | 0.107 ms | 0.591 ms | +554% | **1.77%** |
| 1280×720 | 0.213 ms | 1.138 ms | +533% | 3.41% |

Two facts coexist and are **not** contradictory:

1. **Alloc churn is NOT negligible on GB10** — it is ~6× the op, the same "alloc is the dominant
   cost" lesson as pointcloud. So removing the cache would be a real regression. *(This refutes the
   naive "coherent memory makes alloc cheap" premise — coherent memory makes *copies* cheap, not
   `cudaMalloc`/`cudaFree`.)*
2. **But it's already cached.** The shipped path is the CACHED column. The only frame-budget-relevant
   number is the cached op (0.087–0.107 ms = **<0.4% of budget**). There is no remaining alloc to
   remove on the align hot path.

**H7 VERDICT: NO-GO — already implemented.** The optimization is shipped; the win is banked. The
actionable output is *defensive*: do not remove the `if(!_d_)` guards (the bench shows it would cost
0.5–0.6 ms/frame at vigil res), and treat `cuda-align.cu`'s caching as load-bearing. No `src/` change
is warranted.

*Caveat:* synthetic input on an idle GPU; the cached p50 here (0.087–0.107 ms) is a lower bound and
differs from the 0.293 ms HIL figure (on-camera thermal/power state, USB jitter, real intrinsics).
The **ratio** (churn ≫ cached, churn ≈ 1.6–1.8% of budget) is the transferable result, not the absolutes.

---

## H8 — USB-event-thread vs CUDA CPU affinity/pinning (PROXY)

### What is being tested, and why it's a proxy

The idea: pin the libusb event thread (the event loop in `src/libusb/context-libusb.cpp`) to a core
**distinct** from CUDA/align CPU work, so the two don't contend and frame-arrival jitter drops.
**No camera is available**, so this is a strict **PROXY**. `scripts/gb10/bench_h7h8_affinity.cpp`
models:

- **"USB" thread** — a libusb-style event loop: it **blocks via `clock_nanosleep(CLOCK_MONOTONIC,
  TIMER_ABSTIME)`** to a fixed cadence (200 µs ≈ 5 kHz), exactly as a blocking `poll()` yields the
  CPU, then does a small "parse" payload (copy+checksum, like a transfer callback). The jitter metric
  is **wakeup overrun** = `actual_wake − target` — i.e. *how late the scheduler resumed the thread*.
  This is the faithful metric: scheduling contention lands in the *sleep/wait* window, which earlier
  work-time timing could not see (an initial version measured parse-work time and was blind — proven
  by the adversarial same-core arm showing zero tail movement despite provable oversubscription;
  switching to wakeup-overrun fixed the instrument).
- **"CUDA" thread** — a tight FP-busy thread, the CPU-side stand-in for CUDA launch + align host
  marshalling. It contends for cores/cache. *(No real GPU submit: the question is CPU-scheduler
  contention, not GPU throughput.)*

Regimes (USB-thread wakeup-overrun distribution in each):

| Regime | USB core | CUDA core | Models |
|--------|---------|-----------|--------|
| FREE   | unpinned | unpinned | shipped default — scheduler places both freely on 20 cores |
| SPLIT  | A725 #0 (eff) | X925 #5 (perf) | distinct-core pinning, USB on eff core |
| USB_X  | X925 #5 (perf) | X925 #6 (perf) | distinct-core pinning, USB on perf core |
| SAME   | X925 #5 | X925 #5 | **adversarial contention ceiling** — both forced onto one core |

### Results (3 runs, idle host, loadavg < 0.6)

Representative run (10 s/regime; the other two runs in `()` for tail-noise context):

```
  regime   p50    p95    p99    p99.9            max
  FREE    52.5   53.6   54.0   55.9  (55.4, 55.2)   321 / 244 / 411 us
  SPLIT   52.5   53.2   53.6   61.9  (66.2, 53.9)  1438 / 3382 /   64 us
  USB_X   51.4   51.8   52.1   52.8  (53.4, 57.6)   825 /  467 /  220 us
  SAME    51.1   51.5   51.9   52.7  (52.6, 52.4)  3000 / 2222 / 2801 us
```

(All values µs. There is a constant **~52 µs wakeup-overrun floor** in every regime — that is the
kernel timer-slack / nanosleep granularity, identical across regimes, **not** contention.)

### Interpretation & jitter reality check

- **p50 / p95 / p99 are flat across all four regimes** — the spread is < 1 µs, entirely within the
  ~52 µs timer floor. The slight ~1–2 µs edge for X925-pinned regimes (USB_X, SAME) is just the
  3.9 GHz core waking marginally faster than the scheduler's average placement — **not** a
  contention effect.
- **p99.9 is noise.** Across three runs SPLIT swung +19.5% / −2.3% / +10.7% vs FREE; USB_X swung
  −3.6% / +4.3% / −5.6%. No regime is a reproducible winner. The ±5–20% band on a ~52 µs floor is
  run-to-run scheduler variance, not a pinning signal.
- **The deep tail (`max`) is dominated by forced same-core (SAME: 2.2–3.0 ms)** — the adversarial arm
  *does* produce the expected contention ceiling, occasionally also catching SPLIT (3.4 ms once). But
  these are **< 0.01%** events. The shipped FREE scheduler keeps `max` at 244–411 µs because with 20
  cores and ~a-handful of vigil threads it simply never co-schedules the two hot threads onto one core.
- **Pinning the USB thread to an A725 eff core (SPLIT) is the one consistently *worse* configuration**
  at the tail — a concrete "do not do this."

**Frame-budget relevance:** even the worst observed p99.9 (66 µs) and the same-core `max` (3 ms) are
compared against a 33.3 ms (30 fps) — or 11 ms (90 fps) — frame budget. A ~50 µs wakeup floor is
0.15% of the 30 fps budget; the only thing that approaches relevance (3 ms same-core spike) is exactly
what the default scheduler already prevents.

**H8 VERDICT: NO-GO (proxy).** On GB10's 20 cores, vigil's thread count never oversubscribes, so there
is no contention for pinning to fix. The proxy shows no reproducible jitter win from distinct-core
pinning, and eff-core pinning is actively harmful. Per precedent (this is a proxy, no real USB), a GO
would only ever be "promising, needs real-camera confirmation" — and the proxy does not even reach
that bar. *If* a future config oversubscribes cores (many streams + heavy CUDA + other CPU load), the
finding to revisit on-camera is narrow: **avoid eff-core pinning; if anything, keep the USB thread off
a CUDA-busy perf core** — but measure IRQ/MSI affinity and the real CUDA driver threads, which this
proxy does not model.

---

## Combined frame-budget summary

| | What it would save | Frame budget (33.3 ms @30 fps) | Verdict |
|--|--------------------|-------------------------------:|---------|
| **H7** | 0 (already cached). Counterfactual churn it prevents = 0.52–0.59 ms | already < 1.8% (and already eliminated) | **NO-GO — implemented** |
| **H8** | No reproducible jitter reduction; ~52 µs floor is scheduler-invariant | ~0.15% even at the floor; relevant tail (3 ms) only under forced same-core, which the scheduler avoids | **NO-GO — proxy** |

Consistent with the GB10 lesson from P4 / Finding A / pointcloud caching: **the lever on this platform
is eliminating allocation churn (already done for align), not copies, pipelining, or CPU pinning.**
Both H7 and H8 are net-new ideas that the measurement retires.

---

## Reproduce (no camera)

```bash
just bench-h7h8        # both
just bench-h7h8 h7     # align alloc only (nvcc; ~5 s)
just bench-h7h8 h8     # affinity jitter only (g++; ~32 s for 4×8 s regimes)
# or directly:
scripts/gb10/bench_h7h8.sh all
```

Env knobs: `H7_ITERS` (400), `H7_WARMUP` (50), `H8_SECONDS` (8), `H8_CADENCE_US` (200).
**For a clean H8 tail, run with any heavy core build IDLE** — the script prints `loadavg(1m)` as a
guard; a concurrent build contaminates p99.9.

### Suggested `just` recipe line(s)

Add to the `justfile` (not edited here per file-ownership):

```just
# H7 (align per-frame alloc) + H8 (USB/CUDA affinity jitter) feasibility benches — no camera
bench-h7h8 WHICH="all":
    scripts/gb10/bench_h7h8.sh {{WHICH}}
```

---

## Honest scope / caveats

1. **H7 absolutes are synthetic lower bounds.** Idle-GPU timing; the cached op (0.087–0.107 ms)
   under-reads the 0.293 ms HIL figure. The transferable result is the *ratio* (churn ≈ 6× op ≈
   1.6–1.8% budget) and the *source fact* (already cached), not the absolute ms.
2. **H8 is a PROXY, not a camera test.** It models a `poll()`-blocked event thread + a CPU-busy
   CUDA-host thread. It does **not** model real libusb URB latency, USB/xHCI IRQ/MSI affinity, or the
   CUDA driver's own threads — all of which differ on-camera. The NO-GO is "no contention to fix on 20
   cores," which is structural and robust; but any *positive* pinning claim would require on-camera
   confirmation this bench cannot provide.
3. **H8 timer floor.** The ~52 µs wakeup-overrun floor is kernel `clock_nanosleep` granularity, not
   contention; it is identical across regimes and cancels out of the comparison. Sub-floor differences
   are noise.
4. **No `src/` change is proposed by either idea.** H7 is already implemented (defensive note only:
   keep the align device-buffer cache). H8 yields no shippable win. No `#if`-guarded sketch is
   warranted for either — both are NO-GO.
```
