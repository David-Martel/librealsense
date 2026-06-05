# GB10 RealSense — Measured Performance Reference

**Date:** 2026-06-05  
**Host:** `spark-3066` (NVIDIA DGX Spark / GB10, sm_121, ARM64, Ubuntu 24.04.4, kernel 6.17.0-1021-nvidia)  
**Camera:** Intel D435 — firmware 5.15.1.55 — on rear-panel USB-3.2 5000 Mbps (own bus, `NVDA8000:00`)  
**SDK:** librealsense2 2.58.1 (David-Martel GB10 fork), build `build-gb10-full`:  
`RSUSB + BUILD_WITH_CUDA=ON + RS2_GB10_USB_TUNING=1` + CUDA OpenCV 4.14 + NVENC ffmpeg  
**Python:** uv venv CPython 3.12.3 + pyrealsense2 + numpy 1.26.4 + cv2.cuda  
**CUDA:** 13.0 · GPU: GB10 (unified / coherent memory, sm_121)

This file is the single authoritative performance reference for the GB10 RealSense tooling.
Numbers marked **re-run-today** were measured in this session (2026-06-05, offline, no camera).
Numbers marked **cited** come from dated HIL logs or docs; camera is required to reproduce them.

---

## Summary Table

| Operation | Metric | Value | Source | Camera? |
|-----------|--------|-------|--------|---------|
| **CUDA cached-pool byte-identity** — pointcloud + conversion, mode0 vs mode1, 5-res sweep | PASS/FAIL | **PASS** (all 10 cases) | re-run-today (`just test-cached`) | No |
| **rs.align** depth→color, CUDA vs NEON (p50 ms per call) | speedup | **15–19×** (CUDA 0.293–0.295 ms, CPU 4.33–5.67 ms) | cited — HIL-SOAK-AND-ACCEL-2026-06-03.md §4 + HIL-RESULTS | Yes |
| **rs.pointcloud** shipped (mode 0) vs CUDA cached (mode 1), 848×480 | speedup | **3.3× faster** (mode1 0.32 ms vs shipped 1.05–1.09 ms; mode1 also beats NEON 0.38–0.54 ms) | cited — CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES-2026-06-03.md Opp 1 | Yes |
| **rs.pointcloud** shipped CUDA (mode 0) vs NEON | relative perf | **0.57× (CUDA slower)** — per-frame alloc churn; caching fixes it | cited — HIL-SOAK-AND-ACCEL-2026-06-03.md §4 | Yes |
| **Color conversion** (YUYV→RGB) CUDA vs NEON, 1280×720×30 | relative perf | **~NEON-parity** (CUDA 2.00 ms/frame vs NEON 2.03 ms/frame, ±2%) | cited — CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES-2026-06-03.md Finding A | Yes |
| **P4 async pipelining** — conversion 848×480, overlap gain A→B | aggregate fps gain | **+64.7%** (14887 → 24514 fps); overlap eff 85.9% | re-run-today (`just bench-async`) | No |
| **P4 async pipelining** — pointcloud 1280×720, overlap gain A→B | aggregate fps gain | **−1.8%** (negative; D2H-dominated, GB10 contention) | re-run-today (`just bench-async`) | No |
| **P4 async pipelining** — NO-GO verdict for single-camera real-time | verdict | **NO-GO** (op already 80–270× camera rate; < 2.5% of frame budget) | re-run-today (`just bench-async`) | No |
| **NVENC h264_nvenc cq=23/p4** XPSNR-Y | dB | **39.14 dB** (perceptually high-quality) | re-run-today (`just nvenc-sweep`) | No |
| **NVENC h264_nvenc cq=23/p4** encode speed | ×real-time | **10.9×** | re-run-today (`just nvenc-sweep`) | No |
| **NVENC h264_nvenc cq=23/p4** file size vs source | ratio | **+39%** vs already-compressed source | re-run-today (`just nvenc-sweep`) | No |
| **NVENC cq/preset knee** | cq | cq=23 (2.1 dB cost, 31% size save vs cq=19; next step 3.1 dB for 26% save) | re-run-today (`just nvenc-sweep`) | No |
| **Keep-on-GPU render saving** (GL-resident chain vs D2H path) | ms/frame | **~2.9–3.3 ms/frame** at 1280×720; ~1.0 ms at 640×480; ~7.0 ms at 1920×1080 | cited — ROS2-GL-PINNED-FINDINGS-2026-06-05.md §2 | No (synthetic) |
| **ROS2 depth stream** (minimal config, 848×480×30, 25 s run) | fps / drops | **30.03 fps, 708 frames, 0 drops** | cited — ros2-depth-minimal-20260605-134449.log | Yes |
| **Advanced single-stream HIL** (depth 848×480@60, 300 frames) | effective fps | **57.17 fps** (1 stream gap; full CUDA+pointcloud+postproc chain) | cited — HIL-RESULTS-2026-06-03.md §2 | Yes |
| **Long soak — RSUSB clean bus** (phased single→dual→churn→quad) | controller | **GREEN, zero -110, SURVIVED** | cited — HIL-SOAK-AND-ACCEL-2026-06-03.md §1 | Yes |
| **P7 re-acquire guard false-fire** under strict REFUSE | false-fire rate | **0 / 5 opens** (guard stays silent; never fires on a valid session) | cited — HIL-RESULTS-2026-06-03.md §P7 | Yes |

