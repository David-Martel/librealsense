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
| **CUDA zero-copy — rs.pointcloud** 848×480 (2026-09-10) | p50 ms | **0.234 → 0.135 ms (−42%)** with `BUILD_WITH_CUDA_ZEROCOPY=ON` | §9 below | Yes |
| **CUDA zero-copy — rs.align** depth→color 848×480 (2026-09-10) | p50 ms | **0.321 → 0.301 ms (−6.2%)**, inputs mapped only | §9 below | Yes |
| **CUDA zero-copy — align OUTPUT mapped** (2026-09-10) | p50 ms | **0.321 → 2.237 ms (+6.9×, REGRESSION)** — atomics on host memory | §9 below | Yes |
| **`-ffp-contract=off` cost** on GB10 depth filters (2026-09-10) | relative perf | **≤2.5%, mostly <1%** — bit-identity is effectively free; keep `off` | §10 below | No |
| **D435 codec capability** — firmware 5.17.3.10 (2026-09-10) | formats | **Y8/Y16/Z16/RGB8/RAW16/YUYV/BGRA only** — no MJPEG, no H.264/H.265, no Z16H | §11 below | Yes |
| **R6 multistream envelope ramp** — spark-3066, kernel 6.17.0-1029 (2026-09-10) | verdict | **12/12 PASS, 0 kernel USB faults**, incl. the config that crashed the xHCI 2026-06-02 | §12 below | Yes |

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


---

## 9. CUDA zero-copy on GB10 — measured 2026-09-10

**Not comparable to the June rows above without care.** Different SDK (**2.58.4**, not 2.58.1) and
different camera firmware (**5.17.3.10**, not 5.15.1.55). Every number in this section was measured
in one session on `spark-3066` with CUDA 13.0, so the ON/OFF comparisons within it are internally
valid; comparisons against June's absolute figures are not.

### The runtime gate does pass

`BUILD_WITH_CUDA_ZEROCOPY` allocates frame buffers with `cudaHostAlloc(cudaHostAllocMapped)` and is
gated at runtime on `cudaDevAttrIntegrated`. That GB10 satisfies this was **measured, not assumed**:

```
device            : NVIDIA GB10 (sm_121)
INTEGRATED        : 1        <- the gate librealsense uses
canMapHostMemory  : 1     pageableMemAccess : 1
concurrentManaged : 1     usesHostPageTables: 1
```

### Results — D435 848×480, 200–400 frames per leg

| Op | zero-copy OFF | zero-copy ON | Δ |
|---|---:|---:|---|
| `rs.pointcloud` (p50) | 0.234 ms | **0.135 ms** | **−42%** |
| `rs.align` depth→color (p50) | 0.321 ms | **0.301 ms** | **−6.2%** |
| `rs.colorize` (p50) — control, no CUDA path | 1.952 ms | 1.943 ms | ~0 |

align was measured over 400 frames × 3 alternating runs per leg to cancel scene and thermal drift;
p95 also improved (0.367 → 0.334 ms).

### The important part: zero-copy is NOT uniformly a win

`cuda-align.cu` was not wired for zero-copy upstream. Wiring it naively made align **6.6× slower**.
The per-buffer ladder (`RS2_ALIGN_ZC`, added in this session) isolates why:

| Mode | What maps | p50 |
|---|---|---:|
| 0 | nothing (upstream staging) | 0.321 ms |
| **1** | **inputs only — default** | **0.301 ms** |
| 2 | output only | 2.237 ms (**+6.9×**) |
| 3 | inputs and output | 2.195 ms (**+6.8×**) |

The discriminator is the **access pattern of the mapped buffer, not zero-copy itself**:

- Reads of the depth/colour planes are streaming and coalesced → serving them from mapped host
  memory costs almost nothing and saves a full-frame H2D.
- `kernel_depth_to_other` resolves occlusion with `atomic_min_uint16`. **Atomics against host memory
  over the coherence fabric are dramatically slower than against device-local memory.** Keeping the
  output in device memory and paying one D2H is far cheaper.
- `cuda-pointcloud.cu` writes its output with no atomics — one point per thread — which is why
  upstream's mapping of *its* output is a 42% win rather than a regression.

**Generalisable rule for GB10: map streaming reads, keep atomic or scattered writes device-local.**
"Unified memory means copies are free" is wrong here, and by a factor of ~7.

### Correctness

**Both** optimizations are byte-identical, checked independently — timing alone was not accepted as
grounds for changing a default:

- **align**: a 164-frame `.db3` playback under `RS2_ALIGN_ZC` modes 0, 1 and 3 produces the same
  SHA-256 over every aligned depth plane.
