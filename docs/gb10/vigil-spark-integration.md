# GB10 librealsense fork ↔ vigil-spark integration analysis

**Date:** 2026-06-05
**Fork:** `~/dev/repos/librealsense` (David-Martel/librealsense `master`, tag `v2.58.1-gb10.1`)
**Target deployment (READ-ONLY, analyzed not modified):** `/opt/vigil-spark`
**Scope:** static reading only — no camera access, no vigil run, no git ops. This is a recommendation
document; actual vigil changes are out of scope (the user's call).

**Evidence vs inference labels:** lines tagged `[E]` are evidence read directly from source this session
(file:line cited); `[I]` are inference/reasoning from that evidence; `[M]` are facts carried from the
prior posebench validation memory (separately HIL-proven, not re-verified this session).

---

## 1. vigil's current RealSense usage (evidence)

**Sole camera owner:** `vigil_ros_ws/src/sensors/sensors/realsensenode.py` — a custom ROS2 `Node`
(`RealSenseNode`). It is the one node that opens the device. `[E]`

| Aspect | Finding | Evidence |
|---|---|---|
| **API** | `pyrealsense2` **direct** (NOT the `realsense2_camera` ROS2 wrapper) | `import pyrealsense2 as realsense` — realsensenode.py:8 `[E]` |
| **Python** | **3.12** (`requires-python = ">=3.12"`, `python_version = "3.12"`) | `pyproject.toml` `[E]`; runtime interp `venv-jazzy-aarch64` = CPython 3.12.3 `[M]` |
| **Streams** | **2 streams**: color 640×480 `bgr8`@30 + depth 640×480 `z16`@30 | realsensenode.py:52–53 `[E]` |
| **Publish rate** | timer at param `fps` (default **15**), independent of the 30 fps device profile | realsensenode.py:35–40, 70 `[E]` |
| **Align** | `rs.align(rs.stream.color)` built once (line 63), `alignment.process(frames)` **every frame** in `publish_image` | realsensenode.py:63, 84 `[E]` |
| **Pointcloud** | **None.** No `rs.pointcloud()` anywhere in the camera node | grep: no `rs.pointcloud`/`calculate` in realsensenode.py `[E]` |
| **Recording** | **None.** Color is `cv2.imencode('.jpg', …, QUALITY 80)` for ROS transport; depth shipped as raw `z16` byte array. No video file / NVENC | realsensenode.py:152, 200, 224 `[E]` |
| **Post-proc filters** | **None** in the camera node (no decimation/spatial/temporal/hole-filling) | grep: no `rs.*_filter` in realsensenode.py `[E]` |
| **Threading** | one `threading.Thread(target=rclpy.spin)` per device; single device expected | realsensenode.py:286–289; comment line 22 "we expect only 1" `[E]` |
| **Failure handling** | on `wait_for_frames` exception → `restart_pipeline()` (stop, 0.5 s sleep, re-`start`); on restart failure → `destroy_node()` | realsensenode.py:90–144 `[E]` |

**Downstream topology (consumers do their own depth math — they do NOT re-open the camera):** `[E]`
- `realsensenode` publishes `/realsense/color/image_raw` (`FrameMessage`, JPEG + intrinsics JSON) and
  `/realsense/depth/image_raw` (`DepthMessage`, raw `z16` + intrinsics fields).
- Consumers: `pose_estimator` (subscribes both; demo stub publishes intrinsics — pose_estimator.py:65),
  `SAM/samnode` (both — samnode.py:65–66), `generalist_tracker` (depth — line 33, scales by
  `depth_scale` line 176), `projection` (color — line 65).
- `SAM`, `pose_estimator` `import pyrealsense2` **only for the intrinsics struct / per-pixel deproject
  math** (`SAM/Intrinsics`, samnode.py:14,135), NOT to stream. `[E][I]`

**Second-camera-open risk (latent, must-verify-before-deploy):** the vendored external integration
`external_systems/.../UMichProjection/ros/local/realsense_reader.py` and
`.../subscriptions/camera_reader/realsense.py` each call `pipeline.start()` and open their **own**
depth+color+align (realsense_reader.py:31–43). `[E]` Evidence says these are **NOT in the active launch
path**: `UMichProjection/ros/projection_node.py` has all `rclpy`/Node imports commented out
(projection_node.py:2–6) and the files live under `ros/local/` + `subscriptions/camera_reader/`. The
active `projection` node (`vigil_ros_ws/src/projection/projection/projection.py`) subscribes to the
color **topic**, it does not open the camera (projection.py:65). `[E]` **Verdict:** not active per
evidence, but a 2-process camera open is the one thing the fork says *never* to do (§3) — confirm before
any deploy.

---

## 2. Integration map — per GB10 customization

vigil's structure constrains the fit far more than a feature checklist would suggest. The honest result:
**two customizations are transparent high-value wins via a single SDK swap; the rest do not apply to
vigil as currently built** (they would only matter under a future re-architecture or migration).

| # | GB10 customization | vigil need? | How adopted | Concrete integration point | Risk / effort |
|---|---|---|---|---|---|
| 1 | **USB/controller-death mitigations** (`RS2_GB10_USB_TUNING`: deeper URB pool, gentler stop, re-acquire guard) | **YES — critical** | Drop-in (SDK swap; no vigil code change) | the `pyrealsense2` + `librealsense2.so` that vigil's 3.12 interp resolves | Low effort; high value |
| 2 | **CUDA `rs.align` depth→color** (auto-selected when `BUILD_WITH_CUDA=ON`) | **YES — high value** | Drop-in (transparent: same `rs.align` API) | realsensenode.py:84 `alignment.process(frames)` — runs **every frame** | Low effort; high value |
| 3 | **CUDA pointcloud cached pools** (`RS2_PC_MODE=1`, 3.3×) | **No** | n/a | vigil never calls `rs.pointcloud()`; downstream uses per-pixel deproject math | — |
| 4 | **CONV cache** (YUYV→color, `RS2_CONV_MODE=1`) | **Marginal** | Drop-in if present | vigil requests `bgr8`/`z16`, not YUYV; conversion is ~NEON-parity anyway | Negligible effect |
| 5 | **Keep-on-GPU GL render** (`rs.gl`, ~3 ms/frame, no D2H) | **Low fit** | Would need re-architecture | render (`pose_estimator scene_render`, line 383) is a **separate process** from capture, with **CPU JPEG** between them | High effort; blocked by IPC boundary |
| 6 | **ROS2 depth-minimal config** (`realsense2_camera`, #26 SOLVED) | **Reference only** | n/a today | vigil uses pyrealsense2 **direct**, not the ROS2 wrapper | Only relevant if vigil migrates to the wrapper |
| 7 | **NVENC cq=23 GPU encode** | **No today** | n/a | vigil JPEG-encodes per-frame for transport; records no video | — |
| 8 | **py3.13 / py3.14 SDK trees** (`LRS_PY_TAG`) | **Future** | Rebuild SDK per interpreter | vigil's stated target is ROS2 L-release + py≥3.13 | Medium; ready when vigil migrates |

### The two that matter — detail

**#1 USB mitigations (drop-in, critical).** vigil is structurally a **2-stream** consumer
(color+depth, realsensenode.py:52–53) `[E]` and **cannot drop to single-stream** — downstream needs both
(SAM/projection consume color; pose/tracker/SAM consume depth) `[E][I]`. 2-stream is exactly the
*eyes-open* zone the fork's safe envelope warns about (§3). The USB mitigations are what let the
2-stream posebench RGB-D pipeline run controller-GREEN with zero `-110`/HC-died `[M]`. Adopted purely by
swapping the SDK vigil loads — **no vigil code change**.

**#2 CUDA align (drop-in, transparent, measured).** vigil calls `rs.align(rs.stream.color).process()`
every frame (realsensenode.py:63,84) `[E]`. `rs.align` auto-dispatches CUDA when
`rs2_is_gpu_available()`, else NEON, else scalar — the **same API call** moves CPU→GPU with no code
change `[E:` align-bench header + benchmarks.md:195`]`. Measured on this fork at vigil's exact profile:
**CUDA 0.293–0.295 ms/call vs CPU(NEON+OpenMP) 4.33–5.67 ms/call = 15–19× faster**, isolated by a build
that differs from the CPU baseline *only* in `BUILD_WITH_CUDA` (so the CPU side is not crippled)
`[E: benchmarks.md:22,191–197; rs-gb10-align-bench.py:16]`. At vigil's 15 fps timer this is ~4–5 ms of
per-frame CPU returned to the (CPU-bound, py3.12) node on every publish. `[I]`

> **Discrepancy reconciled:** README.md:18 ("`rs.align` is already cached upstream — no flag") refers to
> *buffer pooling*, a separate axis from CUDA dispatch. The 15–19× is GPU-vs-CPU compute, realized
> transparently by the CUDA build. Both are true; they are not the same thing. `[E][I]`

### What "drop-in" actually requires (deployment, not code)

No vigil *source* change — but it **is** a deployment/packaging change `[I]`:
- The GB10-built `pyrealsense2*.so` + `librealsense2.so.2.58` + CUDA runtime must be what vigil's **3.12**
  interpreter resolves (via `PYTHONPATH` + `LD_LIBRARY_PATH` to the build Release). This is exactly the
  ABI through-line the posebench test bed already proved in the *same* `venv-jazzy-aarch64` 3.12.3
  interpreter `[M]`.
- The GB10 `.so` is SONAME `.so.2.58`, **distinct from the apt 2.57** `[M]` — it co-installs without
  clobbering the system package.
- vigil already vendors a `qobi/pyrealsense2/` package that simply re-exports a dropped-in binary
  (`from .pyrealsense2 import *`, qobi/pyrealsense2/__init__.py) `[E]` — a pre-existing swap mechanism /
  precedent for substituting the SDK binary.

**Present vs future, kept separate:** present = the existing `build-gb10-full` (CUDA + USB tuning,
py3.12) drops into vigil's current 3.12 runtime today `[M]`. Future = the py3.13/3.14 trees are built and
ready for vigil's stated ROS2-L / py≥3.13 migration target `[M]`, requiring an SDK rebuild per
interpreter (FindPython venv pins handle it).

---

## 3. Robustness assessment of the GB10 fork (for a vigil dependency)

**Production-ready for the two recommended wins (USB + CUDA align)?** Largely yes, with explicit caveats.
Both are `#if`-guarded and byte-identical to upstream when off, the cached ladders verified max-abs-diff 0,
and the CUDA-align path is the stock reference call (low surface) `[E: README.md:15–18; benchmarks.md]`.
The R1 hardening fix replaced `assert()` (stripped under `-DNDEBUG` in Release) with `cuda_or_throw()` on
the now-default cached paths `[M]`. Build provenance is captured per-binary (`BUILD_PROVENANCE.json`,
tag `v2.58.1-gb10.1`) so a vigil deployment can pin and reproduce an exact SDK `[E: README.md:60–73]`.

**Gaps / risks before vigil depends on it:**

1. **2-stream is *eyes-open*, mitigations reduce but do not eliminate the risk.** The underlying xHCI
   controller-death is an NVIDIA defect; `RS2_GB10_USB_TUNING` shrinks the trigger surface, it does not
   remove it `[E: README.md:75–79]`. vigil is unavoidably 2-stream (§1) and runs long sessions. **Claim
   "safer," never "safe."** 2-stream **long-soak** at vigil's duty cycle is not yet proven (posebench
   live runs were 12–15 s) `[M]`. Recommend a controlled 2-stream soak at vigil's profile before vigil
   depends on it.
2. **Firmware is on HOLD (5.15.1.55), downgrade device-blocked** `[E: README.md ref; M]`. The fix is
   software-side only; no firmware remedy is available.
3. **`rs.gl` keep-on-GPU only exists in GLSL+examples builds.** Needs
   `-DBUILD_GLSL_EXTENSIONS=ON -DBUILD_PC_STITCHING=ON` to produce `librealsense2-gl.so`, and carries a
   known teardown-order SIGSEGV (root-caused; fix = `rs2_gl_shutdown_processing()` before context
   destroy) `[M]`. Not in the default `build-gb10-full`. Irrelevant to the two recommended wins, but
   blocks customization #5 if ever pursued.
4. **cv2-CUDA / OpenCV split.** The CUDA-OpenCV link is 4.14 from `/opt/gb10-cuda`; vigil's own cv2 is
   stock 3.12-era `[E: build script:56–63; M]`. The recommended wins do **not** touch cv2, so this is not
   a blocker for them — but any future CUDA-OpenCV adoption in vigil would need that runtime aligned.
5. **Single-camera envelope.** The whole safety model assumes one camera, one process. The latent
   UMichProjection second-open (§1) must be confirmed dormant before deploy.
6. **CONV cache marginal for vigil** — vigil requests `bgr8`, not YUYV, so the conversion-cache path is
   largely not exercised; harmless but not a win `[E][I]`.

**Net:** the USB+CUDA-align pair is the mature, low-surface core of the fork and is appropriate for vigil
to depend on, *gated on a 2-stream soak at vigil's profile*. The GL/NVENC/pointcloud/ROS2-wrapper pieces
are real and measured but do not fit vigil's current architecture and are not on the critical path.

---

## 4. Recommended adoption path (phased; honors one-camera envelope + READ-ONLY)

> Recommendation only. No vigil files are changed by this document. Every phase keeps the single-camera,
> single-process envelope; vigil stays structurally 2-stream (it must) — the mitigations make that safer,
> not safe.

**Phase 0 — confirm the latent risk is dormant (no deploy).** Verify the UMichProjection
`realsense_reader` second-camera-open path is not reachable in vigil's active launch (§1 evidence says it
is not). A 2-process camera open is the one prohibited action.