---

## 1. CUDA Cached-Pool Byte-Identity — re-run-today

**Recipe:** `just test-cached` → `scripts/gb10/test-cached-pools.sh`  
**No camera required.** Compiles `test_cached_pools.cu` + `test_conv_cache_correctness.cu` with nvcc and runs them.

### What it proves

The cached buffer pools (`RS2_PC_MODE=1`, `RS2_CONV_MODE=1`) for both CUDA pointcloud and CUDA conversion:

1. **Byte-identical output** to the baseline (mode 0 / no-cache) over every resolution in the sweep — not merely "similar", not within a tolerance, but zero-diff identical.
2. **Grow-only allocator** — the pool correctly expands its device buffer on resolution increase (e.g. 848×480 → 1280×720) and reuses the larger allocation on a subsequent shrink (1280×720 → 424×240 → 1280×720 → 640×480).
3. **Mode selection** — mode 0 and mode 1 are correctly dispatched separately; neither contaminates the other.

### Result (2026-06-05)

```
== conv: mode0 (baseline) vs mode1 (cached) over 848x480 1280x720 424x240 1280x720 640x480 ==
  [PASS] 848x480 byte-identical (cached == baseline)
  [PASS] 1280x720 byte-identical (cached == baseline)
  [PASS] 424x240 byte-identical (cached == baseline)
  [PASS] 1280x720 byte-identical (cached == baseline)
  [PASS] 640x480 byte-identical (cached == baseline)
  conv: grow-only + byte-identical + mode-select OK
== pc: mode0 (baseline) vs mode1 (cached) over 848x480 1280x720 424x240 1280x720 640x480 ==
  [PASS] 848x480 byte-identical (cached == baseline)
  [PASS] 1280x720 byte-identical (cached == baseline)
  [PASS] 424x240 byte-identical (cached == baseline)
  [PASS] 1280x720 byte-identical (cached == baseline)
  [PASS] 640x480 byte-identical (cached == baseline)
  pc: grow-only + byte-identical + mode-select OK

CACHED-POOLS TEST: PASS
```

**10/10 PASS.** This is the gate that justifies shipping cached pools as the GB10 default: the optimization does not alter outputs, it only eliminates per-frame `cudaMalloc`/`cudaFree` churn (which makes the as-shipped CUDA path 0.57× NEON, ~1.75× slower; caching it yields 3.3× over the shipped path — see §4).

**Caveat:** byte-identity test is run on synthetic input (the host-side buffer filled by the test harness). It does not run on a live camera frame, but the kernel operates identically on any input data.

---

## 2. P4 Async Pipelining Microbench — re-run-today

**Recipe:** `just bench-async` → `scripts/gb10/async-pipeline-bench.sh`  
**No camera required.** Compiles `bench_async_pipeline.cu` linking the real library kernels (`cuda-pointcloud.cu`, `cuda-conversion.cu`) with nvcc -O3 -Werror; 400 timed iterations, 30 warmup discarded.

Variants:
- **REF** — shipped cached path (pageable memory, 1 stream, sync per frame) — the `RS2_PC_MODE=1` / `RS2_CONV_MODE=1` path
- **A** — pinned host memory, 1 stream, sync per frame (isolates pinning effect)
- **B** — pinned, K=4 CUDA streams, async batch sync (isolates overlap effect)

### Per-Stage Isolation (today's run)