- **pointcloud**: the same playback with zero-copy **OFF** and **ON** produces the same SHA-256 over
  every vertex buffer (164 frames, `2880dcd326d0…`). This matters more than it looks: mapped and
  device-local memory could plausibly have taken different rounding paths, which would have made
  −42% a silent output change rather than a win.

**`BUILD_WITH_CUDA_ZEROCOPY=OFF` is compiled and gated, not merely reasoned about** (2026-09-10).
`OFF` is upstream's default in `CMake/lrs_options.cmake`, so the `#else` legs of `align_zc_mode()`
and the `try_device_ptr → nullptr` path are what every other downstream actually builds — and until
this run they had never been compiled with the align patch in the tree. A scratch prefix
(`LRS_GB10_CUDA_ZEROCOPY=OFF`, spark-3066, CUDA 13.0) was built from merged master `df4693bcb`:

| Check | Result |
|---|---|
| `cuda-align.cu.o` compiles in the OFF config | yes (137 KiB object) |
| full `ninja` + `ninja install`, incl. `pyrealsense2` | rc=0 |
| provenance line | `build.BUILD_WITH_CUDA_ZEROCOPY=0` |
| `rs-gb10-profiler --self-test` | **32/0** |
| `rs-enumerate-devices` / `rs-fw-update` / `rs-record` | rc=0 |
| aligned-depth SHA-256 over the 164-frame fixture | `2bf7df33fea0d19c…` |

That SHA-256 is **identical** to the canonical ON prefix under both `RS2_ALIGN_ZC=1` (the new
default) and `RS2_ALIGN_ZC=0`. So all three configurations — upstream-default OFF, ON/staging, and
ON/inputs-mapped — produce bit-identical aligned depth; the optimization is a pure timing change and
the non-zero-copy path is genuinely untouched. The probe prefix was pruned after the gate.

**Reproduced on a clean artifact.** The numbers above were first measured on a build assembled
incrementally, so they were re-taken on the canonical prefix built from merged master
(`164f19674`, `v2.58.2-1474-g164f19674`): align p50 **0.308 ms**, pointcloud p50 **0.136 ms**,
colorize 1.921 ms — matching to within run-to-run noise. Profiler self-test **32/0** reporting
`BUILD_WITH_CUDA_ZEROCOPY=1`, all 10 tools rc=0 ×3, 0 duplicated globals exported.

`LRS_GB10_CUDA_ZEROCOPY` now defaults **ON** in `scripts/build-dgx-spark-gb10.sh` on this evidence.

---

## 10. `-ffp-contract` — measured 2026-09-10, camera-free

Upstream `08b6d0031` hard-coded `-ffp-contract=off` for bit-identical filter output. On aarch64
every NEON lane has a fused multiply-add, so the question is what that bit-identity costs. Made
selectable (`RS2_FP_CONTRACT` / `FP_CONTRACT=` in `bench-filters.sh`) and measured, 300 iterations:

| Filter (1280×720, deterministic rows) | `off` | `fast` | Δ |
|---|---:|---:|---|
| threshold scalar/autovec | 1.5686 | 1.5405 | −1.8% |
| threshold neon | 0.2861 | 0.2856 | −0.2% |
| disparity scalar/autovec | 1.3003 | 1.3063 | +0.5% |
| disparity neon | 0.2600 | 0.2599 | ~0 |
| temporal scalar/autovec | 4.2597 | 4.2433 | −0.4% |
| temporal neon | 0.9050 | 0.8824 | −2.5% |
| decimation scalar/autovec | 2.0602 | 2.0565 | −0.2% |
| spatial-hv scalar/autovec | 15.7042 | 15.6897 | −0.1% |

**Verdict: keep `off`.** The cost is ≤2.5% and mostly under 1% — inside run-to-run noise for most
rows — so there is nothing to buy by giving up bit-identity. Both modes also reported *all variants
bit-identical to the scalar reference*, so on these filters GCC does not actually contract
differently across flavours; the guarantee upstream wanted is being had for free.

The OpenMP rows are excluded from the comparison: they swing far more than the effect size
(e.g. temporal neon+omp 0.393 vs 0.213 ms) because of thread scheduling, not contraction.

**Scope:** this measures **host** filter code only. `-ffp-contract` in `CMAKE_C/CXX_FLAGS` never
reaches device code — `nvcc` defaults to `--fmad=true`, so the CUDA kernels already fuse regardless.

---

## 11. D435 codec capability — measured 2026-09-10

The question was whether the camera can compress on-device (H.264/H.265/smarter depth coding) to
relieve the USB link. **It cannot.** Firmware **5.17.3.10** advertises only:

```
Y8   Y16   Z16   RGB8   RAW16   YUYV   BGRA
```

