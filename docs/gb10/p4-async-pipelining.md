# P4 — Async Pipelining (double-buffer + CUDA streams): Feasibility & Microbench (#31)

Host `spark-3066` · NVIDIA **DGX Spark / GB10** (sm_121, unified memory) · CUDA 13.0 · gcc 13.3 · Ubuntu 24.04.
Bench: [`scripts/gb10/bench_async_pipeline.cu`](../../scripts/gb10/bench_async_pipeline.cu) +
[`scripts/gb10/async-pipeline-bench.sh`](../../scripts/gb10/async-pipeline-bench.sh). **NO CAMERA — synthetic input only.**

## TL;DR — **NO-GO** for the shipped single-camera real-time path

Adding double-buffering + multi-CUDA-stream overlap on top of the already-landed cached buffer pools
(Finding A) is **NOT worth integrating** for the single-camera real-time use case the fork ships for.
The overlap *mechanism is real and measurable* (conversion gains up to ~+58% aggregate throughput at
~84% overlap efficiency), but it speeds a CUDA op that is **already 80–270× faster than the camera frame
rate** and that represents **< 2.5% of the end-to-end frame budget** — Finding A showed the e2e cost is
plumbing, which pipelining cannot touch. The cost side is pure downside: K-deep multi-stream **breaks the
byte-identical single-buffer cache contract**, adds race-prone shared-buffer management, and raises
per-frame latency 3–4×. **Verdict: NO-GO** for single-camera real-time. *Revisit only* if an offline/batch
or multi-camera **aggregate-throughput** workload ever appears (see "Scope nuance").

This is an honest, measured "no" — exactly the acceptable outcome the investigation was framed to allow.

---

## Design — how the overlap maps onto the cached paths

The shipped cached path (default `RS2_PC_MODE=1` / `RS2_CONV_MODE=1`, see
[`src/cuda/cuda-pointcloud.cu`](../../src/cuda/cuda-pointcloud.cu) and
[`src/cuda/cuda-conversion.cu`](../../src/cuda/cuda-conversion.cu)) is, per frame:

```
H2D memcpy  ->  kernel<<<...>>>  ->  cudaStreamSynchronize(0)/cudaDeviceSynchronize()  ->  D2H memcpy
```

— a **single default-stream, fully synchronous** sequence reusing a process-static, grow-only, mutex-guarded
device buffer. Because the buffer is single-instance and the stream syncs every frame, frame *N*'s D2H cannot
overlap frame *N+1*'s H2D or compute.

The pipelined variant (**B**) breaks that serialization:

- **K device buffers** (`K = 4` here) on **K CUDA streams**, round-robin per frame.
- **Pinned host memory** + **`cudaMemcpyAsync`** so copies are genuinely asynchronous (pageable +
  `cudaMemcpyAsync` silently serializes — pinning is a *validity blocker*, not a nicety; without it B would
  show ~0 overlap *by construction* and the verdict would be worthless).
- One `cudaStreamSynchronize` per stream **at end of batch** for throughput; per-frame CUDA-event pairs for
  latency. Frame *N*'s D2H now overlaps frame *N+1*'s H2D / compute on a different stream.

Integration into the library would require: a ring of K cached buffers per resolution (vs the single buffer
today), per-stream lifetime tracking so the host doesn't read a D2H that hasn't completed, and pinned-host
staging of the producer's frame data. **This is exactly what breaks the current byte-identical contract** —
the cached-pool unit test ([`scripts/gb10/test-cached-pools.sh`](../../scripts/gb10/test-cached-pools.sh))
asserts mode-1 output is byte-identical to baseline over a grow/shrink resolution sequence; a K-deep ring with
async completion is a materially different, race-sensitive object.

### Method — same work, only scheduling differs

Three variants run the **identical library kernels** (`kernel_deproject_depth_cuda`,
`kernel_unpack_yuy2_bgr8_cuda`, forward-declared and linked from the real `.cu` files — the shipped wrappers
hard-code the default stream + internal sync, so you cannot pipeline *through* them; you drive the same kernel
yourself):