| Workload | H2D (µs) | kernel (µs) | D2H (µs) | sum (µs) | overlap floor = max (µs) |
|----------|--------:|------------:|---------:|---------:|------------------------:|
| Pointcloud 848×480 | 18.9 | 9.9 | 88.4 | 117.1 | 107.2 |
| Pointcloud 1280×720 | 47.4 | 36.4 | 194.7 | 278.5 | 242.1 |
| Conversion 848×480 | 16.0 | 8.6 | 23.6 | 48.1 | 39.6 |
| Conversion 1280×720 | 34.9 | 15.4 | 49.6 | 99.9 | 84.5 |

D2H dominates in all cases; the kernel is small. This means copy time exists to hide, but on GB10's unified memory concurrent copies+compute contend for one pool, so overlap saturates quickly.

### A vs B Throughput and Overlap Efficiency (today's run)

| Workload | REF fps | A fps | B fps | Overlap gain A→B | Overlap eff | A lat p50 (µs) | B lat p50 (µs) |
|----------|---------:|------:|------:|----------------:|------------:|---------------:|---------------:|
| Pointcloud 848×480 | 7,174 | 8,160 | 8,638 | **+5.9%** | **13.2%** | 117 | 443 |
| Pointcloud 1280×720 | 3,569 | 3,698 | 3,631 | **−1.8%** | **8.5%** | 263 | 1,036 |
| Conversion 848×480 | 15,967 | 14,887 | 24,514 | **+64.7%** | **85.9%** | 57 | 162 |
| Conversion 1280×720 | 8,260 | 8,581 | 11,913 | **+38.8%** | **103.7%** | 112 | 334 |

Pinning gain (REF→A) is within noise (−6.8% to +13.7%) — coherent unified memory, pageable already near-DMA. Latency (B) is 3–4× higher than A throughout.

### NO-GO Verdict

Three decisive legs — confirmed again by today's re-run:

1. **No usable throughput headroom.** The CUDA op runs at 3,600–24,500 fps for a camera that delivers 30–90 fps. Even the best overlap gain (+64.7%) speeds a stage worth < 2.5% of an 11–33 ms frame budget. The dominant per-frame cost is plumbing (~2 ms; Finding A), which pipelining cannot reach.
2. **Overlap collapses under GB10 memory contention.** Pointcloud at 1280×720 shows −1.8% overlap gain (period_B ≈ sum_stages): concurrent copies and compute contend for the same unified memory pool.
3. **Pure cost downside.** K-stream breaks the byte-identical single-buffer cache contract (proven by `test-cached`), adds race-prone per-stream completion tracking, and raises per-frame latency 3–4×.

**Verdict: NO-GO for the shipped single-camera real-time path.** (Scope nuance: conversion at 848×480 shows +64.7% aggregate throughput. If a future offline/batch multi-camera workload appears, revisit the conversion path only.)

**SM clock during today's run:** ramps to 2,515 MHz sustained under bench load (idle reads ~312 MHz). The NO-GO verdict is clock-robust: higher clock shrinks the kernel relative to bandwidth-bound copies, deepening the "unusable headroom" conclusion.

Note on run-to-run variation vs prior doc: the prior run in `p4-async-pipelining.md` showed REF fps 7722/3533/15499/8680 vs today's 7174/3569/15967/8260 — small differences consistent with DVFS governor variance. The stage-table physics (D2H-dominated, overlap efficiency pattern) and the NO-GO conclusion are stable.

---

## 3. NVENC CQ×Preset Sweep — re-run-today

**Recipe:** `just nvenc-sweep` → `scripts/gb10/nvenc-cq-sweep.sh`  
**No camera required.** Sweeps `h264_nvenc` with `-rc vbr -cq N -b:v 0` at CQ ∈ {19,23,26,29,33} × preset ∈ {p4,p6} on the recorded clip. Quality measured by XPSNR (native ffmpeg filter; luminance-weighted 4:2:0 average).

**Input clip:** `~/realsense-gb10-validation/keepongpu-rec.mp4`  
(848×480, 30 fps, 7.73 s / 232 frames, 2,273,860 bytes / 2,348 kb/s; h264_nvenc output from the keep-on-GPU viewer `--record` path)

**Caveat on the source:** the input is already H.264-compressed (from a prior NVENC session), not raw RGBA frames. XPSNR is measured against this lossy reference; absolute values are inflated vs what a raw-frame recording would show. Cross-cq comparisons are valid; the knee location (cq=23) may shift one step toward cq=26 for raw-frame inputs.