No MJPEG, no H.264, no H.265, no Z16H. The D4 ASIC has no video encoder, so **compression cannot
come from firmware on this camera** — it must be host-side on GB10 (NVENC/NVDEC), which the June
NVENC rows above already characterise (h264_nvenc cq=23 → 10.9× real-time, 39.14 dB XPSNR-Y).

This also means the USB link carries raw frames, and the binding constraint is the **xHCI
controller defect, not bandwidth**: 848×480 Z16@60 ≈ 49 MB/s plus 1080p YUYV@30 ≈ 124 MB/s sit well
inside USB 3.2 Gen 1.

**Caveat for any depth-compression work:** H.264/HEVC are 8-bit-luma codecs and quantise 16-bit Z16
destructively. Encoding depth needs either a plane-split into two 8-bit channels or a lossless /
Main12 HEVC profile. That is a real experiment, not a settled result, and is not claimed here.


---

## 12. R6 multistream envelope ramp — measured 2026-09-10

The GB10 xHCI controller-death defect produced the fleet's **single-high-rate-stream-only**
operating envelope. Because that envelope has been enforced continuously since June, "the defect is
fixed" and "the defect was never provoked again" have been observationally identical. This is the
test that separates them.

**The premise did change:** spark-3066's kernel has moved **6.17.0-1021-nvidia → 6.17.0-1029-nvidia**
since the June baseline. Platform provenance recorded in full, because no June document captured a
BIOS version and every earlier comparison was therefore impossible:

```
kernel  6.17.0-1029-nvidia      BIOS 5.36_0ACUM018 (2025-08-06)
product NVIDIA_DGX_Spark        D435 347622075921 firmware 5.17.3.10
```

### Results — `scripts/gb10/rs-gb10-stress.sh`, two sweeps

| Sweep | Entries | Result | Kernel USB faults |
|---|---|---|---|
| 12 s/entry | 12 | **12 PASS / 0 fail** | **0** |
| 60 s/entry (~12 min streaming) | 12 | **12 PASS / 0 fail** | **0** |

Including, at 60 s/entry:

| Entry | Measured |
|---|---|
| **`HEAVY_60fps_848x480_D+C+IR`** — source-annotated *"crashed GB10 xHCI 2026-06-02"* | 59.53/60 fps on all three streams, 0 gaps |
| `90fps_848x480_D+IR` | 89.72/90 fps |
| `300fps_848x100_depth` | 293.68/300 fps |
| `60fps_960x540color_+848D` | 59.41/60 fps |

Fault tally with benign librealsense/UVC noise excluded (`USBDEVFS_CLEAR_HALT`, UVC control
`981ae2`) — **all zero**: USB disconnect, clear_halt, xhci fail, device reset, cannot enable,
over-current, bandwidth, descriptor read, babble.

Separately, dual depth+color 848×480@60 with per-frame `rs.align` — the incident-#3 configuration
class — ran **5 consecutive times**: 300/300 frames each, 0 stream gaps, 59.52–59.54 fps, 0 faults
per run. The camera enumerated normally afterwards and no service was disrupted (`vigil-router` and
all three CI runner containers stayed up; no reboot was needed).

### Verdict: do NOT lift the envelope yet

The lethal configuration class is no longer lethal on this kernel. That is a real change and it is
now measured rather than assumed. It is **not** sufficient to change fleet policy:

1. **This is spark-3066 only.** Its D435 sits on a **native xHCI root port**; **spark-0060's sits
   behind a hub**, which is a materially different USB topology and is unvalidated.
2. **~12 minutes is not a soak.** The June defect was intermittent, so absence over minutes is much
   weaker evidence than the original presence.
3. Nothing here isolates *which* change fixed it — kernel, BIOS, and camera firmware all moved
   together since June. **And so did the SDK**: the June crash was 2.58.1, whereas this ramp ran
   2.58.4 with zero-copy and the align patch. The GB10 USB mitigations (`RS2_GB10_USB_TUNING`) are
   present in both, so the SDK is an unlikely cause — but it is a fourth uncontrolled variable and
   should be named as one.

**Recommendation: keep the single-high-rate-stream envelope until a long soak and an equivalent
spark-0060 run agree.** What this result does justify is *scheduling* that work instead of treating
multistream as permanently forbidden.

---

## 13. Upstream ABI breaks in 2.58.4 — checked against this fleet, 2026-09-10

