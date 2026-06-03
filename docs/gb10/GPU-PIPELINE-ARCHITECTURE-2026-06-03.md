# GB10 RealSense — full-pipeline GPU acceleration: USB → Processing → Display → File (2026-06-03)

Goal: push GPU/parallel acceleration through every stage of the RealSense pipeline on DGX Spark / GB10,
**measure** each claim, and account for the shared (unified) memory architecture. This consolidates the
op-level review ([`CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES`](CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES-2026-06-03.md))
and the per-op benchmarks ([`HIL-SOAK-AND-ACCEL`](HIL-SOAK-AND-ACCEL-2026-06-03.md) §4) into one
stage-by-stage map. **Measured numbers are marked ✅; analysis/untested is marked ⬜.**

## Stage map (each stage → its GPU option, status, harness)

| stage | op | best path on GB10 | measured | harness |
|---|---|---|---|---|
| **1. USB ingest** | format conversion YUYV→RGB | CUDA auto-selected; **CPU-neutral vs NEON** ✅ | CUDA 2.00 vs NEON 2.03 ms/frame @720p (no regression) | `just hil-gpu-pipeline --convert-only` |
| **2. Processing** | **align** depth→color | **CUDA 15–19× > NEON** ✅ | CUDA p50 0.29 ms vs NEON 4.3–5.7 ms | `just hil-align-bench` |
| | **pointcloud** | **CUDA cached-device 3.3× > shipped, > NEON** ✅ | 0.32 ms (cached) vs 1.05 (baseline) vs 0.54 NEON | `just hil-pc-zerocopy` |
| | post-proc filters (spatial/temporal/hole-fill/…) | **none today (pure scalar)** ✅ / NEON+OMP ⬜ / **learned-CNN via TensorRT** ⬜ | filters 0-accel; a 5-conv CNN filter = **0.9 ms, 37× headroom** ✅ | `rs-gb10-trt-probe.py` |
| **3. Display / render** | colorize | **OpenGL `colorizer-gl`** (no CUDA path) ⬜ | GL ships (`BUILD_GLSL_EXTENSIONS=ON`) but **never benchmarked** | (todo: GL HIL build) |
| | preview / resize | cv2.cuda ✅ (resize 3.8×) / cvtColor 0.5× ✅ | from prior cv2.cuda HIL | `just hil-advanced` |
| **4. File write** | compressed video | **NVENC** (h264/hevc) ✅ | SSIM 0.965/0.971 @ 34–40× realtime, XPSNR ~31 dB | `just hil-quality` |
| | raw capture | rosbag2 `.db3` = **uncompressed/CPU** (not a GPU path) | — | `just hil-capture-playback` |

## The shared-memory finding (the headline architectural result)
GB10 is unified memory, so the intuition was "eliminate H2D/D2H copies." The pointcloud attribution
ladder (baseline → cached-device → cached-managed, each correctness-checked vs a numpy deproject, all
`max_abs_diff = 0`) shows the **dominant** cost is elsewhere:
- **ROCK-SOLID (mode0→mode1):** the shipped CUDA path was 0.57×/slow because it calls `cudaMalloc`/
  `cudaFree` **every frame** (~180 alloc+free/sec — synchronizing, heavyweight). **Caching the device
  buffers → 3.3× faster than the shipped baseline** (and ≥ NEON: cached-device 0.32 ms vs a scene-dependent
  NEON 0.38–0.54, i.e. ~1.2–1.7×). **Allocation churn was the bottleneck.**
- **NOT a clean copy-vs-no-copy test:** `cudaMallocManaged` (mode 2) came in *slower* than cached-device
  (0.50 vs 0.32 ms) — **but mode 2 still does two plain `memcpy`s (in/out) AND an explicit
  `cudaDeviceSynchronize`, so it did not eliminate the copies; it swapped `cudaMemcpy` for `memcpy`+sync.**
  So "copies don't matter on GB10" is **unproven** — what is shown is only that *this* managed-memory
  variant was not faster than cached-device. **True pool-level zero-copy on the SDK's own frame buffers
  (cached `cudaHostRegister`) was not isolated.**
