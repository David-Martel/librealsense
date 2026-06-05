# GB10 — ROS2 integration, keep-on-GPU GL, and pinned-memory (parallel-agent results, 2026-06-05)

Three specialist agents ran in parallel (offline-only; all live HIL serialized by the parent; warnings-as-
errors enforced on changed files). Results below; reproducible artifacts under `~/realsense-gb10-validation/`.

## 1. ROS2 `realsense2_camera` — BUILDS against the GB10 SDK ✅ (Jazzy; see §4 for the Lyrica/py3.13 re-target)
`realsense-ros` tag **4.58.1** (matches librealsense 2.58.x) colcon-builds clean (exit 0, **zero compiler
warnings**) against the installed GB10 SDK at `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/`. Binding:
`-Drealsense2_DIR=<sdk>/lib/cmake/realsense2` + `CMAKE_PREFIX_PATH=<sdk>`. The node links
**`librealsense2.so.2.58`** (the GB10 SDK SONAME) — distinct from the apt package's `.so.2.57`, so the loader
can never cross-resolve them. `ros2 component types` shows `realsense2_camera::RealSenseNodeFactory`; `ldd`
confirms the GB10 `.so`. Workspace: `~/realsense-gb10-validation/ros2-ws/`; **safe depth-only launch**
(`launch_depth_only.sh`: `enable_color:=false enable_gyro:=false enable_accel:=false
depth_module.depth_profile:=848x480x30` = single-stream conservative-safe envelope; default all-stream is
eyes-open/lethal on GB10). Parent runs the first live launch. *(Built on ROS2 **Jazzy** + py3.12 — the
current host; re-target plan in §4.)*

## 2. Keep-on-GPU OpenGL chain — REAL WIN for GPU/render consumers ✅ (scales with resolution)
A GL-resident chain `gl::colorizer (GL texture) → draw to FBO → final readback` avoids librealsense's
synchronous `get_data()` D2H (and the pipeline stall it forces). Measured (synthetic, `software_device`,
correctness max|Δ| 3/255 vs CPU colorize; reproduced by parent):

| resolution | **keep-on-GPU saving (B−A)** | breakdown |
|---|---|---|
| 640×480 | **~1.0 ms/frame** | D2H 0.49 + H2D 0.20 + sync stall |
| 1280×720 | **~2.9–3.3 ms/frame** | grows with pixels |
| 1920×1080 | **~7.0 ms/frame** | sync-stall-dominated |

**Crucial scope:** the win is for a **GPU/render consumer** (display FBO, NVENC, CUDA interop) that never
needs the image on the host — a **CPU** consumer must pay the D2H regardless and gains nothing. This is
exactly the test-bed's render path → **recommended follow-on: route the test-bed colorize→render through the
GL-resident chain** to reclaim ~3 ms/frame at 720p. No SDK source change needed (uses the `rs2_gl_*` C API;
needs `librealsense2-gl.so`, built via `-DBUILD_GLSL_EXTENSIONS=ON -DBUILD_PC_STITCHING=ON`). Harness:
`~/realsense-gb10-validation/posebench/gl/keep_on_gpu_bench.sh`. Caveat: GL processing-lane teardown SIGSEGVs
(known) — harness `_exit(0)`s after results; a production integration must handle GL teardown.

## 3. Pinned host memory — DROPPED ❌ (actively harmful on GB10 coherent memory)
Adding `cudaHostAlloc` pinned staging to the cached pools measured **2.4× SLOWER** (848×480: 0.118→0.281
ms/call; 1280×720: 0.268→0.652). On GB10's coherent unified memory, pageable `cudaMemcpy` already runs at
near-DMA speed (no bounce buffer), so pinned staging adds a **redundant host-to-host copy** rather than
removing one. Byte-identical + warnings-clean, but **dropped — not even kept gated** (zero-payoff maintenance
surface). This is the **third** confirmation of the GB10 shared-memory lesson: *the lever is eliminating
allocation churn, not touching copies* (managed memory wasn't faster either; cached-device pageable is the
right operating point — the promoted default).

## 4. ROS2 re-target: latest ROS2 ("Lyrica") + Python ≥3.13 (vigil-spark direction)
The §1 integration is **Jazzy + py3.12** (this host's stack). vigil-spark will target the **latest ROS2
(2026 L-release) + Python ≥3.13**. Honest constraints + plan:
- **ROS2↔Ubuntu LTS coupling:** ROS2 LTS distros pin to Ubuntu LTS (Jazzy→24.04). The 2026 L-release LTS
  targets **Ubuntu 26.04**, not this 24.04 host; the newest ROS2 *installable on 24.04* is **Kilted** (2025).
  So a full latest-ROS2 build needs either Ubuntu 26.04 or a source build of the L-release on 24.04.
- **Python ≥3.13 is achievable here** (the gb10-cuda stack already provisions a uv-managed py3.14 venv). The
  GB10 SDK's pyrealsense2 is currently built for **cpython-3.12**; targeting py3.13/3.14 requires a
  **pyrealsense2 rebuild** against that interpreter (the build script's `PYTHON_EXECUTABLE` + the FindPython
  venv pins handle this — same ABI-trap guard already in `scripts/build-dgx-spark-gb10.sh`).
- **Plan:** (a) build a py3.13+/cpython pyrealsense2 against the target interpreter (feasible now); (b) rebuild
  `realsense2_camera` against the target ROS2 once installed (Kilted on 24.04 now, or the L-release on 26.04);
  (c) the C++ SDK + CUDA work is interpreter/ROS-independent and carries over unchanged.