Upstream issue [#15617](https://github.com/IntelRealSense/librealsense/issues/15617) reports that
2.58.4 breaks ABI against 2.58.3 despite being a patch release. Both breaks were checked against
what this fleet actually uses, rather than assumed harmless:

| Break | Blast radius here |
|---|---|
| `rs2_software_sensor_add_inference_stream{,_ex}` removed | **None** — no reference in vigil-spark or vigil-utils |
| `RS2_EXTENSION_OBJECT_DETECTION_SENSOR` removed **from the middle** of `rs2_extension`, shifting every later value | **Negligible** — it sat at position **69 of 71**, so only `RS2_EXTENSION_PERCEPTION_PROFILE` and `RS2_EXTENSION_COUNT` shift. Both are D555/perception features; the fleet runs D435 and references neither. |

This is a near miss rather than a non-issue: a mid-enum removal silently changes integer values for
anything compiled against 2.58.3 headers that then loads a 2.58.4 `.so`. **Any A7 re-pin must
rebuild every consumer against the same headers**, not just repoint the `.so` — in particular
`pyrealsense2` and any ROS 2 node binary, which are separately compiled artifacts.


---

## 14. Per-host build environment — the two Sparks are NOT interchangeable

Discovered 2026-09-10 while staging the canonical 2.58.4 build on both hosts. **The correct
`CUDA_HOME` differs per host, and the difference is inverted from what the hostnames suggest.**

| Host | `/usr/local/cuda` → | Prebuilt CUDA OpenCV compiled against | **Pin `CUDA_HOME` to** |
|---|---|---|---|
| `spark-3066` | 13.2 | **13.0** | `/usr/local/cuda-13.0` |
| `spark-0060` | 13.2 | **13.2** | `/usr/local/cuda-13.2` |

Getting it wrong is a hard configure failure, not a silent one:

```
CMake Error at /opt/gb10-cuda/install/opencv/lib/cmake/opencv4/OpenCVConfig.cmake:111 (message):
  OpenCV static library was compiled with CUDA 13.2 support.  Please, use the
  same version or rebuild OpenCV with CUDA 13.0
Call Stack (most recent call first):
  wrappers/opencv/CMakeLists.txt:5 (find_package)
```

So **do not copy a working build invocation from one Spark to the other.** Read the host's own
OpenCV before choosing the pin. Both hosts otherwise match: kernel `6.17.0-1029-nvidia`, BIOS
`5.36_0ACUM018`, and both carry `/usr/local/cuda-13.0` and `-13.2` side by side.

Also set `CUDACXX` explicitly on both: `nvcc` is **not** on a non-interactive SSH `PATH`, which
surfaces as the misleading "No CMAKE_CUDA_COMPILER could be found".


### 14.1 Both Sparks staged — 2026-09-10, neither re-pinned

The canonical prefix `/opt/vigil/opt/librealsense-v2.58.4-dgx-spark-gb10` is built from merged
master (`164f19674`, `v2.58.2-1474-g164f19674`) and gated on **both** hosts:

| Gate | spark-3066 | spark-0060 |
|---|---|---|
| `CUDA_HOME` used | `/usr/local/cuda-13.0` | `/usr/local/cuda-13.2` |
| `rs-gb10-profiler --self-test` | **32/0** | **32/0** |
| `BUILD_WITH_CUDA_ZEROCOPY` in provenance | 1 | 1 |
| 10 tools × 3 runs | rc=000 | rc=000 |
| duplicated globals exported | 0 | 0 |
| CUDA objects compiled | 5 | 5 |
| `/usr/local/lib/librealsense2.so` | **unchanged, 2.58.1** | **unchanged, 2.58.1** |

**Nothing is re-pinned.** A7 remains gated on rebuilding every consumer against the 2.58.4 headers
(§13) and on the envelope question (§12).

Two notes on spark-0060 specifically:

- Its build log contains two `RS2_USB_STATUS_BUSY` errors from the script's `validate()` step. The
  D435 **is** attached (`Bus 002 Device 003: ID 8086:0b07`) but is **held by another process** —
  the live `vigil_c2` session. This is expected on 0060 and is not a build defect; the artifact
  gates clean.
- Staging 0060's *build* is **not** validating 0060's *envelope*. Its camera sits behind a hub
  rather than on a native root port, and the multistream ramp (§12) has only ever run on 3066. That
  remains the open item.

**asuspro13 is the third pin target and was deliberately NOT staged.** `vigil-spark` pins this
compiled code on the ASUS host as well as the Sparks, so its current state belongs in this table
even though nothing was changed there:

| | asuspro13 |
|---|---|
| resolved `librealsense2.so` | `/opt/vigil/librealsense/lib/librealsense2.so.2.58` (via `ldconfig`) |
| `/usr/local/lib/librealsense2.so` | absent — the ASUS host does **not** use that path |
| `/opt/vigil/opt/librealsense-asus-system` | present but holds only `lib/python3.12/site-packages` |
| 2.58.4 artifact staged | **no** |

It is not staged because `scripts/build-dgx-spark-gb10.sh` is GB10-specific (CUDA arch, the
`/opt/gb10-cuda` OpenCV, the aarch64 pins) and does not apply to an x86_64 host with no CUDA — a
2.58.4 build for asuspro13 needs its own recipe. Note the pin path differs from the Sparks'
(`/opt/vigil/librealsense`, not `/opt/vigil/opt/librealsense-v2.58.1-…`), so any fleet-wide re-pin
script that assumes one layout will miss this host. asuspro13 and dtm-p1gen7 remain future work.

---

## 15. 720p multistream — measured on BOTH Sparks, 2026-09-10

Run under explicit direction to remove the single-high-rate-stream envelope and compare the two
Sparks against each other. Harness: `scripts/gb10/rs-gb10-stress-matrix.py`, new 720p tier, kernel
tripwire armed, headless, SDK = the canonical `librealsense-v2.58.4-dgx-spark-gb10` prefix.

### 15.1 The hardware ceiling at 720p is 30 Hz

A D435 advertises 1280×720 at **30/15/6 Hz only** — on depth, colour **and** infrared. There is no
faster 720p mode on this SKU, so "720p at the highest frame rate possible" is 1280×720@30 and no
host-side change can raise it. `960×540 @ 60 Hz` is the only >30 fps 16:9 colour mode; `848×480` gets
60 Hz colour and 90 Hz depth/IR; `848×100` and `256×144` depth reach 300 Hz.

What host-side work *can* do is sustain **all four streams** at 720p30 at once. That is a heavier
wire load than `HEAVY_60fps_848x480_D+C+IR`, the configuration that killed the GB10 xHCI on
2026-06-02, so it is the right thing to soak.

### 15.2 Sweep result — 7/7 on both hosts

20 s per entry. Every stream on every entry delivered 29.95–30.00 fps against a 30 fps request.

| Entry | spark-3066 (`6-1`, `NVDA8000:02`) | spark-0060 (`2-1.1`, `NVDA8000:00`) |
|---|---|---|
| `30fps_1280x720_depth` | PASS 29.96, g=0 | PASS 29.95, g=1 |
| `30fps_1280x720_color` (bgr8) | PASS 29.98, g=0 | PASS 29.98, g=0 |
| `30fps_1280x720_color_yuyv` | PASS 29.95, g=0 | PASS 29.98, g=0 |
| `30fps_1280x720_D+C` | PASS 29.98/29.98, g=0 | PASS 29.98/29.98, g=0 |
| `30fps_1280x720_D+IR` | PASS 29.98/29.98, g=0 | PASS 29.98/29.98, g=0 † |
| `30fps_1280x720_D+C+IR` | PASS ×3 @ 29.98, g=0 | PASS ×3 @ 29.97, g=0 † |
| **`HEAVY_…D+C+IR1+IR2`** | **PASS ×4 @ 29.98, g=0** | **PASS ×4 @ 30.00, g=0** |

Kernel danger signatures (`Host halt`, `HC died`, `not responding`, `Not enough bandwidth`,
`controller not`, `over-current`) across every run on both hosts: **0**. The only USB lines emitted
were the benign `981ae2 Region of Interest Auto Ctrls` UVC non-compliance notice, which the harness
already excludes by name.

† **These two entries first reported PASS with an empty stream list** — the pipeline started, nothing
raised, and *no frame arrived*. That was a false green in the harness itself, not a camera result:
`ok` was seeded as `(err == "")` and then only ever narrowed inside the per-stream loop, which never
ran when no stream delivered. Fixed in `eb0120a05` (zero streams → FAIL; fewer streams than
requested → FAIL, naming which arrived), and the two entries were re-run under the fixed harness to
produce the numbers above. The cause of the original zeros was transient: it was the first camera
open after an 8.5-hour `vigil_c2` session released the device. **Any earlier ramp result in this
document that shows a passing entry should be re-read with that defect in mind** — the 2026-09-10
R6 ramp (§12) rows were re-checked and all carry real frame counts, so none of them relied on it.

### 15.3 Colour format: YUYV saves host work, not bandwidth

The obvious reading — "request YUYV instead of RGB8/BGR8 and save a third of the USB bandwidth" — is
**wrong on D400**, and the source settles it:

- `src/ds/d400/d400-color.cpp:25-26` maps the UVC fourccs `YUY2`/`YUYV` to `RS2_FORMAT_YUYV`. That is
  what the device advertises on the wire.
- `src/ds/d400/d400-color.cpp:342` registers a **processing block**,
  `create_pbf_vector<yuy2_converter>(RS2_FORMAT_YUYV, map_supported_color_formats(RS2_FORMAT_YUYV), …)`.
- `src/device.cpp:255` defines those targets: `{ RGB8, RGBA8, BGR8, BGRA8 }`.

So RGB8, BGR8, RGBA8 and BGRA8 at 1280×720 are **host-side conversions from YUYV**; the wire carries
YUY2 at 2 bytes/pixel regardless of what the application asks for. Requesting `yuyv` therefore saves
**zero USB bandwidth** and instead skips the `yuy2_converter` pass. That is still worth having when
the consumer can take YUYV directly, but it must not be sold as a bandwidth saving.

Measured cost of the conversion at 720p30, single colour stream: bgr8 29.98 fps vs yuyv 29.95 fps on
3066 and 29.98 vs 29.98 on 0060 — i.e. **at 30 Hz the conversion is not the bottleneck and does not
show up in delivered frame rate at all.**

**And on this platform, asking for YUYV is a pessimisation rather than a saving.** The conversion
being "skipped" runs on the **GPU**: `src/proc/color-formats-converter.cpp:63-69` wraps the YUY2
unpack in `#ifdef RS2_USE_CUDA` and returns early into `rscuda::unpack_yuy2_cuda<FORMAT>` whenever
`rs2_is_cuda_available()` — which also makes the NEON implementations at `:232-240` dead code in
every CUDA-enabled GB10 build. Meanwhile vigil-spark's node, having negotiated `yuyv`, converts it
with `cv2.cvtColor(image, cv2.COLOR_YUV2BGR_YUYV)` on a single Grace core
(`realsensenode.py:859-861`). So the fallback trades a GPU conversion for a CPU one and saves nothing
on the wire.

Request `yuyv` only when a downstream **GPU** consumer takes YUYV directly. Otherwise `bgr8` is the
right request on GB10, and a fallback to `yuyv` should be logged as a fault rather than accepted as
a tuning outcome. Credit for catching this: it inverts the conclusion an earlier draft of this
section reached from the wire-format fact alone, which was correct about the wire and wrong about
the consequence because it never asked *where* the skipped conversion would have run.

### 15.4 The two hosts are not topologically identical

| | spark-3066 | spark-0060 |
|---|---|---|
| sysfs path | `6-1` | `2-1.1` |
| upstream of the camera | root port directly | `2109:0211` VIA Labs "USB3.0 Hub", 1 downstream port |
| xHCI controller | `NVDA8000:02` (bus 6) | `NVDA8000:00` (bus 2) |
| root port / negotiated rate | 20 Gbps / **5 Gbps SuperSpeed** | 20 Gbps / **5 Gbps SuperSpeed** |
| camera s/n, firmware | 347622075921, 5.17.3.10 | 327122076391, 5.17.3.10 |

The account owner states 0060's camera is on the spark-bus rather than behind a hub; the sysfs chain
does show a VL2109 between the root port and the camera. Both readings are recorded without
adjudicating whether that hub sits on the mainboard, in a captive cable, or in an adapter — nothing
was opened or traced physically. **What matters for these results is that both links negotiate the
same 5 Gbps SuperSpeed rate, so the bandwidth ceiling is identical, and that the two cameras hang
off different xHCI controller instances** — which is the variable a controller-death defect actually
turns on, and the one this two-host comparison controls for.

Earlier notes in this repo that treated 0060 as "behind a hub, therefore unvalidated" over-weighted
the hub and under-weighted the controller instance.


### 15.5 Soak result — the envelope is retired

Both hosts, 4 concurrent 1280×720@30 streams (Z16 depth + colour + IR1 + IR2), each pass a fresh
pipeline open/close so that start/stop cycling — which preceded **every** observed GB10 controller
death — is exercised, not just steady streaming.

| | spark-3066 | spark-0060 |
|---|---|---|
| passes × duration | **40 × 120 s** (~80 min streaming, 84 min wall) | 11 × 120 s (~22 min) |
| pipeline open/close cycles | **40** | 11 |
| frames delivered | **575,696** | 158,312 |
| min frames per stream per pass | **3598** of 3600 | **3598** of 3600 |
| **dropped frames (gaps), all streams, all passes** | **0** | **0** |
| kernel USB danger signatures | **0** | **0** |
| exit | `DONE_RC=0` | `DONE_RC=0` |

**Combined: 734,008 frames, zero dropped, zero xHCI faults, across two different xHCI controller
instances.**

**Acceptance was by frame count, not by the harness's PASS flag** — deliberately. The spark-3066 run
was started before `eb0120a05` landed, so it executed the harness *with* the false-green defect
(§15.2 †), under which a zero-frame pass prints `[PASS]`. Every pass in both JSON results was
therefore re-checked for four populated streams with ~3600 frames each. All 51 passes satisfy that.
Quoting `summary.passed` from the 3066 run would not have been evidence of anything.

#### Verdict: the single-high-rate-stream envelope is retired

The three conditions this repository set for lifting it are met:

1. **A real soak** — 80 minutes, not the ~12 that was previously called insufficient.
2. **An equivalent run on spark-0060** — done, on a different xHCI controller instance.
3. **Zero faults under a load heavier than the June killer** — 720p30 ×4 moves more bytes per second
   than `HEAVY_60fps_848x480_D+C+IR`, the configuration that killed the controller on 2026-06-02.

The June defect was real and is preserved in `docs/gb10/FINDINGS-2026-06-03.md` as history. What
changed since is at minimum the kernel (6.17.0-1021 → 6.17.0-1029-nvidia), the camera firmware
(5.13.0.55 → 5.17.3.10), the SDK, and the USB topology. This result does **not** identify which of
those fixed it, and does not claim the silicon defect is gone — it establishes that the configuration
the envelope forbade now runs clean for 80 minutes across 40 restarts on both hosts, which is the
question the envelope was actually blocking.

**What replaces it:** the kernel tripwire stays armed in every harness run, and
`RS2_GB10_USB_TUNING` stays available. Guidance changes from "never run multistream" to "multistream
at 720p30 is validated on both Sparks; run the tripwire on any new configuration before trusting it."

---

## 16. OpenCV unification and the retirement of the per-host CUDA pin — 2026-09-10

§14 documented that the two Sparks need different `CUDA_HOME` values and called it a trap to be
memorised. That was treating a symptom. This section records the cause and its removal.

### 16.1 The cause

`wrappers/opencv/CMakeLists.txt:5` hard-fails when the SDK's CUDA version does not match the CUDA
that the OpenCV it links was built against. The OpenCV the GB10 build pointed at,
`/opt/gb10-cuda/install/opencv`, was compiled against **CUDA 13.0 on spark-3066 and CUDA 13.2 on
spark-0060**. Hence the inverted pin — it was never about the hosts, only about which toolkit
happened to be used the day each OpenCV was built.

That inversion was not the only one. Building a single OpenCV across both hosts surfaced three more
asymmetries, none of which was visible from reading any script:

| Asymmetry | spark-3066 | spark-0060 | Effect |
|---|---|---|---|
| Video Codec SDK headers (`nvcuvid.h`, `cuviddec.h`, `nvEncodeAPI.h`) | under `cuda-13.0` only | under `cuda-13.2` only | `cudacodec` builds on one host, not the other |
| Qt | Qt5 only | Qt5 **and** a Qt6 missing `Core5Compat` | OpenCV prefers Qt6 → configure dies on 0060 |
| `/opt/vigil-spark/.venv-ros-py312` CPython | **3.12.3** | **3.12.13** | `FindPythonLibs` needs an *exact* match vs system 3.12.3 → `cv2` built on one host, silently absent on the other |

The headers were byte-identical across hosts (md5), so both CUDA trees on both hosts were
cross-populated. Qt is now pinned to major version 5. The interpreter roles were split: `BUILD_PY`
(system, matches libpython by construction) satisfies `FindPythonLibs`, while `VENV_PY` stays the
runtime target supplying numpy 2 headers and the verification.

### 16.2 What the OpenCV was missing

Measured in the artifact, not read from the script. The build that had been in service:

| Feature | Before | After | Hardware supports it? |
|---|---|---|---|
| CUDA | YES (13.2) | YES (13.2) | — |
| **cuDNN** | absent | **9.25.0** | `libcudnn.so.9` installed |
| **OpenGL** | absent | **YES** | `gl.pc` present |
| **NVCUVID / NVCUVENC** | absent | **YES** | headers present |
| **`cudacodec` H.264 encoder** | **throws from `cuda_stubs.hpp`** | **works** | NVENC present |
| Parallel framework | pthreads | **TBB 2021.11** | `tbb.pc` present |
| modules / CUDA modules | — | 70 / 11 | — |

`hasattr(cv2, "cudacodec")` returns **True in both columns** — OpenCV compiles a stub when it cannot
find the codec SDK. Only instantiating an encoder distinguishes them:

```
before:  createVideoWriter(H264) -> raises from core/private/cuda_stubs.hpp
after :  createVideoWriter(H264) -> OK, writes frames
```

### 16.3 The pin is retired — the test that shows it

Previously `CUDA_HOME=/usr/local/cuda-13.2` configured on 0060 and failed on 3066. With both hosts
pointed at the single `/opt/opencv-cuda-4.14.0`, the identical command now succeeds on both:

| | spark-3066 | spark-0060 |
|---|---|---|
| `CUDA_HOME` | `/usr/local/cuda-13.2` | `/usr/local/cuda-13.2` |
| configure | **OK** | **OK** |
| `OpenCV_DIR` | `/opt/opencv-cuda-4.14.0/lib/cmake/opencv4` | same |
| `CMAKE_CXX_FLAGS_RELEASE` | `-O3 -DNDEBUG -march=armv9.2-a+sve2+bf16+i8mm -mtune=neoverse-v2 …` | same |

**§14's per-host `CUDA_HOME` guidance is superseded.** Use `/usr/local/cuda-13.2` on both. The §14
table stays as the record of why the inversion existed.

### 16.4 Honest performance note

The new OpenCV is **not measurably faster than the old one on ordinary CPU calls** — 720p `imencode`
q80 is 1.767 ms vs 1.790 ms, which is noise. What changed is capability and the elimination of a
silent fallback:

| | `imencode` 720p q80 | CUDA | working `cudacodec` |
|---|---|---|---|
| new canonical build | 1.767 ms | yes | **yes** |
| previous build | 1.790 ms | yes | no (stub) |
| Ubuntu system 4.6.0 | **2.252 ms** (+27%) | **no** | no |

The system 4.6.0 is what a bare `python3` on a Spark resolves, and it has no CUDA at all. That is the
real gap this closes.

---

## 17. Armv9.2 codegen — real, byte-identical, and much smaller than expected

§F6 of the campaign plan found that both the incumbent and replacement SDK were compiled at the
**aarch64 baseline**, because GCC 13.3 does not know Cortex-X925 and `-mcpu=native` on an
unrecognised part resolves to nothing at all. This section measures what fixing that is worth.

### 17.1 The flags demonstrably take effect

| Build | `CMAKE_CXX_FLAGS_RELEASE` | SVE instructions in `librealsense2.so` |
|---|---|---|
| baseline | `-O3 -DNDEBUG -mcpu=native …` | **0** |
| Armv9.2 | `-O3 -DNDEBUG -march=armv9.2-a+sve2+bf16+i8mm -mtune=neoverse-v2 …` | **1739** |

Counted with `objdump -d` over `ptrue`/`whilelt`/`ld1[bhwd]`/`st1[bhwd]`. This is the objective
confirmation that the baseline really was shipping without SVE2 and that the new flags are not
merely accepted but used.

### 17.2 Correctness is unaffected

- `rs-gb10-profiler --self-test`: **checks=32 failures=0**
- `rs-enumerate-devices` / `rs-fw-update` / `rs-record` / `rs-benchmark`: all rc=0
- **Byte-identity**: the 164-frame `.db3` aligned-depth SHA-256 is `2bf7df33fea0d19c…` under the
  Armv9.2 build — **identical** to the baseline and to every configuration in §9. Different
  instructions, same bits.

### 17.3 The measurement — reproducible, and modest

SDK CPU post-processing over the recorded `.db3`, best-of-5 per filter, three independent runs.
Align, pointcloud, colorize and the YUY2 unpack are all **CUDA-gated on GB10**, so the CPU filter
chain is the only place these flags can show up, and it is what is measured here.

| Filter | baseline (ms/frame) | Armv9.2 (ms/frame) | change |
|---|---:|---:|---:|
| `spatial` | 8.8222 / 8.8272 / 8.8291 | 8.7745 / 8.7757 / 8.7786 | **−0.55%** |
| `hole_filling` | 0.4223 / 0.4225 / 0.4214 | 0.3520 / 0.3516 / 0.3561 | **−16.6%** |
| `decimation` | 0.4529 | 0.4547 | none |
| `temporal` | 0.5017 | 0.5018 | none |
| `disparity_transform` | 0.0091 | 0.0091 | none |

Run-to-run spread is ~0.1%, far smaller than either delta, so both effects are real rather than
sampling noise. But note which one is which: **`hole_filling` gains 16.6% while `spatial`, which
dominates the chain at ~8.8 ms, gains 0.55%.** On a realistic chain the net is under 1%.

### 17.4 Verdict — adopt, but for the right reason

This is **not** the large acceleration the baseline-ISA finding suggested it might be. The reason is
structural: on GB10 the SDK's heavy per-frame work (align, pointcloud, colorize, YUY2 unpack) already
runs on the GPU, and the CPU filters that remain are mostly memory-bound rather than compute-bound,
so wider vectors have little to bite on.

Adopt it anyway, because the cost is zero and the alternative is worse than slow — it is *undefined*:

- output is byte-identical, so nothing downstream can observe the change;
- the self-test and every tool are clean;
- the gains, though small, are real and reproducible;
- and the status quo is a build whose ISA is whatever GCC silently fell back to, which is not a
  decision anyone made. `scripts/build-dgx-spark-gb10.sh` now refuses that configuration outright
  (`0e226644a`) rather than shipping it unnoticed.

**Anyone hoping for a large win from arch flags on this SDK should read §17.3 first.** The lever that
would actually matter is moving more work onto the GPU — the NVENC colour path in F10 — not
recompiling the CPU paths.