- **Proven lever = eliminate per-frame allocation churn (cache buffers).** Apply the same cached-buffer
  pattern to `cuda-conversion.cu` and `cuda-align.cu`. Whether removing copies adds anything on top is
  a separate, untested question.

## TensorRT — where it legitimately fits (capability-probed, not integrated)
The core convert/align/pointcloud pipeline is deterministic geometry — **no neural net, so TensorRT has
nothing to accelerate there, and bolting it on would be fabrication.** TensorRT becomes real only for a
**learned** stage: a CNN depth **denoise / hole-completion / super-resolution** replacing the scalar
post-processing filters (which have zero acceleration today), or a downstream perception model (vigil's).
Capability probe (`rs-gb10-trt-probe.py`, synthetic **small** 5-conv/16ch depth filter @848×480, FP32
strongly-typed, **trtexec GPU-compute only — excludes H2D/D2H**): **median 0.897 ms, 37× headroom inside a
30 fps frame** (FP16/INT8 ~2–4× faster). Read this as "a *small* learned filter fits with large margin" —
**production-size depth-completion/denoise U-Nets are orders of magnitude larger and would need their own
probe** (headroom collapses fast, and real use pays the transfer the probe excludes). The honest takeaway:
for a *modest* learned stage, GB10 compute is not the constraint — a trained model is. App/research-layer;
nothing wired into the SDK.

## "All the way to disk on the GPU" — what that actually means
The only GPU file-write path is **NVENC** (h264/hevc via the `/opt/gb10-cuda` ffmpeg) — measured
visually-lossless-ish (SSIM 0.965/0.971) at 34–40× realtime, though XPSNR ~31 dB says the default cq is
under-tuned for medical (→ cq sweep). **rosbag2 `.db3` is uncompressed and CPU-bound** — "GPU to disk"
means the NVENC *video* path, not the rosbag path. A fully GPU-resident chain
`convert(CUDA)→align(CUDA)→colorize(GL)→NVENC encode` avoids host round-trips end-to-end; the GL and
CUDA-interop arms are designed but unmeasured (need a GL-enabled HIL build).

## Prioritized, evidence-based recommendations
1. ✅ **Done/validated:** align CUDA (keep on); pointcloud cached-device fix (gated `RS2_GB10_PC_ZEROCOPY`,
   `RS2_PC_MODE=1`); conversion CUDA is safe (no regression). Keep `BUILD_WITH_CUDA=ON` for vigil.
2. **Ship the cached-buffer pattern** to `cuda-conversion.cu` + `cuda-align.cu` (same diff; cuts alloc
   overhead). Promote pointcloud `RS2_PC_MODE=1` to default after a multi-instance/thread check of the
   shared static pool.
3. **Benchmark the GL pipeline** (ships but untested) — the only GPU path for colorize and a candidate
   keep-on-GPU chain to kill residual D2H.
4. **NEON+OpenMP the scalar filters** *if* a live consumer enables them; otherwise leave them.
5. **TensorRT** is a future learned-filter lane (37× headroom) — needs a model, not more GPU.
6. **NVENC cq sweep by XPSNR** for the medical file-write path.

## Harness changes this work added (the "intelligent test update")
- `rs-gb10-gpu-pipeline.py` (+`_pipeline_compare.py`) — end-to-end USB→convert→align→[colorize] throughput
  + **per-frame CPU-time** attribution, full-vs-nocuda; `--convert-only` isolates conversion.
- `rs-gb10-align-bench.py`, `rs-gb10-pc-zerocopy.py` — per-op CUDA-vs-CPU with correctness checks.
- `rs-gb10-trt-probe.py` — TensorRT NN-headroom capability probe.
- All are tripwire-armed (2-stream is eyes-open), idempotent (timestamped artifacts), and run both builds
  back-to-back so a build/scene confound can't masquerade as a GPU effect.