| Variant | Host memory | Streams | Sync | Role |
|---|---|---|---|---|
| **REF** | pageable | 1 (default) | per frame | the actual shipped cached path, via `rscuda::deproject_depth_cuda` / `unpack_yuy2_cuda_helper` (fidelity anchor) |
| **A** | **pinned** | 1 | per frame | isolates the *pinning* effect (REF→A) |
| **B** | **pinned** | **K=4** | end of batch | isolates the *overlap* effect (A→B) |

Attribution: `pinning gain = REF→A`, `overlap gain = A→B`, `total P4 = REF→B`. Plus an **isolated per-stage
table** (H2D / kernel / D2H timed separately) so the verdict rests on the copy-vs-compute ratio (physics), not
just on A-vs-B deltas that can fall inside run-to-run noise. **Overlap efficiency** =
`(sum_stages − period_B) / (sum_stages − max_stage)` — 1.0 means B fully hid copies behind compute, 0.0 means
no overlap. 400 timed iters, 30 warmup discarded, p50/p95 reported.

---

## Measured results (representative run; stable across 3 repeats)

GB10, sm_121, unified memory. SM clock: idle reads ~350 MHz but the bench's own background sampler shows the
GPU **ramps to a sustained 2548 MHz under the bench load** (`min=351 max=2548 last=2548 MHz`), so the timed
work runs near full clock — the idle reading is not a confound (see "Clock note").

### Per-stage isolation (the decisive evidence)

| Workload @ res | H2D (us) | kernel (us) | D2H (us) | sum (us) | overlap floor = max (us) |
|---|---:|---:|---:|---:|---:|
| Pointcloud 848×480 | 19.1 | **10.4** | **87.4** | 117.0 | 106.6 |
| Pointcloud 1280×720 | 40.9 | **37.1** | **196.7** | 274.8 | 237.7 |
| Conversion 848×480 | 15.7 | 8.5 | 23.5 | 47.8 | 39.3 |
| Conversion 1280×720 | 34.9 | 15.1 | 49.3 | 99.3 | 84.2 |

**The kernel is small; the copy (esp. D2H) is the cost.** This *refutes* a naïve "kernel-bound, nothing to
overlap" rationale — there genuinely is copy time to hide. The real story is in how well B hides it.

### A vs B — throughput (fps), latency (us), overlap efficiency

| Workload @ res | REF fps | A fps | B fps | overlap gain A→B | overlap eff | A lat p50/p95 | B lat p50/p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Pointcloud 848×480 | 7722 | 7706 | 8707 | **+13.0%** | **20.5%** | 118 / 128 | 446 / 453 |
| Pointcloud 1280×720 | 3533 | 3638 | 3635 | **−0.1%** | **−0.8%** | 266 / 268 | 1032 / 1409 |
| Conversion 848×480 | 15499 | 15551 | 24602 | **+58.2%** | **83.7%** | 57 / 62 | 161 / 163 |
| Conversion 1280×720 | 8680 | 8591 | 11545 | **+34.4%** | **84.1%** | 112 / 112 | 336 / 434 |

Pinning gain (REF→A) is within noise (−1.0% … +3.0%) — **consistent with Finding A: on GB10's coherent
memory, host↔device copies are cheap, so pinned vs pageable barely moves.**

### What the numbers say

- **Overlap works where the kernel is comparable to the copy and the copy is small** (conversion: ~84%
  efficiency, +34–58% aggregate throughput). The mechanism is real.
- **Overlap collapses where D2H dominates and is bandwidth-bound** (pointcloud 1280×720: **−0.8%** efficiency,
  period_B ≈ sum_stages). On unified memory, concurrent copies + compute **contend for one memory pool**, so
  the overlap floor (`max_stage`) is barely below `sum_stages` and saturates fast as resolution grows. *This*
  is the real GB10 unified-memory story — not "copies are free," but "copy and compute share one memory, so
  overlap caps out quickly."
- **Even the best case is unusable end-to-end.** Every variant runs at **3500–24600 fps** for a camera that
  delivers **30–90 fps**. The CUDA op is 57–280 us, i.e. **0.2–2.5% of an 11–33 ms frame budget**. Finding A
  established the e2e cost is ~2 ms/frame of *plumbing* that pipelining does not touch. Speeding a stage worth
  < 2.5% of the budget — by *any* factor — is invisible end-to-end.

---