### Preset p4 (today's run)

| cq | Size (bytes) | Size vs src | Bitrate (kb/s) | Encode (s) | Speed (×RT) | XPSNR-Y (dB) | XPSNR-U (dB) | XPSNR-V (dB) | XPSNR w-avg (dB) |
|----|-------------|-------------|---------------|-----------|-------------|-------------|-------------|-------------|-----------------|
| 19 | 4,591,219 | +102% | 4,751.6 | 0.756 | 10.2× | 41.26 | 38.24 | 37.83 | 40.18 |
| **23** | **3,157,953** | **+39%** | **3,268.3** | **0.707** | **10.9×** | **39.14** | **35.54** | **35.54** | **37.94** |
| 26 | 2,345,659 | +3% | 2,427.6 | 0.696 | 11.1× | 36.00 | 32.89 | 32.93 | 34.97 |
| 29 | 1,697,787 | −25% | 1,757.1 | 0.688 | 11.2× | 32.35 | 30.07 | 30.24 | 31.62 |
| 33 | 1,061,651 | −53% | 1,098.7 | 0.711 | 10.9× | 28.35 | 27.29 | 27.61 | 28.05 |

### Preset p6 (today's run)

| cq | Size (bytes) | Size vs src | Bitrate (kb/s) | Encode (s) | Speed (×RT) | XPSNR-Y (dB) | XPSNR-U (dB) | XPSNR-V (dB) | XPSNR w-avg (dB) |
|----|-------------|-------------|---------------|-----------|-------------|-------------|-------------|-------------|-----------------|
| 19 | 4,458,957 | +96% | 4,614.7 | 0.705 | 11.0× | 40.83 | 37.61 | 37.27 | 39.70 |
| 23 | 3,052,473 | +34% | 3,159.1 | 0.713 | 10.8× | 38.41 | 34.83 | 34.80 | 37.21 |
| 26 | 2,252,951 | −1% | 2,331.6 | 0.713 | 10.8× | 35.41 | 32.37 | 32.41 | 34.40 |
| 29 | 1,640,535 | −28% | 1,697.8 | 0.694 | 11.1× | 32.28 | 29.84 | 30.06 | 31.51 |
| 33 | 1,053,707 | −54% | 1,090.5 | 0.705 | 11.0× | 28.55 | 27.38 | 27.74 | 28.22 |

Both presets pass the monotonic-size check (PASSED by script today).

### Quality/Size Knee (p4)

| Step | XPSNR-Y drop | Size saved | dB/MB |
|------|-------------|-----------|-------|
| cq19 → cq23 | −2.12 dB | 1,433 KB (31%) | 1.51 dB/MB |
| cq23 → cq26 | −3.14 dB | 793 KB (26%) | 4.06 dB/MB |
| cq26 → cq29 | −3.65 dB | 633 KB (28%) | 5.91 dB/MB |
| cq29 → cq33 | −4.00 dB | 621 KB (37%) | 6.60 dB/MB |

**Knee is at cq=23.** Going cq19→cq23 costs only 2.1 dB and saves 31% size. Each subsequent step pays 3–4 dB for comparable savings.

### p4 vs p6

p6 saves 3–4% file size at most cq values but produces 0.4–0.7 dB *lower* XPSNR-Y than p4 at the same cq. **p4 is the better default.** Encode speed (10.2–11.2× RT) is identical within measurement noise; the 232-frame clip is too short to discriminate preset CPU complexity.

### Recommended Default

```
h264_nvenc  -rc vbr -cq 23 -b:v 0 -preset p4
```

39.14 dB XPSNR-Y at 10.9× real-time. This is the current `--record` default in the keep-on-GPU viewer.

---

## 4. CUDA Acceleration — Cited

Source: `docs/gb10/HIL-SOAK-AND-ACCEL-2026-06-03.md` §4 and `docs/gb10/CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES-2026-06-03.md`.  
Requires camera (both builds run back-to-back on a live scene).

### rs.align depth→color (vigil's per-frame hot-path op)

CUDA 0.293–0.295 ms/call (p50, stable) vs CPU(NEON+OpenMP) 4.33–5.67 ms/call (scene-dependent, zero-depth short-circuit). **Speedup: 15–19×.** This is the load-bearing CUDA win — align does heavy per-pixel reprojection across thousands of threads, which parallelizes well and dwarfs the H2D/D2H cost.

