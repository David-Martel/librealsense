# GB10 long-soak, capture/playback, and CUDA-acceleration reality (2026-06-03)

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

## 4. CUDA acceleration reality on GB10 — `BUILD_WITH_CUDA` does NOT help these ops
Head-to-head, same scene, CUDA build vs CUDA-OFF build (both `-mcpu=native` + NEON + OpenMP):
| op | res | CUDA ms | CPU(NEON) ms | result |
|----|-----|---------|--------------|--------|
| `rs.colorizer` | 848×480 | 1.467 | 1.475 | **1.00× — no benefit** |
| `rs.pointcloud` | 848×480 | 0.995 | **0.563** | **0.57× — CUDA SLOWER** |
| `rs.colorizer` | 1280×720 | 3.161 | 3.178 | **1.00× — no benefit** |
| `rs.pointcloud` | 1280×720 | 1.488 | **0.859** | **0.58× — CUDA SLOWER** |

**librealsense's CUDA path provides zero speedup (colorize) or a slowdown (pointcloud) on GB10, at
BOTH resolutions.** The 20-core Cortex-X925/A725 + NEON + OpenMP CPU path is faster for these small
per-frame depth ops; the per-frame H2D/D2H copy + kernel-launch overhead on GB10's unified memory is
not amortized (same story as cv2.cuda `cvtColor` 0.5× vs `resize` 3.8× — only large compute wins).

**Architectural implication for vigil-spark / GB10:** do **not** ship librealsense with
`BUILD_WITH_CUDA` expecting acceleration of the realsense processing path — it isn't there. Keep depth
colorize/pointcloud on the ARM CPU and **reserve the GPU for ML inference** (torch/vLLM). This also
frees GPU memory/SMs for the inference pipeline.

### Caveats (honest scope)
- Measured end-to-end Python-observed latency **including D2H materialization** (`get_data`/`get_vertices`)
  — which is the real cost for a CPU consumer like vigil; a pure keep-on-GPU pipeline (no D2H) was not
  evaluated and could differ (but vigil reads on CPU, so D2H is real for it).
- `rs.align`(depth→color) was **not** benchmarked CUDA-vs-CPU (needs dual stream); colorize+pointcloud only.
- Both builds use the same SIMD CPU flags, so the comparison isolates CUDA on/off fairly.

## Standardized suite (idempotent + stable)
`just hil-soak [--display]`, `just hil-capture-playback [--display]`, `just hil-cuda-bench`. Each:
pre-flight gate (refuses on dead controller / no camera / USB-2 link) → warmup → run → continuous
tripwire (aborts + dumps forensics on HC-died) → `result.json` in a unique `hil-runs/<ts>-<test>/` dir.
Re-runnable with no state leakage; multi-stream phases are eyes-open (tripwire-guarded).