## GO / NO-GO

### **NO-GO** for the shipped single-camera real-time path.

The decision rests on three legs (note: **latency is deliberately *not* the decisive reason** — B's worst
per-frame latency (~1.1 ms, pointcloud 1280×720) is still well inside an 11–33 ms camera budget; a reviewer
who checks the budget would correctly reject a latency-based argument):

1. **No usable throughput headroom.** Op throughput is already 80–270× the camera rate; the op is < 2.5% of
   the frame budget; the dominant e2e cost (Finding A) is plumbing pipelining can't reach. Any overlap win is
   invisible end-to-end.
2. **Overlap is structurally capped on unified memory** and **collapses at higher resolution** for the
   D2H-dominated pointcloud path (−0.8% at 1280×720) because concurrent copies + compute contend for one
   memory pool. The single workload that benefits (conversion) benefits only in *aggregate throughput*, which
   the real-time single-camera path does not consume.
3. **Cost is pure downside.** K-deep multi-stream **breaks the byte-identical single-buffer cache** validated
   by the cached-pool test, adds race-prone per-stream completion tracking, and triples–quadruples per-frame
   latency — for zero usable benefit.

**Clock-robustness of the verdict:** even if the SM clock were higher than observed, the kernel (compute)
shrinks while bandwidth-bound copies scale less — so the op gets **more** copy-bound and overlap would help
throughput **more**, never less. That only **deepens** leg 1's "unusable headroom." The verdict does not
depend on the exact clock.

**Consistency with Finding A:** Finding A captured the real win (eliminating per-frame `cudaMalloc/cudaFree`
churn via cached pools; pointcloud 3.3×, conversion ~NEON-parity, byte-identical). P4 sits on top of an
already-cheap, already-de-churned path whose remaining cost is dominated by plumbing, not by the
copy/compute scheduling P4 reorders. NO-GO is exactly what Finding A predicted.

### Scope nuance (the one place this is *not* a flat no)

The **conversion** path shows up to **+58% aggregate throughput** at ~84% overlap efficiency. If a future
**offline/batch** processing job or a **multi-camera aggregate-throughput** workload ever appears (where many
frames are processed back-to-back and *total* throughput, not per-frame deadline, is the metric), pipelining
the conversion path could be worth ~30–50%. **Revisit then** — and only the conversion path, not pointcloud.

### If it were ever a GO — minimal, `#if`-guarded integration sketch

For the record (do **not** implement now): gate behind a new `RS2_GB10_ASYNC_PIPELINE` define, add a
`RS2_*_MODE=3` selecting a K-deep ring of cached buffers + pinned host staging + per-stream completion events,
leave modes 0/1/2 byte-identical and default. The cached-pool byte-identity test would need a new
aggregate-throughput correctness mode (order-independent, all-frames-eventually-correct) since strict
single-buffer byte-identity no longer applies under a ring.

---

## Reproduce (no camera)

```bash
just bench-async          # build (-O3 -Werror clean) + run; prints stage table, A/B/REF, efficiency
# or directly:
bash scripts/gb10/async-pipeline-bench.sh
```

The script compiles `bench_async_pipeline.cu` linking the real `cuda-pointcloud.cu` + `cuda-conversion.cu`
(so the **identical** library kernels run), best-effort pins the SM clock (`nvidia-smi -lgc`, falls back to a
long warmup if it needs root), background-samples the sustained SM clock, runs, and restores clocks.

**Build is `-Werror all-warnings -Xcompiler -Wall,-Wextra,-Werror` clean** at `-O3`.

### Clock note

`nvidia-smi -lgc 0,<max>` is accepted but the idle-read clock stays ~350 MHz on this GB10 / L4T DVFS
governor (no `jetson_clocks` present). The bench's background sampler confirms the GPU **ramps to a sustained
2548 MHz while the timed work runs**, so the measurements are effectively at high clock. As argued above, the
NO-GO verdict is clock-robust regardless.

### Suggested `just` recipe (I did not edit the justfile — add this)

```
# P4 (#31) async-pipelining microbench: shipped cached path vs double-buffered multi-stream (NO camera, GPU)
bench-async:
    bash "{{repo_root}}/scripts/gb10/async-pipeline-bench.sh"
```
