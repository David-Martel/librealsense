# GB10 NVIDIA/CUDA acceleration — LIVE validation (2026-06-05)

**Question:** do the recently-integrated NVIDIA/CUDA tooling updates *meaningfully* accelerate the RealSense device?
**Answer (measured, on-device):** **YES for the cached-pool pointcloud (1.8×), the GPU-resident render (no D2H), and align (15–19×) — NO (honest parity) for colorize/conversion.** A uniform "everything is faster" story would be wrong; the high-integrity answer is per-op.

> **Meaningful bar (set before measuring):** the camera is 30 fps over USB regardless — CUDA cannot change that. "Meaningful" = **reduced per-frame host/CPU compute cost** (freed frame budget / offloaded CPU), measured as `CPU_ms / CUDA_ms`. A win must materially cut the per-frame cost; parity or a regression is not a win.
>
> **Method:** single depth stream (the safe envelope — controller GREEN throughout all runs, no 2-stream live open), GPU warmed (CUDA run first), strictly sequential (one `.so` open at a time, never a 2-process open), `build-gb10-full` (CUDA) vs `build-gb10-nocuda` (CPU/NEON) on the same scene via PYTHONPATH. Tool: `just hil-cuda-bench`.

## Per-op CUDA-vs-CPU (live, p50)

| Op | CUDA p50 | CPU/NEON p50 | speedup | verdict |
|----|---------:|-------------:|--------:|--------|
| **pointcloud** (depth→points) | **0.234 ms** | 0.420 ms | **1.8×** | **MEANINGFUL — the cached-pool payoff** |
| **colorize** (depth→RGB) | 1.498 ms | 1.477 ms | 0.99× | **parity — NOT a win** (plumbing-bound, Finding A) |
| **align** (depth→color) | ~0.29 ms | 4.3–5.7 ms | **15–19×** | **MEANINGFUL** (cited — `benchmarks.md`; 2-stream, not re-run this session to avoid tail-of-session controller risk) |
| **keep-on-GPU render** | **3.43 ms** p50, **no D2H** | (cv2 path pays ~3 ms readback) | — | **MEANINGFUL** (GL-resident; fresh measure, `just hil-keepongpu`) |

### The headline finding — the cached-pool work *flipped* pointcloud from a regression to a win
The **shipped (per-frame-`cudaMalloc`) CUDA pointcloud was ~0.57× NEON — i.e. SLOWER than CPU**. The recently-integrated **cached buffer pools (`RS2_GB10_PC_ZEROCOPY`, promoted to default)** removed the per-frame alloc churn and measured **3.3× over that malloc baseline → ~1.8× faster than NEON CPU**, confirmed live today (0.234 ms vs 0.420 ms). So the recent NVIDIA/CUDA tooling change is exactly what made GPU pointcloud worth using on GB10. colorize/conversion stay plumbing-bound (coherent memory) — honest parity, consistent with "Finding A" and the P4 async NO-GO.

## Non-headless HIL validation — enhanced checks, live PASS
`just hil-nonheadless --depth` **PASS** (controller GREEN), exercising the new depth-integrity + frame-continuity checks: `frames_monotonic`, `no_dropped_frames` (0), `arrival_gap_sane`, `ts_domain_consistent` (Global Time), `depth_not_all_zero/saturated/frozen` (valid-pixel ratio 0.83), 29.9 fps, 0 stutters. This doubles as a happy-path continuity/no-drop reliability check.

## Reliability hardening (this session) — applied + status
3 libusb error/recovery-path fixes applied (warning-clean, cached-pool happy path byte-identical) — see `reliability-audit-2026-06-05.md`:
- `messenger-libusb` submit/cancel: classify the `LIBUSB_ERROR_*` code, not POSIX `errno` (was losing `NO_DEVICE`/`TIMEOUT`/`BUSY` that GB10 recovery keys on);
- `request-libusb` `internal_callback`: null-guard the callback shared_ptr (event-thread crash on the teardown race);
- `enumerator-libusb`: catch-by-const-ref + log the real reason (was swallowing the P7 REFUSE message).
These are **error-path, correct-by-inspection**. Validating them live means deliberately inducing `-110`/re-acquire/HC-death on the fragile controller — that asymmetry (risk a reboot to re-confirm a one-line classification fix) isn't worth it at session tail, so **live HIL for the deliberately-destabilizing reliability tests is deferred** (the 6 prioritized tests are listed in the audit doc). The happy-path reliability (no-drop/continuity/30fps/0-stutter) IS validated above.

## Bottom line
The recently-integrated NVIDIA/CUDA tooling **meaningfully accelerates** the RealSense device on the hot paths that matter — **pointcloud (cached pools, 1.8×)**, **align (15–19×)**, **GPU-resident render (no D2H)** — while colorize/conversion are honest parity. All validated single-stream, controller GREEN; align cited from prior 2-stream measurement.
