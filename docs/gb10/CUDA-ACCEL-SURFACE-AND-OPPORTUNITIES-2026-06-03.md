# librealsense acceleration surface on GB10 — rs.align dependents, the CUDA/GPU dependency map, and graded opportunities (2026-06-03)

> **Historical snapshot (2026-06-03).** Source-analysis and dependency map remain current. Three
> opportunities have since been resolved: **Opportunity 1** (async pipelining / double-buffer) measured
> and **NO-GO** for single-camera real-time (see [p4-async-pipelining.md](p4-async-pipelining.md));
> **Opportunity 2** (keep-on-GPU GL chain) measured — 1–7 ms/frame saving at 720p+
> (see [ROS2-GL-PINNED-FINDINGS-2026-06-05.md](ROS2-GL-PINNED-FINDINGS-2026-06-05.md)); **NVENC cq sweep
> done** — cq=23/p4 deployed (see [nvenc-cq-sweep.md](nvenc-cq-sweep.md)). Opportunity 3 (NEON filters)
> still open.

Source review of the David-Martel fork (`master`), grounded in greps cited inline. Answers three
questions: (1) what depends on `rs.align`; (2) does `rs.colorizer` (or anything else) have CUDA or
CUDA-*linked* dependencies — NVENC/Video-Codec-SDK, cuDNN, DNNL, NPP, TensorRT, nvJPEG; (3) where
more ops could benefit from GPU or CPU-SIMD acceleration. Companion to
[`HIL-SOAK-AND-ACCEL-2026-06-03.md`](HIL-SOAK-AND-ACCEL-2026-06-03.md) §4 (the measured CUDA-per-op data).

---

## 1. Who depends on `rs.align`

`rs.align` is a **leaf processing block**, not an internal dependency of any other SDK component.
- Factory: `align::create_align(rs2_stream)` (`src/proc/align.cpp:28`) → public C API `rs2_create_align`
  (`src/rs.cpp:2936`, `src/realsense.def:303`). It builds the aligned output profile in
  `create_aligned_profile` (`align.cpp:178`). Nothing in `src/` consumes an aligned frame internally —
  align is terminal, produced for an application.
- **Consumers are applications/wrappers, not core code:**
  - Examples: `examples/align`, `examples/align-advanced`, `examples/align-gl`.
  - Wrappers: Python (`wrappers/python/examples/align-depth2color.py` + the `pyrealsense2` `align` binding),
    C# (`wrappers/csharp/Intel.RealSense/Processing/Align.cs`), MATLAB (`wrappers/matlab/align.m`).
  - Viewer/tools: `realsense-viewer` (via `common/`), the GL pipeline (`rs2_gl_create_align`, `src/gl/rs-gl.cpp:163`).
  - **External (the ones that matter here):** vigil-spark `realsensenode.py` (aligns depth→color every
    frame) and ROS `realsense2_camera` (`align_depth.enable`).
- **Implication:** changing align's *implementation* (which backend runs) is safe — no core caller
  depends on its timing or thread behavior; only the produced frame contract matters.

### `rs.align` has FIVE implementations (dispatch in `align.cpp:28-47`)
Runtime/compile dispatch order: **CUDA** (if `rs2_is_gpu_available()`) → **SSE** (`__SSSE3__`, x86) →
**NEON** (`__ARM_NEON && BUILD_WITH_NEON`, ARM) → **generic scalar** (which itself is `#pragma omp
parallel for`). Plus a separate **GL** implementation (`align_gl`, `src/gl/align-gl.cpp`).
On GB10 the live contenders are **CUDA vs NEON** — so the measured **15–19× CUDA win
([§4](HIL-SOAK-AND-ACCEL-2026-06-03.md)) is CUDA beating hand-written NEON+OpenMP**, not naive scalar.
That makes the "keep `BUILD_WITH_CUDA=ON` for vigil" conclusion stronger, not weaker.

---

