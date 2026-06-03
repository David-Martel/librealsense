# GB10 RealSense → 3D-Pose → GPU-Render test bed — researched plan, with every stage gate-PROVEN (2026-06-03)

Goal (user): extract a small pipeline from `~/vigil-system` (`/opt/vigil-spark`) into a standalone test-bed
runner — RealSense **RGB+D concurrent** → real-time **VLM/vision** processing (adapted to **3D person pose**)
→ **GPU render incl. OpenGL to display** — instrumented with **real-time telemetry hooks** for evidence/
benchmarks, exploiting DGX-Spark capabilities. This is the **researched plan**; three specialist agents ran
in parallel and **proved every stage with measured facts** (probes live in
`~/realsense-gb10-validation/posebench/`). Build is staged; this is the checkpoint before full integration.

## Feasibility — ALL gates PROVEN (measured, not assumed)
| gate | result | evidence |
|---|---|---|
| **One interpreter loads my GB10 RealSense build + torch** | ✅ `venv-jazzy` Python **3.12.3** imports `build-gb10-full` pyrealsense2 2.58.1 (CUDA-align + USB mitigations) **and** torch 2.12+cu130 (cuda True) together | direct import test |
| **Real-time pose on GB10** | ✅ keypoint R-CNN **17.5 ms = 57 fps** @480×848 (FP16 `.half()` + `channels_last` + det_cap 10) — 30fps-capable, no lighter model needed | `posebench/infer/bench_pose.py` |
| **Real pose weights obtainable** | ✅ `keypointrcnn…DEFAULT` downloaded 226 MB in 2.8 s (pytorch.org works for this) → demo shows REAL COCO-17 keypoints | `posebench/infer/test_weights_download.py` |
| **2D→3D depth-lift correct** | ✅ deproject + median-window hole handling, **23/23 unit tests pass** (round-trip <1 mm) | `posebench/infer/depth_lift.py` + test |
| **Headless OpenGL on the GPU (not llvmpipe)** | ✅ `GL_RENDERER = NVIDIA GB10/PCIe`, GL 3.3 / driver 580; rendered skeleton-over-depth `proof.png` | `posebench/render/probe_egl.py`, `render_proof.py` → `proof.png` |
| **GPU-to-disk video (NVENC)** | ✅ `h264_nvenc` on GB10 (SM 12.1, NVENC v13) encoded a 60-frame clip | `posebench/render/nvenc_test.mp4` |
| **Telemetry spine** | ✅ per-stage CUDA-event GPU time + wall + queue depth + controller health + JSONL/HUD | `posebench/telemetry.py` (smoke-tested) |

### Assumptions my own probes CORRECTED (kept honest)
- My earlier "55 ms FP16 pose" was an artifact of torchvision's default `min_size=800` (model secretly ran at
  800 px) + unbounded detection cap. Pinned transform + det-cap → **17.5 ms**. *Always pin `min_size/max_size`.*
- NVENC ffmpeg is the **system `/usr/bin/ffmpeg`** (has `h264_nvenc`), **not** `/opt/gb10-cuda/bin/ffmpeg` (absent).
- **No human-pose weights on disk** anywhere in `/opt/vigil-spark` (only an unrelated probe-tracker `.pth`) →
  torchvision auto-download is the weight source; vigil's own pose node (CameraHMR/SMPL) **can't run here** (all
  its weights missing + SMPL license) — which is why SMPL is a stretch, not the MVP.

## Architecture — async, GPU-resident, telemetry-instrumented
The pipeline is **async** so the slow stage never throttles capture/render (proven: infer 57 fps ≫ 30 fps, so
even one-frame lag is imperceptible):
```
[capture 30fps] RealSense RGB+D @640×480 (vigil's config) ─ my CUDA-align build (depth→color, 15–19× > NEON)
      │  color bgr8 + aligned depth z16 + live intrinsics (NOT vigil's hardcoded 640×480 K)
      ▼  (ring buffer, depth 3)
[infer ~57fps] torchvision keypointrcnn FP16+channels_last → COCO-17 keypoints [N,17,3]
      ▼
[lift] deproject each keypoint through aligned depth (median 5×5 window) → 3D [N,17,3] metres (GPU or numpy)
      ▼
[render 30fps] headless EGL (moderngl, NVIDIA GB10) → 3D skeleton + colorized-depth bg → FBO
      │  plain texture upload 0.1 ms (CUDA-GL interop not worth it on unified memory)
      ├─► display ($DISPLAY :1 blit) and/or
      └─► NVENC h264 → .mp4   (GPU-to-disk; rosbag .db3 would be uncompressed/CPU)
            telemetry.py wraps every stage: CUDA-event GPU ms, wall ms, queue depths, frame id, controller health
```
Capture/render run ~30 fps; inference runs flat-out on its own thread; lift+render consume the latest keypoints.