`rs.align` dispatches CUDA (if `rs2_is_gpu_available()`) → NEON → scalar. On GB10 the comparison is **CUDA vs hand-written NEON+OpenMP**, not vs naive scalar.

Reproduce: `just hil-align-bench` (2-stream, eyes-open, tripwire-armed, static scene required).

### rs.pointcloud — allocation churn attribution ladder

Measured with `just hil-pc-zerocopy` (single depth stream, SAFE; each rung correctness-verified, max_abs_diff=0.0 vs numpy deproject):

| Mode | p50 ms | vs NEON | What it isolates |
|------|-------:|--------:|-----------------|
| 0 — shipped (malloc+memcpy+free per frame) | 1.05–1.09 | 0.5× slower | reproduces HIL-SOAK result |
| **1 — cached-device** (alloc once, grow-only) | **0.32** | **1.4–1.7× faster** | allocation churn = the real cost |
| 2 — cached-managed (`cudaMallocManaged`) | 0.49–0.50 | ~equal | managed adds coherence overhead |
| NEON baseline | 0.38–0.54 (scene-dep.) | — | CPU reference |

**Shipping mode 0 is 3.3× slower than the cached-device (mode 1) path.** Mode 1 is the recommended GB10 setting. The `test-cached` bench (§1) confirms mode 1 is byte-identical to mode 0.

### Color conversion (YUYV→RGB, "Finding A")

Measured with `just hil-gpu-pipeline --convert-only` (color-only single stream, SAFE; `_pipeline_compare.py` compares process-total CPU/frame between CUDA and NOCUDA builds, 1280×720×30, static scene):

**CUDA 2.00 ms/frame vs NEON 2.03 ms/frame — CPU-equivalent (±2%, within noise).** No regression; conversion is not a bottleneck at this resolution/rate. The 62% lower CPU/frame seen in the full-pipeline measurement is attributable entirely to `rs.align`, not conversion.

Caveat: process-total CPU floors sub-ms differences; multi-stream or higher-res is untested.

### Per-Op Summary

| op | CUDA path | Measured on GB10 |
|----|:---------:|-----------------|
| `rs.align` depth→color | Yes (auto-selected) | **CUDA 15–19× > NEON** — keep `BUILD_WITH_CUDA=ON` |
| `rs.pointcloud` (shipped mode 0) | Yes (auto-selected) | **CUDA 0.57× < NEON** — alloc churn; mode 1 fixes it |
| `rs.pointcloud` (cached mode 1) | Yes | **1.4–1.7× > NEON** — byte-identical per §1 |
| Color conversion YUYV→RGB | Yes (auto-selected) | **~NEON-parity** — not a bottleneck |
| `rs.colorizer` | **No CUDA path** | CPU-only (has GL path in the GL pipeline) |
| Post-proc filters (spatial/temporal/hole/etc.) | **No CUDA/NEON/OMP** | Pure scalar — opt-in only |

---

## 5. Keep-on-GPU Render — Cited (Synthetic)

Source: `docs/gb10/ROS2-GL-PINNED-FINDINGS-2026-06-05.md` §2.  
Measured with synthetic `software_device` input (no camera). Correctness verified: max|Δ| ≤ 3/255 vs CPU colorize path.

GL-resident chain `gl::colorizer (GL texture) → FBO → final readback` vs the standard path that calls `get_data()` (synchronous D2H):

| Resolution | Keep-on-GPU saving (ms/frame) | Notes |
|------------|------------------------------:|-------|
| 640×480 | ~1.0 | D2H 0.49 + H2D 0.20 + sync stall |
| **1280×720** | **~2.9–3.3** | scales with pixel count |
| 1920×1080 | ~7.0 | sync-stall-dominated |

**This win applies only to GPU/render consumers** (display FBO, NVENC, CUDA interop) that never need the image on the host. A CPU consumer must pay D2H regardless.

Caveat: GL teardown path SIGSEGVs (known); production integration must handle teardown. Harness `_exit(0)`s after measurement.

Reproduce (no camera): `~/realsense-gb10-validation/posebench/gl/keep_on_gpu_bench.sh`  
(requires `build-gb10-gl` with `-DBUILD_GLSL_EXTENSIONS=ON -DBUILD_PC_STITCHING=ON`).

---

## 6. ROS2 Depth Stream — Cited