## 2. Does `rs.colorizer` (or the SDK) use CUDA or CUDA-linked libraries?

**No CUDA-library dependency anywhere.** The entire CUDA footprint is **three hand-written kernel sets**:
| dir / file | op |
|---|---|
| `src/proc/cuda/cuda-align.cu` | align depth↔other |
| `src/cuda/cuda-pointcloud.cu` | deproject → vertices |
| `src/cuda/cuda-conversion.cu` | pixel-format conversion (YUYV/UYVY→RGB, Y8I/Y12I splits, etc.) |

- **CUDA Video Codec SDK (NVENC/NVDEC/`nvcuvid`), cuDNN, DNNL/oneDNN, NPP, TensorRT, nvJPEG, cuFFT:
  NONE referenced** anywhere in `CMake/` or `src/` (grep returned zero hits). There is **no DNN
  inference in the core SDK** at all (the `rs-dnn` example uses OpenCV `cv::dnn`, a separate optional
  dependency — not cuDNN/TensorRT).
- The only CUDA libraries *linked* are cuBLAS + cuSPARSE (`CMake/cuda_config.cmake:8`,
  `ALL_CUDA_LIBS … ${CUDA_cublas_LIBRARY} ${CUDA_cusparse_LIBRARY}`), but **no `cublas*`/`cusparse*`
  calls are found in the kernel sources** (`src/cuda/`, `src/proc/cuda/` use only `cudaMalloc`/
  `cudaMemcpy` + raw kernels). They are linked-but-unused-by-the-kernels — droppable link clutter, not
  functional dependencies.

### `rs.colorizer` specifically
- **No CUDA path** — `src/proc/colorizer.cpp` (340 lines) is pure scalar: zero hits for
  `RS2_USE_CUDA|__ARM_NEON|__SSSE3__|#pragma omp|parallel_for`.
- **But colorize DOES have a GPU path via OpenGL** — `src/gl/colorizer-gl.cpp` does the histogram +
  colormap-LUT in a GLSL **fragment shader** (`texture2D(cmSampler, …)`). So "colorize has no CUDA"
  ≠ "colorize has no GPU acceleration." (Scope: colorize is a *visualization* op — the viewer's path.
  A compute consumer that ingests raw depth, e.g. vigil, may not colorize at all; confirm per consumer
  before treating colorize accel as load-bearing.)

### The runtime CUDA gate is coarse — "a GPU exists," not "CUDA is faster here"
`rs2_is_gpu_available()` = `cudaGetDeviceCount() > 0`, cached (`third-party/rsutils/src/rsutilgpu.cpp:15`).
The **same gate** drives align, pointcloud, **and the per-frame format converters**
(`color-formats-converter.cpp:64`, `depth-formats-converter.cpp`, `pointcloud.cpp`, the Y8I/Y12I
splitters). So on GB10 (GPU present) **every gated op auto-selects CUDA whether or not CUDA helps that
op** — which is why pointcloud silently runs its measured-**slower** (0.57×) CUDA path in production.

---

## 3. The full per-op acceleration matrix on GB10

| op | CUDA | NEON | OpenMP | GL | measured on GB10 |
|---|:--:|:--:|:--:|:--:|---|
| **align** depth→color | ✓ (auto) | ✓ | ✓(scalar) | ✓ | **CUDA 15–19× > NEON** (good) |
| **pointcloud** | ✓ (auto) | ✓ | — | ✓ | **CUDA 0.57× < NEON** (bad, still auto-selected) |
| **format conversion** (YUYV→RGB …) | ✓ (auto) | ✓(`image-neon.cpp`) | — | ✓(`yuy2rgb-gl`) | **UNMEASURED — see Finding A** |
| **colorizer** | ✗ | ✗ | ✗ | ✓(`colorizer-gl`) | — |
| **post-proc filters** (spatial/temporal/hole-filling/decimation/disparity/threshold) | ✗ | ✗ | ✗ | ✗ | **none — pure scalar** |

---

