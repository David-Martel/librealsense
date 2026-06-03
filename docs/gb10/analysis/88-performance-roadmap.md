# 88. GB10 librealsense Performance Roadmap (STATIC review, measurement-gated)

**Status:** STATIC review only. The GB10 USB/xHCI controller is currently DEAD
(see `70-controller-crash-finding.md`); **no hardware benchmarking is possible
until reboot**. Every speedup below is a **hypothesis with a measurement plan**,
not a measured win. Per CLAUDE.md rule #4, **nothing here may be claimed as
"faster" until the before/after benchmark in its row has been run post-reboot.**

**Platform (verified this session, 2026-06-03):**

| Property | Value | How verified |
|---|---|---|
| GPU | NVIDIA GB10, driver 580.159.03, compute cap **12.1** | `nvidia-smi --query-gpu` |
| CUDA toolkit | **13.0** (V13.0.88), `/usr/local/cuda -> cuda-13.0` | `nvcc --version`, `ls -la /usr/local` |
| CPU | **Cortex-X925 + Cortex-A725** heterogeneous, MIDR part `0xd87`, 20 cores | `lscpu`, `/proc/cpuinfo` |
| Toolchain | GCC 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1) | `gcc --version` |
| Unified memory | C2C/ATS full coherence — **raw `malloc()` pointer usable in a kernel without registration: PASS** | microbench `/tmp/ats_test.cu`, this session |
| Caching | `sccache` present at `/usr/bin/sccache` | `which sccache` |
| Profilers | `perf`, `nsys`, `ncu` all present | `which` |

**Hard constraint (dominates every item):** Performance is **secondary to not
killing the xHCI controller.** Per `20-customization-critical-review.md` and
`80-multistream-root-cause-deepdive.md`, the controller dies under
concurrent-stream / control-transfer pressure. **Every item below is host/GPU-side
and touches ZERO USB control transfers and does NOT raise stream count or
framerate.** No item here justifies adding a concurrent stream, raising FPS, or
adding control transfers. The "USB-neutral" column makes this explicit per row.

---

## 0. Measurement methodology split (camera-dependence)