Source: `~/realsense-gb10-validation/ros2-depth-minimal-20260605-134449.log` and  
`docs/gb10/ros2-stream-start-analysis.md` (A/B results box).  
Requires camera (D435, safe single-stream envelope).

**Minimal-config launch** (`scripts/gb10/ros2-launch-depth-minimal.sh`):  
depth-only 848×480×30, AE on, no manual exposure, no multi-stream.

| Metric | Result |
|--------|--------|
| Depth frames received | **708** (index 0→707, Z16 848×480) |
| Frame rate | **30.03 fps** |
| Drops | **0** (707 expected over 23.57 s = 707 actual) |
| Fatal errors | None — no "Hardware Error", no -110 |
| Benign warnings | 2× `index 768 / 0x0300 EAGAIN` at startup (SDK rides through them) |
| Controller after | GREEN — device released cleanly |

Single-variable A/B (8 controlled runs on the same session):
- **Minimal config PASS** — 4/4 repeated PASS (ab0, ab1, ab2, ab4)
- **Original full-default FAIL** — deterministically 0 frames (ab3, ab5, ab6 still 0 frames)
- **H1 (manual-exposure-under-AE) REFUTED** — adding `depth_module.exposure:=8500` to minimal still streams (ab1); the fix is the override *combination*, not the exposure write alone.

`realsense2_camera` tag 4.58.1 builds cleanly against GB10 SDK 2.58.1 (zero compiler warnings).  
Reproduce (camera required): `just ros2-hil --live` or `just ros2-hil` (offline parse-log self-test of the 708-frame proven log).

---

## 7. NVENC Encode Fidelity (Live Clip) — Cited

Source: `docs/gb10/QUALITY-RESULTS-2026-06-03.md` §(a).  
Measured from a live 120-frame 848×480@30 capture; reference = lossless ffv1 of the same raw capture.

| Codec | SSIM | PSNR (dB) | XPSNR (dB) | Size | Ratio vs lossless |
|-------|------|-----------|------------|------|-------------------|
| h264_nvenc (p5, cq23) | 0.965 | 38.7 | 30.8 | 1.06 MB | 34× |
| hevc_nvenc (p5, cq23) | 0.971 | 39.2 | 31.8 | 0.91 MB | 40× |
| lossless ffv1 (ref) | 1.000 | ∞ | — | 36.6 MB | 1× |

Both codecs pass SSIM/PSNR visually-lossless thresholds (SSIM > 0.95, PSNR > 35 dB) at 34–40× compression. **Yellow flag: XPSNR ~31 dB** (perceptually "good" but not "excellent" — the gap vs SSIM means cq=23 may leave perceptual quality on the table at 34× ratio). The §3 sweep (cq×preset, today) provides the per-step XPSNR data to guide tuning.

Note: the §3 sweep used a re-encoded H.264 source (XPSNR inflated vs raw frames). The §7 numbers are from a raw live capture and are the ground-truth absolute XPSNR reference.

Reproduce (camera required): `just hil-quality`

---

## 8. Misc Operational Numbers — Cited

### Advanced single-stream per-op latency (HIL)

Source: `docs/gb10/HIL-RESULTS-2026-06-03.md` §2.  
Single depth 848×480@60, 300 frames, 30-frame warmup (camera required).

| Op | p50 (ms) | p95 (ms) | max (ms) | Note |
|----|:--------:|:--------:|:--------:|------|
| `wait_for_frames` | 10.66 | 11.84 | 16.04 | ~60 fps cadence |
| `rs.colorizer` (CPU — no CUDA path) | 1.53 | 1.60 | 2.01 | stable |
| `rs.pointcloud.calculate` (CUDA mode 0) | 0.98 | 1.62 | 222.3 | **first-call init stall** — pre-warm before hot loop |
| cv2.cuda pipeline (upload→resize→8u→download) | 0.40 | 0.52 | 36.4 | first-frame CUDA warmup stall |
| post-proc chain (dec+spatial+temporal+hole) | 3.10 | 3.15 | 4.61 | CPU, stable |

Effective throughput with the full per-frame chain: **57.17 fps** (1 gap over 300 frames).

### cv2.cuda GPU-vs-CPU (camera-free synthetic workload)

Source: `docs/gb10/QUALITY-RESULTS-2026-06-03.md` §(b).

| Op | GPU (ms/op) | CPU (ms/op) | Speedup |
|----|:-----------:|:-----------:|--------:|
| `cvtColor` BGR2GRAY | 0.80 | 0.38 | **0.5× (GPU slower)** |
| `resize` 2× upscale | 6.2 | 23.5 | **3.8× (GPU faster)** |