## 4. Graded opportunities (ranked by leverage, honestly scoped)

An op is a *real* opportunity only if it is (a) on a live consumer's hot path **and** (b) costly per
frame. Ranked accordingly:

### Finding A — per-frame color conversion on CUDA — **MEASURED 2026-06-03: NOT a regression**
`color-formats-converter.cpp` gates YUYV/UYVY→RGB on the coarse `is_gpu_available()`, so in the CUDA
build the color decode runs CUDA on every color frame. The worry: by the D2H-bound logic that made
pointcloud 0.57×, this could be a silent per-frame regression on vigil's guaranteed hot path.
**Tested** (`just hil-gpu-pipeline --convert-only`, color-only single stream so align can't swamp the
signal, CPU-ms/frame from `/proc/self/stat`, 1280×720×30, both builds back-to-back, static scene):
**CUDA 2.00 ms/frame vs NEON 2.03 ms/frame — CPU-equivalent (±2%, within noise); both sustained 29.96/30 fps.**
→ **No regression.** The conversion is small enough that neither path is a bottleneck; the D2H penalty
does not manifest at this resolution/rate. (The earlier *full*-pipeline run showed CUDA 62% lower
CPU/frame — that is **entirely `align`**, not conversion: `color_access` p50 was identical 0.023 vs
0.024 ms on both builds, and the CPU delta equalled the measured align saving. The `--convert-only`
isolation is what prevents that mis-attribution.) **Caveat:** process-total CPU floors out sub-ms
differences; a multi-stream or much-higher-res color load is untested. Tools: `rs-gb10-gpu-pipeline.py`
+ `_pipeline_compare.py`.

### Opportunity 1 — **MEASURED 2026-06-03: the bottleneck was per-frame allocation, NOT the copy**
The CUDA pointcloud kernel does **per-frame `cudaMalloc`×3 + `cudaMemcpy` H2D/D2H + `cudaFree`×3**
(`cuda-pointcloud.cu`). I added a compile-gated (`RS2_GB10_PC_ZEROCOPY`, default OFF = upstream-identical)
runtime **attribution ladder** (`RS2_PC_MODE` 0/1/2) and ran it as `just hil-pc-zerocopy` (848×480, single
depth stream, **each rung correctness-checked vs a numpy CPU deproject — all `max_abs_diff = 0.0`**):

| rung (`RS2_PC_MODE`) | p50 ms (2 runs) | vs NEON | what it isolates |
|---|---|---|---|
| 0 baseline (shipped: malloc+memcpy+free each frame) | 1.05–1.09 | **0.5× (slower)** | reproduces the old 0.57× |
| **1 cached-device** (alloc once, reuse; still `cudaMemcpy`) | **0.32** | **~1.4–1.7× FASTER** | **allocation churn = the real cost** |
| 2 cached-managed (`cudaMallocManaged` once + plain memcpy + explicit sync) | 0.49–0.50 | ~equal | the copy, on coherent memory |
| NEON baseline | 0.38–0.54 (scene-dep.) | — | CPU reference |

**Conclusions (measured, not hypothesized):**
1. **The shipped CUDA pointcloud is needlessly ~3.3× slow because it allocates and frees device memory
   every frame** (≈180 `cudaMalloc`+`cudaFree`/sec — heavyweight synchronizing calls). **Caching the
   buffers alone (mode 1) makes CUDA pointcloud 3.3× faster than the shipped path and faster than NEON**,
   reversing the prior "CUDA is slower" finding. This is the fix.
2. **GB10 unified memory does NOT rescue this op the way the hypothesis guessed.** `cudaMallocManaged`
   (mode 2) is **slower than cached device buffers** (0.49 vs 0.32) — the `cudaMemcpy` over coherent RAM
   is cheap, and managed memory's fault/coherence + the explicit `cudaDeviceSynchronize` cost more than
   they save. **On GB10 the lever is eliminating allocation churn, not eliminating copies.** (Caveat:
   mode 2 still does two *plain* memcpys for I/O marshalling; a true pool-level zero-copy on the SDK's
   own frame buffers via cached `cudaHostRegister` is untested — but mode 1 already beats NEON, so the
   motivation is low.)