## DGX-Spark "claims to fame" exploited (each tied to a measured finding)
- **CUDA align** (my build): depth→color every frame, **15–19× faster than NEON** — the per-frame op the pose
  lift depends on.
- **Cached-buffer CUDA pointcloud** for the depth background: **3.3× faster than the shipped path, > NEON**
  (per-frame `cudaMalloc` churn was the cost — `RS2_GB10_PC_ZEROCOPY`/`RS2_PC_MODE=1`).
- **Unified/coherent memory**: texture upload + frame→CUDA-tensor copies are sub-ms (host "round trip" is one
  physical RAM pool) — so a plain upload beats the complexity of CUDA-GL interop here (measured 0.1 vs 0.76 ms).
- **NVENC** v13 (h264/hevc/av1) for compressed GPU-to-disk capture.
- **TensorRT** = future learned-filter lane only (probed 37× headroom for a small CNN); **R-CNN→TRT export
  fails at tracing** (dynamic NMS/RoI ops) — torch-CUDA-FP16 is the proven baseline, TRT a flagged yak-shave.

## Telemetry contract (the spine — `posebench/telemetry.py`)
`Telemetry(stages=[…])` → per frame: `begin_frame()`, `with tele.stage(name, gpu=True): …`, `set_queue()`,
`end_frame()`. Emits **CUDA-event GPU ms** (wall hides async GPU work), monotonic wall ms, per-stage p50/p95,
inter-stage **queue depths** (where an async pipeline stalls), **frame ids**, rolling **fps**, **controller
health** (journal tripwire — RGB+D + infer + render is the sustained 2-stream load that has killed the xHCI 3×),
a `hud_lines()` overlay for on-screen real-time telemetry, and `telemetry.jsonl` + `result.json` per run.

## Staged implementation plan (what's proven vs what remains)
- **Phase 0 — gates ✅ DONE** (this turn): every stage proven in isolation; modules written (`depth_lift`,
  `inference_config.build_pose_model`, EGL renderer, NVENC, `telemetry`).
- **Phase 1 — integrate (next):** single-process runner wiring capture→infer→render→telemetry; **single-stream
  first** (depth-only sanity), then **add color (2-stream = eyes-open)** with the `hil_common` tripwire armed and
  **short** runs; HUD on screen; JSONL out. Deliverable: `posebench/run_testbed.py`.
- **Phase 2 — optimize with evidence:** use the telemetry to find the real bottleneck (likely infer); try the
  240×424 fast path (85 fps) under render load; cached-buffer pointcloud bg; pinned-memory H2D; thread vs
  process for infer.
- **Phase 3 — capture/soak:** NVENC record the rendered output; longer tripwire-guarded soak once short runs are
  clean; compare fully-GPU vs CPU-render arms.

## Safety (non-negotiable, carried from the RealSense work)
RGB+D + continuous inference + render is the **eyes-open** sustained-load envelope (the GB10 xHCI Stop-Endpoint
defect killed the controller 3×). The runner MUST: arm the `hil_common` preflight + journal tripwire, surface
controller health in telemetry every second, **start single-stream and short**, and never run an unattended soak
before short 2-stream runs are clean. Firmware stays on HOLD (5.15.1.55). `/opt/vigil-spark` is read-only — the
test bed *borrows* vigil's logic into a new tree, it does not modify or run the ROS graph.

## Pointers
Probes/modules: `~/realsense-gb10-validation/posebench/{infer,render}/` + `telemetry.py` (+ `render/NOTES.md`).
Proof artifacts: `render/proof.png`, `render/nvenc_test.mp4`. Built on `docs/gb10/GPU-PIPELINE-ARCHITECTURE`,
`CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES`, `HIL-SOAK-AND-ACCEL` §4.