GB10 unified-memory dispatch overhead dominates trivial ops. The acceleration win requires heavy ops and keep-on-GPU pipelines.

---

## How to Reproduce

| Bench | Recipe | Camera? | Notes |
|-------|--------|---------|-------|
| Cached-pool byte-identity | `just test-cached` | No | nvcc required; ~10 s |
| P4 async pipelining | `just bench-async` | No | nvcc required; ~3 min; GPU ramps to 2500+ MHz |
| NVENC cq×preset sweep | `just nvenc-sweep` | No | requires `keepongpu-rec.mp4` in `~/realsense-gb10-validation/`; ~2 min |
| NVENC sweep (custom input) | `just nvenc-sweep INPUT=/path/to/clip.mp4 CQ_LIST="19 23 26" PRESET_LIST="p4"` | No | |
| Keep-on-GPU render | `~/realsense-gb10-validation/posebench/gl/keep_on_gpu_bench.sh` | No | synthetic; needs `build-gb10-gl` |
| rs.align CUDA bench | `just hil-align-bench` | **YES** | 2-stream, tripwire-armed, static scene |
| rs.pointcloud cache ladder | `just hil-pc-zerocopy` | **YES** | single depth stream, SAFE |
| Color conversion bench | `just hil-gpu-pipeline --convert-only` | **YES** | single stream, SAFE |
| Advanced single-stream HIL | `just hil-advanced` | **YES** | single depth @60, SAFE |
| NVENC live fidelity | `just hil-quality` | **YES** | single color @30, SAFE |
| ROS2 depth stream | `just ros2-hil --live` | **YES** | single depth, SAFE envelope |
| ROS2 parse-log self-test | `just ros2-hil` | No | replays the proven 708-frame log |
| Long soak | `just hil-soak` | **YES** | multi-stream, eyes-open, tripwire-guarded |

**SAFE envelope:** single high-rate stream on the clean USB-3 bus (`NVDA8000:00`), never through the dock.  
Multi-stream operations are **eyes-open** — the GB10 xHCI controller can die (requires reboot) after a Stop-Endpoint timeout; all multi-stream HIL recipes are tripwire-armed.

---

## Caveats and Honest Scope

1. **Synthetic vs real input.** `test-cached` and `bench-async` operate on synthetic buffers, not live camera frames. The kernels are identical — but on-camera thermal/power state, USB latency jitter, and memory traffic patterns differ from synthetic. Correctness (byte-identity) is fully offline-verifiable; timing on synthetic is a lower bound on real-world variance.

2. **NVENC sweep source.** The sweep input (`keepongpu-rec.mp4`) is already H.264-compressed. XPSNR values are inflated relative to raw-frame recording. The knee (cq=23) is reliable for comparisons; absolute dB numbers are not transferable to a raw-RGBA source without re-sweeping.

3. **Alignment bench is scene-dependent.** CPU path for `rs.align` short-circuits zero-depth pixels, so its time varies with scene depth coverage (4.33–5.67 ms across two runs). CUDA side is scene-invariant (0.293–0.295 ms).

4. **P4 bench clock variance.** SM clock idles at 312 MHz but ramps to 2515 MHz under bench load (today) vs 2548 MHz (prior doc run). Small difference; the NO-GO verdict is explicitly clock-robust (higher clock makes the op more copy-bound, deepening the verdict).

5. **GB10 unified memory lesson (Finding A / P4 / pinned memory).** Three independent measurements confirm: on GB10 coherent unified memory, *the lever is eliminating allocation churn, not touching copies*. Cached-device pageable buffers are the right operating point for both pointcloud and conversion.

6. **Multi-stream safety boundary.** Long soak on the clean USB-3 bus SURVIVED (HIL-SOAK-AND-ACCEL). This is NOT an envelope relaxation — four confounds changed at once, runs were ~5 minutes, and V4L2 soak data is still absent. Conservative single-stream guidance remains for anything that must not fail.

7. **ROS2 0x0300 root cause.** The minimal-config fix is proven (4/4 PASS); H1 (manual-exposure-under-AE write) is REFUTED. The actual cause is a combination of node-default parameter overrides that cannot be isolated to a single variable with the current A/B runs. A follow-on 2^N parameter-subset scan is deferred.