**Ship path:** mode 1 (cached-device) is the recommended GB10 setting. Source default stays mode 0 so
`build-gb10-full` is unchanged for other tools; promoting mode 1 to the default needs a multi-instance /
multi-thread check of the (mutex-guarded, grow-only) static buffer pool first. The same cached-buffer
pattern should be applied to `cuda-conversion.cu` and `cuda-align.cu` (align already wins, but caching
would widen the margin and cut its allocation overhead).

### Opportunity 2 (shipping but untested GPU path) — benchmark the GL pipeline; consider keep-on-GPU
The GL blocks (`colorizer-gl`, `pointcloud-gl`, `align-gl`, `yuy2rgb-gl`) **ship in the production build**
(`-DBUILD_GLSL_EXTENSIONS=ON`, `scripts/build-dgx-spark-gb10.sh:209`) but were **never in the HIL build**
— an entire GPU path in the shipping artifact that has **never been benchmarked** (opportunity *and* an
"untested code in production" flag). *Hypothesis (untested on GB10):* a GL-resident multi-op chain keeps
frames in GPU textures and avoids the per-op H2D/D2H that hurt CUDA pointcloud, so for a colorize→align
chain GL could beat both CUDA-with-copies and NEON. Needs a GL-vs-CUDA-vs-NEON bake-off to confirm; it is
also the *only* GPU path for **colorize**.

### Opportunity 3 (largest *unaccelerated* surface — but opt-in, consumer-dependent) — the post-processing filters
spatial (499 L), decimation (858 L), temporal (282 L), hole-filling (103 L), disparity-transform (120 L),
threshold (81 L) are **pure scalar — zero CUDA / NEON / SSE / OpenMP / GL**. This is the largest
unaccelerated code surface, *but* these filters are **opt-in** (a consumer must enable them), so impact
is consumer-specific, not universal. Cheapest credible win: **NEON + `#pragma omp parallel for`** on
spatial/temporal/hole-filling (cross-platform, no GPU copy, follows the existing `neon-align.cpp`
pattern). Justified only after confirming a live consumer (vigil/ROS) actually enables them — measure
the consumer's filter config before investing.

### Not applicable to the core SDK
- **NVENC / CUDA Video Codec SDK** — librealsense recording (rosbag2) is uncompressed; there is no
  encode path to accelerate *in the SDK*. NVENC is a legitimate **application-layer** win (the
  `/opt/gb10-cuda` ffmpeg already has it) for vigil's capture/playback or `rs-convert`, not core.
- **cuDNN / DNNL / TensorRT** — no neural inference exists in the core SDK; nothing for these to accelerate.

---

## 5. Concrete next actions (no firmware involved; current code/config held)
1. **Measure Finding A** — end-to-end color-throughput full-vs-nocuda (is per-frame CUDA YUYV→RGB a regression?).
2. **Prototype Opportunity 1** — `cudaMallocManaged`/zero-copy in `cuda-pointcloud.cu` + `cuda-conversion.cu`;
   re-bench pointcloud & conversion. Smallest diff, attacks the measured cause.
3. **Benchmark Opportunity 2** — add GL to a HIL build; GL-vs-CUDA-vs-NEON for align/colorize/pointcloud.
4. **Gate audit** — consider a per-op CUDA opt-out so pointcloud (measured slower) isn't force-selected by
   the coarse `is_gpu_available()` gate.
5. Filters (Opportunity 3) only after confirming a consumer enables them.

**Honest scope:** §1–3 are source-verified facts; §4 Finding A and Opportunities are *analysis* — the
ratios for conversion/GL/zero-copy are **not yet measured**. The one measured number remains align
(15–19× CUDA) from the companion doc.