Two classes of validation. Do the camera-free ones *first* (they run now-ish on
any synthetic buffer and don't wait on the camera); the end-to-end ones are
**post-reboot, camera-attached only**.

**A. Camera-FREE (scriptable on synthetic frames — can run as soon as the box is
up, no D435 needed):**
- CUDA kernel micro-timing & copy elimination → `nsys profile` + `ncu`.
- Kernel occupancy / block-size question → `ncu --metrics sm__warps_active.avg.pct_of_peak_sustained_active` (a.k.a. achieved occupancy).
- ISA / `-march` A/B → compile a synthetic align+convert harness, `perf stat`.
- Build time / cache → wall-clock + `sccache --show-stats`.

**B. Camera-DEPENDENT (post-reboot, D435 authorized, USB3 link only):**
- End-to-end FPS, latency, frame-drops → `rs-gb10-profiler` (already built).
- Canonical command (from `realsense.TODO.md` Open Items):
  ```bash
  realsense-gb10-env rs-gb10-profiler --profile vga30 --cycles 1 --duration-sec 15 \
    --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250 --cooldown-ms 1000
  ```
- **Profiles to use for the two task-specified workloads:** `vga30`
  (640x480@30, VIGIL's validated steady state) and a 848x480@60-equivalent run.
  **Do NOT add streams or push to multi-stream `--profile all` for perf A/Bs** —
  keep to single tested profiles to respect the controller constraint.

**Golden rule for every A/B:** isolated prefix stays in place
(`/opt/vigil/opt/librealsense-…-gb10`) so the baseline `/usr/local` build is the
control. Change exactly one variable per build, record the build identity in the
result line.

---

## 1. P0 — Build correctness (these undermine *every* perf claim if wrong)

### 1.1 `CUDA_HOME` defaults to a nonexistent path — `/usr/local/cuda-13.2`

- **What + where:** `scripts/build-dgx-spark-gb10.sh:7`
  `CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.2}"`. That directory **does not
  exist** — actual toolkit is `/usr/local/cuda -> /etc/alternatives/cuda ->
  cuda-13.0`. The flag is passed as `-DCUDA_TOOLKIT_ROOT_DIR="$CUDA_HOME"`
  (line 145).
- **Why it matters / anomaly:** The TODO reports CUDA builds *succeeded* despite
  this. The likely explanation is CMake's CUDA language detection found `nvcc`
  on `PATH` (13.0) and compiled fine, while `CUDA_TOOLKIT_ROOT_DIR` pointed at a
  missing dir used only for *finding libs/headers/helper FindCUDA paths*. That is
  a **silent toolkit-inconsistency risk** (headers from one place, nvcc from
  another) and it poisons confidence in every CUDA benchmark below. It is also a
  perf-of-iteration bug: a stale/odd root dir can defeat caching and slow
  reconfigure.
- **Fix:** default to the symlink: `CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"`.
  Keep the env override.
- **Measurement (build-correctness, camera-free):**
  ```bash
  # After fix, reconfigure and confirm the toolkit nvcc and root agree:
  grep -i "CMAKE_CUDA_COMPILER:" <build>/CMakeCache.txt   # must be /usr/local/cuda/bin/nvcc
  grep -i "CUDA_TOOLKIT_ROOT_DIR" <build>/CMakeCache.txt  # must be /usr/local/cuda
  # And a runtime sanity: cuda runtime version the lib was built against
  strings /opt/vigil/opt/.../lib/librealsense2.so | grep -m1 -i "cuda" 
  ```
  **Metric:** both point at 13.0/`/usr/local/cuda`; no mismatch warning at
  configure.
- **Effort:** trivial (1 line). **Risk:** very low. **Type:** build-flag/config.

### 1.2 CUDA arch — keep `121`, but document the `120` fallback rationale

- **What + where:** `LRS_GB10_CUDA_ARCH=121` (line 8), passed as
  `-DCMAKE_CUDA_ARCHITECTURES`. nvcc 13.0 accepts `sm_121`; our microbench built
  with `-arch=sm_121`. **No change needed** — flagged only so the perf reviewer
  knows it is correct (compute cap 12.1 verified via `nvidia-smi`). If a future
  nvcc rejects 121, `120` is the documented fallback (TODO Open Items).
- **Measurement:** `nvcc -arch=sm_121 <trivial>.cu` already PASSES this session.
- **Effort:** none. **Risk:** none. **Type:** build-flag (no-op, documented).

---

## 2. P1 — CPU ISA: the single biggest grounded build-flag finding

### 2.1 `-mcpu=native` silently degrades to baseline `armv8-a`

- **What + where:** `scripts/build-dgx-spark-gb10.sh:14`
  `NATIVE_FLAGS="… -mcpu=native …"`, applied to **all C/C++ release flags**
  (lines 138–139), i.e. every CPU-side path: NEON align (`src/proc/neon`),
  format conversion fallbacks, temporal/spatial filters, rsutils.
- **Verified mechanism (this session, GCC 13.3):**
  - GCC 13.3 **does not know the Cortex-X925 MIDR (0xd87)**. `-mcpu=cortex-x925`
    → `error: unknown value`.
  - `-mcpu=native` therefore falls all the way back to **`.arch armv8-a`**
    (confirmed: `gcc -mcpu=native -S` emits `.arch armv8-a`). That is ARMv8.0
    baseline tuning.
  - For contrast, `-march=armv9.2-a -mtune=native` emits
    **`.arch armv9.2-a+crc`** and defines `__ARM_FEATURE_SVE2`,
    `__ARM_FEATURE_MATMUL_INT8`, `__ARM_FEATURE_SVE_BF16` (all verified via
    `gcc -dM -E`).
- **What this actually costs (precise — NOT "all SIMD is off", NOT "wrong
  tuning"):** NEON is mandatory in armv8-a, so the **hand-written NEON paths still
  compile and run today**. The **grounded** loss is the **feature set**: (a)
  **no SVE2** → the compiler cannot autovectorize the many scalar hot loops (e.g.
  per-pixel conversion and filter loops) to SVE2; (b) no armv9 baseline / i8mm /
  bf16. The headline is **missed SVE2 autovectorization**, not "SIMD disabled."
  **Scheduling is NOT a grounded loss:** GCC 13.3 has no Cortex-X925 cost model at
  all, so neither the current build nor the candidate gets X925-aware scheduling
  (verified below).
- **Recommended flag (the *feature set* is the certain win; the *tune* is an open
  A/B):** `-march=armv9.2-a` (both X925 and A725 are armv9.2-a, so the SVE2/i8mm
  feature set is safe on either cluster). For the tune, **`-mtune` is undecided,
  not pre-decided:**
  - Verified this session: `-mtune=native` resolves to an **empty/unnamed** cost
    model on GCC 13.3 (`gcc -mtune=native -Q --help=target` shows `-mtune=` blank,
    vs `-mtune=generic` which prints `generic`) — i.e. native ≈ generic here
    because GCC can't identify MIDR 0xd87. So `-mtune=native` buys **no real
    scheduling improvement.**
  - Therefore the tune choice is an **open A/B arm among {generic (≈native),
    neoverse-v2, grace}**, decided by `perf stat`, not asserted. A concrete
    out-of-order cost model (neoverse-v2) *could* schedule the OoO X925 better
    than generic — that is precisely what the measurement must settle. Do **not**
    pre-reject neoverse-v2/grace as "mis-tuning"; they are the OoO-cost-model arms.
  - `-mcpu=grace`/`-mcpu=neoverse-v2` (full mcpu) also set armv9 features and are
    valid combined arms; prefer `-march=armv9.2-a` + an explicit `-mtune` so the
    feature set is pinned independently of the tune experiment.
- **Mandatory safety check before shipping (heterogeneous-core SIGILL):** code
  compiled for armv9.2/SVE2 must not execute an unsupported instruction if a
  thread migrates between the X925 and A725 clusters. Both are armv9.2/SVE2 so it
  *should* be safe, but **verify, don't assume**:
  ```bash
  # Pin the realsense self-test / a conversion microbench to a big core and a little core:
  taskset -c 0  <armv9-built test binary>   # X925 cluster
  taskset -c 19 <armv9-built test binary>   # A725 cluster (adjust index per lscpu)
  dmesg | tail   # look for "illegal instruction" / SIGILL
  ```
  **Pass = no SIGILL on either cluster.** This is the discriminator that makes
  the flag change safe.
- **Measurement (camera-free A/B):**
  ```bash
  # Build a synthetic harness that runs the NEON align + YUY2->RGB8 conversion
  # over N synthetic 640x480 and 848x480 frames in a loop.
  # Build twice: (baseline) -mcpu=native  vs (candidate) -march=armv9.2-a -mtune=native
  perf stat -r 20 -e instructions,cycles,task-clock ./convert_align_harness
  ```
  **Metric:** wall `task-clock` ms/frame and instructions/frame, baseline vs
  candidate, 20 reps, report mean±stddev. Also `objdump -d` the conversion TU and
  confirm SVE (`z`-register) instructions appear in the candidate.
- **Also test (separate A/B arm):** a GCC-14 toolchain (which *may* know
  `cortex-x925`) → would let `-mcpu=native` work properly. Treat as an independent
  experiment; do not bundle with the `-march` change.
- **Effort:** low (flag change) + medium (write the harness). **Risk:** low–medium
  (gated by the SIGILL check). **Type:** build-flag (+ small code harness for
  measurement).

---

## 3. P1 — CUDA align: in VIGIL's hot path, copies + churn remain

VIGIL uses `rs.align(align_to)` (verified:
`…/UMichProjection/.../realsense.py:43`), so the **CUDA align path is on the
real product hot path** — this is the CUDA item to prioritize. The validated
640x480@30 run used SDK alignment.

### 3.1 align: device buffers cached, but H2D + D2H copies every frame

- **What + where:** `src/proc/cuda/cuda-align.cu`,
  `align_other_to_depth` / `align_depth_to_other`. Device buffers are already
  lazily cached (`if (!_d_depth_in) …`), which is good. But **every frame** still
  does: `cudaMemcpy(H2D depth)`, `cudaMemcpy(H2D other)` (color path),
  `cudaMemset`, kernels, then `cudaMemcpy(D2H aligned_out)`, plus a full
  `cudaStreamSynchronize(0)`. On 640x480: depth ~600KB + color ~900KB H2D and
  ~600–900KB D2H per frame, serialized around a default-stream sync.
- **Mechanism of speedup (hypothesis, three distinct options — measure each,
  some may regress):**
  1. **`cudaHostRegister` the SDK frame staging buffer** → H2D becomes a faster
     pinned-path or is elided. Lowest-risk; honors the TODO warning ("do not
     assume RealSense-owned frame memory can be registered" — so register a
     *bridge* staging copy, not the frame archive memory directly).
  2. **`cudaMallocManaged` for `_d_*` buffers** → pages migrate on demand; can
     remove the explicit copies. Medium risk.
  3. **ATS full-coherence: pass the host pointer straight to the kernel** (no
     copy, no registration). **Microbench PASS this session** — a raw `malloc`
     pointer worked in a kernel on this box. Highest payoff, but see the caveat.
- **Critical caveat (coherent ≠ always faster):** `kernel_other_to_depth` and
  `kernel_depth_to_other` do **scattered rectangle reads/writes** over the "other"
  image. Repeated *remote* reads over C2C can be **slower** than one bulk H2D
  copy followed by local-GPU-memory reads. So the per-kernel decision is
  empirical: the cheap, regular `kernel_map_depth_to_other` may benefit from
  zero-copy while the scattered transfer kernels may want to keep an explicit
  device copy. **This item can REGRESS if applied blindly.**
- **Measurement (camera-free first, then camera-confirmed):**
  ```bash
  # Camera-free: drive the align helpers with synthetic depth+color buffers, loop.
  nsys profile -t cuda,osrt -o align_baseline   ./align_harness   # current copy path
  nsys profile -t cuda,osrt -o align_zerocopy   ./align_harness   # candidate
  # Compare: cudaMemcpy time, kernel time, gaps. Also per-kernel:
  ncu --set full --kernel-name kernel_other_to_depth ./align_harness
  ```
  **Metrics:** (a) total `cudaMemcpy` ms/frame → 0 if zero-copy succeeds; (b)
  kernel ms/frame for the scattered kernels (must NOT increase); (c) end-to-end
  align ms/frame. **Decision rule:** adopt zero-copy per-kernel ONLY where total
  (copy+kernel) ms/frame drops vs baseline; otherwise keep the copy.
  - Camera-confirmed (post-reboot): `rs-gb10-profiler --profile vga30` with
    alignment, compare effective FPS and processing-time field vs baseline build.
- **Workload comparison the task asked for (640x480@30 vs 848x480@60):** 848x480
  is ~1.33× the pixels and 60Hz is 2× the rate → ~2.66× the per-second copy
  bytes. So the copy-elimination win, *if real*, should be **measurably larger at
  848x480@60**. Quantify by running the align_harness at both resolutions and
  reporting copy-bytes/sec eliminated and ms/frame at each. **Caveat:** do not run
  848@60 *on the camera* as a perf A/B unless the controller is confirmed stable —
  the camera-free harness covers the copy question without USB risk.
- **Effort:** medium (bridge + per-kernel guard). **Risk:** medium (can regress;
  gated by per-kernel nsys/ncu). **Type:** code change.

### 3.2 align kernel block size — 32×32 = 1024 threads/block (MEASURE, don't assume)

- **What + where:** `src/proc/cuda/cuda-align.cu:15`
  `#define RS2_CUDA_THREADS_PER_BLOCK 32`, used as a 2D block
  `dim3 threads(32,32)` = 1024 threads/block (the hardware max). This *may* cap
  occupancy or it may be fine — **do not assert it's bad without ncu.**
- **Measurement (camera-free):**
  ```bash
  ncu --metrics sm__warps_active.avg.pct_of_peak_sustained_active \
      --kernel-name kernel_map_depth_to_other ./align_harness    # baseline 32x32
  # then rebuild with 16x16 (256/block) and re-measure
  ```
  **Metric:** achieved occupancy % and kernel ms. Only change the block size if
  ncu shows occupancy-limited AND a smaller block raises it AND ms/frame drops.
- **Effort:** trivial. **Risk:** low. **Type:** code change (gated by ncu).

---

## 4. P2 — CUDA pointcloud: cleanest code win, but gate on consumer

### 4.1 pointcloud `deproject_depth_cuda` reallocates every call

- **What + where:** `src/cuda/cuda-pointcloud.cu:83-115`. **Every call** does
  three `cudaMalloc` + a full H2D of depth + H2D of intrinsics + kernel + D2H of
  points + three `cudaFree`. This is the worst CUDA pattern (allocation churn on
  the per-frame path) — unlike align, there is **no buffer caching here at all.**
- **Why P2 (gated):** VIGIL's app code uses `rs.align` but **no direct
  pointcloud/`calculate()` consumer was found** in `vigil-spark` (only inference
  downstream). The TODO's "upload once, keep on GPU" pipeline (§5) is the future
  consumer. So this is the *cleanest, lowest-risk* code win available, but its
  **priority is gated on pointcloud actually being on a product path.** If a
  pointcloud consumer is added, promote to P1.
- **Mechanism:** cache `dev_points`/`dev_depth`/`dev_intrin` like `cuda-align.cu`
  already does (lazy alloc + reuse) → removes 3 malloc + 3 free per frame; then
  optionally apply the same zero-copy/managed hypothesis as §3.1 (the deproject
  kernel is a *regular* per-pixel read → a good zero-copy candidate, unlike the
  scattered align kernels).
- **Measurement (camera-free):**
  ```bash
  nsys profile -t cuda -o pc_baseline ./pointcloud_harness   # current malloc/free path
  nsys profile -t cuda -o pc_cached   ./pointcloud_harness   # candidate
  ```
  **Metric:** per-frame `cudaMalloc`+`cudaFree` time → ~0; total deproject
  ms/frame. Report at 640x480 and 848x480.
- **Effort:** low (mirror align's caching). **Risk:** low. **Type:** code change.

---

## 5. P1/P2 — Upload-once, keep-on-GPU downstream pipeline

- **What + where:** TODO "Performance Work": *"upload each color/depth frame once,
  then keep resize, preprocessing, inference, pointcloud, and encoding on GPU."*
  The real downstream GPU consumer on this box is **SAM3 inference**
  (`vigil_ros_ws/sam3/…`, Torch/Triton/CUDA), not librealsense itself.
- **Mechanism:** today the SDK copies frames to host; a Torch/inference consumer
  then re-uploads to GPU → **two crossings per frame.** With GB10 ATS coherence
  (microbench PASS), a custom C++/Python bridge can hand the inference stage a
  device-resident or coherent host buffer **once**, eliminating the re-upload and
  the host bounce for display-vs-compute split (TODO: "Publish RGB + compressed
  depth preview for UI; reserve raw Z16 for compute").
- **This is mostly *outside* librealsense** (it's a VIGIL bridge), so it is P1
  for VIGIL throughput but P2 for *this SDK roadmap*. Flagged for completeness.
- **Measurement (camera-free, then end-to-end):**
  ```bash
  # Count host<->device crossings per frame with nsys on the VIGIL capture+infer loop
  nsys profile -t cuda,osrt -o vigil_pipeline_baseline <vigil capture+sam3 stub>
  # Metric: cudaMemcpy count & bytes per frame (target: depth/color uploaded once),
  # and end-to-end frame latency (capture -> inference-ready).
  ```
  **Metric:** memcpy crossings/frame (target ≤1 per modality) and capture→infer
  latency ms. **USB-neutral:** bridge work is post-capture; no stream/control
  change.
- **Effort:** high. **Risk:** medium. **Type:** code change (VIGIL bridge,
  optional small SDK accessor to expose a coherent frame pointer).

---

## 6. P2 — CUDA-enabled OpenCV under `/opt/vigil/opt/opencv-cuda`

- **What + where:** TODO Open Item — `/opt/vigil/opt/opencv-cuda` is **ABSENT**
  (verified this session); system `cv2` exposed no usable CUDA device. VIGIL
  preview uses CPU `cv2`.
- **Is it worth it?** Only if profiling shows `cv2` resize/cvtColor/preview is a
  real CPU cost in the VIGIL loop. **Do not build it speculatively** — a
  CUDA-OpenCV build is large and adds maintenance surface. Worth it *iff* the
  measurement below shows preview CPU is a bottleneck.
- **Measurement (decide first, camera-free where possible):**
  ```bash
  # 1) Establish the cost to beat (system cv2, CPU):
  python -c "import cv2,time,numpy as np; a=np.zeros((480,640,3),np.uint8); \
    t=time.perf_counter(); [cv2.resize(a,(320,240)) for _ in range(1000)]; \
    print('cpu resize ms/op', (time.perf_counter()-t))"
  # repeat for cvtColor BGR<->RGB, and the actual VIGIL preview op-mix.
  # 2) Only if that is a non-trivial fraction of frame budget, build opencv-cuda and
  #    re-time cv2.cuda equivalents incl. the H2D/D2H (which on GB10 may be cheap via ATS).
  ```
  **Metric:** ms/op CPU vs `cv2.cuda` *including* upload/download. **Decision
  rule:** build only if cuda path (with transfer) beats CPU by a margin that
  matters at frame rate. **Note:** on GB10 the upload that usually kills small
  `cv2.cuda` ops may be cheap via ATS — so this is genuinely worth *measuring*,
  not dismissing.
- **Effort:** high (build OpenCV-CUDA). **Risk:** low to the SDK (separate
  prefix). **Type:** config/build of a separate component.

---

## 7. P2 — LTO/IPO: currently OFF, provide the A/B protocol to decide

- **What + where:** `LRS_GB10_WITH_IPO=OFF` (line 19) →
  `-DCMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE=OFF`. TODO: keep OFF until a clean
  A/B; pybind/CUDA links are LTO-sensitive.
- **Mechanism (hypothesis):** LTO can inline across TUs in the hot conversion /
  filter / rsutils paths → fewer calls, better scheduling. **But** it lengthens
  link time, can interact badly with the CUDA device-link step and the pybind11
  module, and historically has produced *no measured win here.*
- **A/B protocol (camera-free for the perf number, camera for end-to-end):**
  ```bash
  # Arm A: WITH_IPO=OFF (baseline).  Arm B: WITH_IPO=ON.
  # 1) Build correctness gate (B must link cleanly — pybind + CUDA device link):
  cmake --build <build> 2>&1 | tee lto_build.log   # zero link errors, note link time
  # 2) Perf: run the convert+align synthetic harness under perf stat (as in §2.1),
  #    20 reps, mean±stddev, A vs B.  Also nsys for CUDA-side unchanged.
  # 3) Size/iteration cost: record incremental rebuild time and .so size A vs B.
  ```
  **Decision rule:** enable LTO ONLY if (a) it links cleanly incl. pybind/CUDA,
  AND (b) the harness shows a statistically clear ms/frame improvement (non-
  overlapping mean±stddev), AND (c) the rebuild-time penalty is acceptable for the
  dev loop. Default stays OFF until all three hold.
- **Effort:** low (flag) + medium (harness, shared with §2.1). **Risk:** medium
  (link fragility). **Type:** build-flag.

---

## 8. P2 — sccache effectiveness for CUDA, and async-logging cost

### 8.1 Verify sccache actually caches `.cu`/nvcc units

- **What + where:** build script wires
  `-DCMAKE_CUDA_COMPILER_LAUNCHER=sccache` (line 125). **nvcc caching under
  sccache is frequently a silent no-op.** Unverified.
- **Measurement (camera-free):**
  ```bash
  sccache --zero-stats
  # clean reconfigure+build, then an identical no-op rebuild:
  sccache --show-stats   # look at "Cache hits" vs "Cache misses", and specifically
                         # whether CUDA compilations are counted as cacheable or
                         # "non-cacheable calls" (nvcc often lands here)
  ```
  **Metric:** cache-hit rate on the 2nd build; count of "non-cacheable" nvcc
  calls. **If nvcc is non-cacheable**, either accept (C++ TUs still cache) or test
  `-DCMAKE_CUDA_COMPILER_LAUNCHER` removal to avoid wrapper overhead with no
  benefit. This is a **perf-of-iteration** item, not runtime.
- **Effort:** trivial. **Risk:** none. **Type:** build-config measurement.

### 8.2 Async EasyLogging++ — confirm it's a win, not a cost

- **What + where:** `-DENABLE_EASYLOGGINGPP_ASYNC=ON` (line 168). Async logging
  moves log I/O off the frame-callback thread (good for the "keep callbacks fast"
  threading rule) but adds a queue + worker. Per `realsense.TODO.md`, C++20 build
  has "EasyLogging++ fortify warnings" debt.
- **Measurement (camera-free):** run the align/convert harness with logging at
  INFO vs async-on/off, `perf stat` ms/frame; confirm the frame-callback thread
  is not blocked on log I/O (`perf record` + `perf report`, look for log write in
  the callback stack). **Metric:** ms/frame and absence of log I/O on the hot
  thread. Likely keep ON; just *prove* it.
- **Effort:** low. **Risk:** none. **Type:** build-flag (verification only).

---

## 9. P2 — SIMD opportunities in hot conversion / disparity (NEON → SVE2)

- **What + where:** `src/cuda/cuda-conversion.cu` shows the YUY2→RGB/BGR math; the
  CPU fallbacks for these conversions (`src/proc/color-formats-converter.cpp`) and
  the NEON align (`src/proc/neon/neon-align.cpp`) are the CPU hot loops. With the
  §2.1 `-march=armv9.2-a` change, GCC can **autovectorize these to SVE2** — which
  it cannot today (armv8-a baseline). There may also be hand-written-NEON loops
  that a hand-written SVE2 version would beat, but **start with letting the
  compiler autovectorize via the flag; only hand-write SVE2 if the autovec
  measurement leaves headroom.**
- **Measurement (camera-free):** as §2.1 (`perf stat` ms/frame on the conversion
  harness, baseline vs armv9.2 autovec; inspect `objdump -d` for `z`-registers).
  For hand-written SVE2 (if pursued), A/B the specific function with
  `perf stat -e instructions,cycles`.
- **Effort:** low (flag, covered by §2.1) / high (hand-written SVE2). **Risk:**
  low / medium. **Type:** build-flag first, then optional code change.

---

## 10. Reliability-adjacent — frame queue depth / drain-to-newest (NOTE only)

- **What + where:** TODO Runtime Reliability: *"Drain to the newest frameset and
  use queue depth 1 or 2 for display paths."* This is a **latency** improvement
  (lower end-to-end lag, fewer stale frames) **and** a reliability item (bounded
  memory, faster stop). **It intersects the controller constraint** — it does NOT
  add streams or control transfers, it *reduces* buffering, so it is USB-safe and
  reliability-positive. Belongs primarily to the reliability plan
  (`50-remediation-plan.md`); listed here because it lowers perceived latency.
- **Measurement (camera-dependent, post-reboot):** `rs-gb10-profiler` already
  records inter-frame gaps and processing/render timing; A/B queue depth 1 vs 2 vs
  default on `vga30`, compare *latency* (inter-frame gap distribution, stale-frame
  count) — **not** FPS. **Metric:** p99 frame-age at consumer.
- **Effort:** low. **Risk:** low (reliability-positive). **Type:** code/config in
  the capture path (not the SDK build).

---

## 11. Prioritized summary

**USB-neutral = does NOT add streams, raise FPS, or add control transfers (all
items below qualify).** **Camera-free = can be measured on synthetic buffers
without the D435.**

### Build-flag / config items

| Pri | Item | §  | Effort | Risk | Camera-free measure? | USB-neutral |
|-----|------|----|--------|------|----------------------|-------------|
| P0  | Fix `CUDA_HOME` → `/usr/local/cuda` (nonexistent 13.2 default) | 1.1 | trivial | very low | yes (CMakeCache check) | yes |
| P0  | Keep CUDA arch `121` (verified, no-op) | 1.2 | none | none | yes (nvcc compile) | yes |
| P1  | `-mcpu=native`(→armv8-a) → `-march=armv9.2-a` (SVE2 feature set; tune is open A/B {generic,neoverse-v2,grace}) + SIGILL cross-cluster check | 2.1 | low | low–med | yes (perf stat harness) | yes |
| P2  | LTO/IPO A/B (link-clean + ms/frame + rebuild cost gates) | 7 | low–med | med | yes (harness) | yes |
| P2  | sccache CUDA cacheability (`--show-stats`) | 8.1 | trivial | none | yes | yes |
| P2  | Async logging cost/benefit confirm | 8.2 | low | none | yes | yes |
| P2  | OpenCV-CUDA build decision (measure CPU cv2 first) | 6 | high | low | partly | yes |
| P2  | GCC-14 toolchain A/B (may know cortex-x925) | 2.1 | med | low | yes | yes |

### Code-change items

| Pri | Item | §  | Effort | Risk | Camera-free measure? | USB-neutral |
|-----|------|----|--------|------|----------------------|-------------|
| P1  | align: eliminate per-frame H2D/D2H (hostRegister/managed/ATS), **per-kernel gated, can regress** | 3.1 | med | med | yes (nsys/ncu harness) | yes |
| P1  | align block-size 32×32=1024 — measure occupancy, change only if ncu says so | 3.2 | trivial | low | yes (ncu) | yes |
| P1* | Upload-once keep-on-GPU bridge to SAM3 inference (mostly VIGIL, not SDK) | 5 | high | med | partly (nsys crossings) | yes |
| P2  | pointcloud: cache device buffers (stop per-call malloc/free) — gate on a real consumer | 4.1 | low | low | yes (nsys) | yes |
| P2  | conversion `numBlocks=0` when `count<256` — **correctness note** (grid-stride covers 640×480; only tiny frames misfire) | — | trivial | low | yes (unit) | yes |
| P2  | SVE2 autovec of conversion/disparity (via §2.1 flag; hand-SVE2 only if headroom) | 9 | low/high | low/med | yes (perf/objdump) | yes |
| P2  | Queue depth 1–2 / drain-to-newest (latency; reliability-owned) | 10 | low | low | no (camera) | yes (reduces buffering) |

\* P1 for VIGIL throughput, P2 for *this SDK* (it's a bridge above librealsense).

---

## 12. MANDATORY caveat

**None of the items above may be reported as a performance "win" until the
specific before/after benchmark in its row has been executed and shows a
statistically clear improvement (per CLAUDE.md rule #4).** The GB10 xHCI
controller is currently DEAD; all camera-dependent measurements are blocked until
reboot, and even the camera-free CUDA/CPU microbenchmarks have **not** been run
this session — only the *enabling facts* were verified (CUDA 13.0 path mismatch,
X925/A725 cores, `-mcpu=native`→armv8-a, `-mtune=native`→empty/generic-equivalent
(no X925 cost model in GCC 13.3), `-march=armv9.2-a`→armv9.2+SVE2 accepted,
ATS raw-malloc-pointer-in-kernel PASS, managed-memory PASS). The recommended
order of operations post-reboot:

1. P0 build-correctness fix (1.1) and re-verify the CUDA toolkit identity.
2. Camera-FREE microbenchmarks (§2.1 ISA, §3 align nsys/ncu, §4 pointcloud, §7
   LTO, §8 sccache) on synthetic frames — **no camera, no USB risk.**
3. Cross-cluster SIGILL safety check (§2.1) before adopting any armv9 flag.
4. Only after the camera is confirmed stable on a USB3 link: end-to-end
   `rs-gb10-profiler` A/Bs on **single** tested profiles (`vga30`, then an
   848x480@60-equivalent) — never multi-stream perf A/Bs, to respect the
   controller constraint.
