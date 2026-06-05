# GB10 long-soak, capture/playback, and CUDA-acceleration reality (2026-06-03)

> **Historical snapshot (2026-06-03).** Soak/playback results and per-op CUDA measurements remain
> current. The "keep-on-GPU pipeline not evaluated" caveat (§4) has since been resolved: **keep-on-GPU
> GL chain measured at 1–7 ms/frame saving** at 720p+
> (see [ROS2-GL-PINNED-FINDINGS-2026-06-05.md](ROS2-GL-PINNED-FINDINGS-2026-06-05.md)).

Host `spark-3066`, D435 on the clean USB-3 bus, full CUDA build (`build-gb10-full`) + a CUDA-OFF
build (`build-gb10-nocuda`, identical flags minus `BUILD_WITH_CUDA`) for the head-to-head. Tests are
standardized + idempotent: `scripts/gb10/hil_common.py` (pre-flight gate, continuous controller
tripwire, timestamped artifact dir, stats, `--display` non-headless) + `scripts/gb10/rs-gb10-hil-suite.py`
(`just hil-soak` / `hil-capture-playback`). Every run wrote `result.json` + (if death) kernel forensics.

## 1. Long-soak — SURVIVED (controller GREEN, ZERO `-110`)
~5-minute phased soak on the clean bus, RSUSB + P2/P3/P4/P7 active:
| phase | secs | frames | fps | gaps |
|-------|------|--------|-----|------|
| single depth @60 | 90 | 5037 | 56.0 | 1 |
| dual depth+color @60 + `rs.align` | 90 | 5320 | 59.1 | 1 |
| start/stop churn ×8 (death-#2 pattern) | — | all 8 streamed | — | — |
| quad-stream depth+color+IR×2 @60 | 60 | 3537 | 58.9 | 2 |

**Control features exercised mid-soak** (these issue the control transfers that *cause* the `-110`
storms): emitter toggle, **laser power sweep 0→360**, **visual-preset cycle 0→5**, auto-exposure toggle
— all OK. **Total `-110` = 0; no HC-died; SURVIVED.**

This clears the "multi-minute soak" bar. **Still not an envelope relaxation for medical-critical use:**
one soak, **RSUSB only** (not V4L2), and four confounds remain (clean bus + mitigations + backend +
fresh build). Conservative single-stream guidance stays until a V4L2 soak + repeats also show zero `-110`.

## 2. Capture / playback stress — PASS (deterministic replay)
Record→replay path (note: this build uses **rosbag2 `.db3`**, not legacy `.bag` — `BUILD_ROSBAG2=ON`):
- Recorded 429 frames (depth+color @30, 15 s) → 889 MB `.db3`.
- Real-time replay: 432 frames, 4 gaps.
- **Max-speed replay ×4 loops: 434 / 434 / 434 / 434 — perfectly deterministic, matches record.**
- Verdict: recorded ✓, replay frame-count stable ✓, matches record ✓. Controller GREEN, 0 `-110`.

## 3. Non-headless (watchable) rendering — CONFIRMED
Both `soak --display` and `capture-playback --display` render live to an on-screen cv2 window on
`$DISPLAY=:1` and save an `ffmpeg x11grab` proof (`*-proof.png` in the artifact dir). Verified by eye:
the playback window shows the replayed color frames; the soak window shows the live colorized depth.
(`--display` adds ~15 % capture overhead — phase-1 fps 56→47 — expected; the render competes for CPU.)

## 4. CUDA acceleration reality on GB10 — measured per op (which procs even HAVE a CUDA path)
Head-to-head, same scene, CUDA build vs CUDA-OFF build (both `-mcpu=native` + NEON + OpenMP). **Source
grep first** — which procs have a CUDA path: `src/cuda/` has `cuda-conversion.cu`, `cuda-pointcloud.cu`,
`src/proc/cuda/cuda-align.cu`; `pointcloud.cpp`/`align.cpp`/`depth-formats-converter.cpp` gate on
`RS2_USE_CUDA`. **`colorizer.cpp` has NO CUDA reference — colorize has no CUDA path at all.**

| op | has CUDA path? | res | CUDA ms | CPU(NEON) ms | reading |
|----|----------------|-----|---------|--------------|---------|
| `rs.colorizer` | **NO** | 848×480 | 1.467 | 1.475 | identical because **both ran the same CPU code** — `BUILD_WITH_CUDA` is irrelevant to colorize |
| `rs.colorizer` | **NO** | 1280×720 | 3.161 | 3.178 | same — no CUDA path |
| `rs.pointcloud` | **YES** | 848×480 | 0.995 | **0.563** | 0.57× as-shipped — but see UPDATE: the cause is per-frame alloc, not CUDA |
| `rs.pointcloud` | **YES** | 1280×720 | 1.488 | **0.859** | 0.58× as-shipped |

> **UPDATE 2026-06-03 (`CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES` Opp 1, `just hil-pc-zerocopy`):** the 0.57×
> was an artifact of the kernel doing `cudaMalloc`/`cudaFree` **every frame**. Caching the device buffers
> (mode 1) makes CUDA pointcloud **~0.32 ms — 3.3× faster than the shipped path and faster than NEON.**
> `cudaMallocManaged` (unified memory) is *slower* than cached-device — on GB10 the cost is allocation
> churn, not the copy. All rungs correctness-verified (max-abs-diff 0.0 vs numpy deproject).
| `rs.align`→color | **YES** | 848×480 | **0.293–0.295** | **4.33–5.67** | **15–19× — CUDA much FASTER** (vigil's per-frame op; measured 2026-06-03, `just hil-align-bench`) |
| depth-format conversion | YES | — | — | — | not benchmarked |

**What is actually measured:** CUDA on GB10 is **op-dependent — not a blanket win or loss**:
- `rs.align`(depth→color): **CUDA 15–19× FASTER** (CUDA p50 0.293–0.295 ms, rock-stable across runs;
  CPU 4.33–5.67 ms, scene-dependent because the CPU path short-circuits zero-depth pixels). align does
  heavy per-pixel reprojection (two intrinsics + extrinsics + scatter) that parallelizes across thousands
  of threads, so the win dwarfs the H2D/D2H cost. **This is the load-bearing op vigil runs every frame.**
- `rs.pointcloud`: CUDA **0.57× SLOWER** — cheap per-pixel deproject dominated by the D2H vertex-buffer
  copy on GB10's unified memory (same story as cv2.cuda `cvtColor` 0.5× vs `resize` 3.8×).
- `rs.colorizer`: **no CUDA path at all** — "no benefit" is not a CUDA result, just CPU-only code.

**Scoped implication (corrected):** a build-wide "disable CUDA" recommendation would be **WRONG** for
vigil — the op it actually depends on (align) is 15–19× faster with CUDA. **Keep `BUILD_WITH_CUDA=ON`
for vigil.** pointcloud being slower is real but vigil does not pointcloud per frame; if a consumer is
pointcloud-heavy and CPU-bound, that specific op can be steered to CPU, but the build stays CUDA-on.
Both align runs left the controller **green with zero `-110`** (2-stream depth+color steady-state, fixed
config) — another data point that the current unit/firmware/topology tolerates vigil's stream shape.

### Caveats (honest scope)
- End-to-end Python-observed latency **including D2H materialization** (`get_data`/`get_vertices`) — the
  real cost for a CPU consumer like vigil; a keep-on-GPU pipeline (no D2H) was not evaluated.
- align CPU time is **scene-dependent** (zero-depth short-circuit): 4.33–5.67 ms over two static-scene
  runs → speedup 14.7–19.4×. CUDA side is idempotent (0.293/0.295). Both builds run back-to-back; camera
  static. The *direction* benchmarked is depth→color (`rs.align(rs.stream.color)`, `kernel_depth_to_other`);
  the reverse (color→depth, `kernel_other_to_depth`) is selectable via `LRS_ALIGN_TO=depth` but unmeasured.
- depth-format conversion (also a CUDA path) still untested.
- Both builds share SIMD CPU flags (verified: full vs nocuda CMakeCache differ ONLY in `BUILD_WITH_CUDA`
  + the CUDA-arch var), so where a CUDA path exists the comparison isolates CUDA on/off fairly.

## Standardized suite (idempotent + stable)
`just hil-soak [--display]`, `just hil-capture-playback [--display]`, `just hil-cuda-bench`. Each:
pre-flight gate (refuses on dead controller / no camera / USB-2 link) → warmup → run → continuous
tripwire (aborts + dumps forensics on HC-died) → `result.json` in a unique `hil-runs/<ts>-<test>/` dir.
Re-runnable with no state leakage; multi-stream phases are eyes-open (tripwire-guarded).