**Phase 1 — SDK swap on the current 3.12 interpreter (lowest-risk, highest-value).** Point vigil's 3.12
runtime at the `build-gb10-full` SDK (CUDA + USB tuning) via `PYTHONPATH`/`LD_LIBRARY_PATH` (or via the
existing `qobi/pyrealsense2/` drop-in). **Zero vigil code change.** This buys, transparently:
- USB controller-death mitigations on vigil's unavoidable 2-stream config (#1);
- CUDA align 15–19× on the per-frame `process()` call (#2, realsensenode.py:84).

Validate with `just gb10-doctor` (toolchain/SDK/CUDA/controller/USB-3 preflight) and a **controlled
2-stream HIL soak at vigil's exact profile** (color+depth 640×480, vigil's session duration), tripwire on
`HC died`, before vigil is allowed to depend on it.

**Phase 2 — migration readiness (when vigil moves to ROS2-L / py≥3.13).** Rebuild the SDK against the
target interpreter using the existing py3.13/3.14 trees; the same two wins carry forward. If vigil ever
migrates the camera node to the `realsense2_camera` ROS2 wrapper, the depth-minimal config (#26 SOLVED)
becomes the drop-in reference at that point.

**Phase 3 — opportunistic, only if vigil's architecture changes.** Keep-on-GPU GL (#5) becomes worth it
only if capture+render are colocated in one process without the CPU-JPEG hop; NVENC (#7) only if vigil
adds video recording; pointcloud cache (#3) only if vigil switches to `rs.pointcloud`. None are on the
critical path.

---

### Bottom line
vigil's single custom node (`realsensenode.py`) opening a 2-stream pyrealsense2 pipeline on py3.12, with a
per-frame CPU align and no pointcloud/record, is an **ideal fit for exactly two of the fork's
customizations** — USB mitigations and transparent CUDA align — both delivered by a single SDK swap with
no source change, and both directly addressing vigil's two real exposures (2-stream controller-death risk
and per-frame align cost). The remaining customizations are sound but do not match vigil as currently
built. Phase-1 = the SDK swap on the current 3.12 interpreter, gated on a 2-stream soak.
