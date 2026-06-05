# librealsense Consumers Inventory — GB10 (DGX Spark) — 2026-06-05

Read-only audit of every application / code path on this GB10 box that consumes
librealsense, classified by: **needs GB10 rebuild?**, **single vs multistream**, and
**xHCI-controller-death risk on GB10**. No camera, no git, no writes outside this file.

`vigil-spark` (`/opt/vigil-spark`, read-only) is owned by another agent and only noted
here; this inventory covers **everything else**.

---

## 0. The two SDK trees on the box (the rebuild axis)

There are **two complete librealsense toolchains installed**, and which one a consumer
binds to decides whether it is GB10-safe:

| SDK tree | Version | Path | USB tuning / fork patches? | Evidence |
|---|---|---|---|---|
| **GB10 fork (custom)** | **2.58.1** (`v2.58.1-gb10.1-28-g966fdef5a`) | `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/{bin,lib}` | **YES** — `RS2_GB10_USB_TUNING=1`, `FORCE_RSUSB_BACKEND=ON`, CUDA arch 121, NEON, PC-zerocopy, conv-cache | `BUILD_PROVENANCE.json` (build-gb10-full); `ldd` below |
| **apt stock** | **2.57.7** (`ros-jazzy-librealsense2 2.57.7-1noble`) | `/opt/ros/jazzy/bin/*`, `/opt/ros/jazzy/lib/aarch64-linux-gnu/librealsense2.so.2.57` | **NO** — stock upstream, no GB10 USB tuning | `dpkg -l \| grep realsense`; `ldd /opt/ros/jazzy/bin/rs-multicam` → `librealsense2.so.2.57` |

**Rule of thumb for "needs GB10 rebuild?":**
- Anything that resolves to **2.58 fork** = already-rebuilt, USB-tuned → as safe as its stream profile allows. (evidence: `ldd` shows the `/opt/vigil/opt/...gb10/lib` path)
- Anything that resolves to **apt 2.57** = stock, no USB tuning → **must be re-pointed at the 2.58 fork** (via `LD_LIBRARY_PATH`) before any GB10 live use. **The `/opt/ros/jazzy/bin/rs-*` toolset is the trap**: same names as the fork tools but links 2.57.
- Python consumers depend on **which build tree's `pyrealsense2` is on `PYTHONPATH`** — see §6.

The override pattern used everywhere safe on this box (evidence: `launch_depth_only.sh`,
`ros2-launch-depth-minimal.sh`):
```
export LD_LIBRARY_PATH=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib:$LD_LIBRARY_PATH
```

### GB10 multistream danger (the controller-death class)
From the validation corpus (`HIL-MULTISTREAM-2026-06-03.md`, the gb10 scripts' own
docstrings): **concurrent multi-stream (depth+color / +IR / +IMU) at high bandwidth,
start/stop churn, and `hardware_reset` are the configurations that killed the xHCI
controller (incidents #2, #3).** The proven-safe envelope is **single stream, 848×480×30,
one process, serial, no concurrent heavy load.** "Eyes-open" = run only under the
`hil_common` journal tripwire that aborts on the first `-110`/`HC died`.

Marking: **[E]** = evidence (file:line / ldd / dir listing observed this session); **[I]** = inference.

---

## 1. SDK graphical tools & CLI tools (C++, link librealsense2[-gl])

Path: `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/bin/` (GB10 2.58 fork — `ldd` **[E]**)
and a **parallel apt 2.57 copy** at `/opt/ros/jazzy/bin/` (stock — `ldd` **[E]**).

| Consumer | Lang | Needs GB10 rebuild? | Single / Multistream | Controller-risk on GB10 | Notes |
|---|---|---|---|---|---|
| **realsense-viewer** | C++ (gl) | Fork=DONE; **apt copy=YES (re-point)** | **MULTISTREAM (eyes-open)** | **HIGH** | Opens depth+color+IR+IMU by default in the GUI = the exact death config. `ldd` fork→2.58-gl **[E]**, apt→2.57-gl **[E]**. Use only single-stream, tripwire-armed. |
| **rs-depth-quality** | C++ (gl) | Fork=DONE | depth+IR (≥2) → **MULTISTREAM** | MED–HIGH | Quality tool streams depth + IR pair. `ldd`→2.58-gl **[E]**. |
| rs-enumerate-devices | C++ | Fork=DONE | **single / none** (enumerates, no streaming) | **SAFE** | `ldd`→2.58 **[E]**. Control-only; no high-bandwidth stream. |
| rs-fw-update / rs-fw-logger / rs-terminal / rs-dds-* / rs-eth-config | C++ | Fork=DONE | none / control | **SAFE** (fw-update does a reset — see note) | Control / firmware. `rs-fw-update` performs a device reset → minor risk, but not a streaming-bandwidth path. **[E]** present in `bin/`. |
| rs-data-collect / rs-benchmark | C++ | Fork=DONE | **MULTISTREAM** (configurable, bandwidth sweep) | **HIGH** | Benchmark/collect push many profiles. **[I]** from purpose; **[E]** binaries present. |
| rs-convert / rs-rosbag-inspector / rs-record-playback | C++ | Fork=DONE | **file/playback — no live device** | **SAFE** | Operate on `.bag` files, not the camera. |
| **rs-gb10-profiler** | C++ (GB10-only) | Fork=DONE (GB10 bespoke) | profiling — streams | MED | `ldd`→2.58 **[E]**. GB10 profiling tool; scope per-invocation. |
| rs-align / rs-align-gl / rs-pointcloud / rs-color / rs-depth / rs-distance / rs-measure / rs-hdr / rs-motion / rs-infrared etc. | C++ | Fork=DONE | mixed (see §2 — same code as examples) | see §2 | The `rs-*` in `bin/` are the built `examples/` (§2). |

> **Highest-leverage SDK finding:** the apt `/opt/ros/jazzy/bin/realsense-viewer`
> (and the whole `/opt/ros/jazzy/bin/rs-*` set) links **2.57 with no USB tuning** and the
> viewer is **multistream-by-default** — running it directly is the single most likely
> way to kill the controller. Always launch the fork viewer with the `LD_LIBRARY_PATH`
> override (a `gb10-viewer-launch.sh` + `.desktop` already exist in `scripts/gb10/` for this).

---

## 2. Examples (C++ in `examples/`, built into the `rs-*` tools above)

Stream classification **corrected** for `enable_all_streams()` and bare `pipe.start()`
(a bare start opens the device's **default recommended profiles = depth+color+IR = multistream**).

| Example | Needs GB10 rebuild? | Single / Multistream | Controller-risk | Evidence |
|---|---|---|---|---|
| **rs-multicam** | Fork=DONE | **MULTI-DEVICE MULTISTREAM** | **HIGH** | One `pipeline` per device, `enable_all_streams`-style; multi-device is worst-case bandwidth. `multicam.cpp:19,32,46` (vector of pipelines) **[E]** |
| **rs-capture** | Fork=DONE | **MULTISTREAM** | **HIGH** | `cfg.enable_all_streams()` `capture.cpp:25` **[E]** — opens **all** streams. |
| **rs-pointcloud** | Fork=DONE | **MULTISTREAM** (default profiles) | **MED–HIGH** | bare `pipe.start()` `pointcloud.cpp:29` **[E]** → default depth+color. |
| **rs-save-to-disk** | Fork=DONE | **MULTISTREAM** (default profiles) | **MED–HIGH** | bare `pipe.start()` `save-to-disk.cpp:27` **[E]** |
| **rs-align / rs-align-advanced** | Fork=DONE | **MULTISTREAM (depth+color + align)** | **HIGH** | `align.cpp:64-65` enables depth+color **[E]**; align-advanced bare start `:43` **[E]**. Align = the incident-#3 config. |
| **rs-motion** | Fork=DONE | **MULTISTREAM (gyro+accel+motion)** | MED | `motion.cpp:250-256` enables ACCEL+GYRO+MOTION **[E]**. IMU lower-bandwidth than video but still ≥2 streams. |
| **rs-measure** | Fork=DONE | **MULTISTREAM (depth+color)** | **HIGH** | 6 stream refs **[E]** |
| **rs-hdr** | Fork=DONE | **MULTISTREAM** + HDR config churn | **HIGH** | depth pair + HDR sub-preset toggling **[E]** |
| rs-sensor-control / rs-hello-realsense | Fork=DONE | **single / minimal** | LOW | sensor-control = per-sensor manual; hello = single depth print **[I]** (no enable_stream lines) |
| rs-software-device / rs-record-playback | Fork=DONE | **no live device** | **SAFE** | synthetic / file. |

---

## 3. Unit-tests — `unit-tests/live/*` (pyrealsense2 + Catch2; REAL live-camera apps)

These are genuine applications: each needs a **GB10 pyrealsense2 build** to run and many
are **deliberately multistream / stress / hw-reset** — i.e. the exact GB10 danger class.
File counts observed this session **[E]** (`ls` per dir).

| Category (dir) | #tests | Needs GB10 rebuild? | Single / Multistream / Stress | Controller-risk | Evidence / notes |
|---|---|---|---|---|---|
| **hw-reset/** | 5 py | YES (live) | **HW-RESET + STRESS** | **CRITICAL** | `test-stress.py`, `pytest-hub-recycle-imu.py`, `pytest-notifications-callback-gil.py`. hardware_reset + hub-recycle = the incident-#2/#3 trigger. **[E]** |
| **options/** | 9 py | YES | **STRESS** | **HIGH** | `test-set-gain-stress-test.py`, `test-uvc-power-stress-test.py`, `test-drops-on-set.py` — control-transfer storms (the `-110` source). **[E]** |
| **frames/** | 15 py | YES | **MULTISTREAM + START/STOP churn** | **HIGH** | `pytest-pipeline-start-stop.py`, `pytest-fps-permutations.py`, `pytest-fps-performance.py`, frame-drop tests. **[E]** |
| **multi_devices/** | 2 py | YES | **MULTI-DEVICE STREAMING** | **HIGH** | `test-devices-streaming.py`, `test-devices-enumeration.py`. **[E]** |
| **hdr/** | 4 py | YES | **MULTISTREAM + HDR churn** | **HIGH** | `test-hdr-performance.py`, `test-hdr-configurations.py`. **[E]** |
| **camera-sync/** | 1 py | YES | **MULTISTREAM (intra-cam sync)** | **HIGH** | `test-intra-camera-sync.py` — multiple synced streams. **[E]** |
| **syncer/** | 1 cpp | YES | **MULTISTREAM throughput** | **HIGH** | `test-throughput.cpp` (the syncer combines ≥2 streams). **[E]** |
| **d400/** | 14 py | YES | mixed; several **MULTISTREAM/HDR** | MED–HIGH | `test-hdr-long.py`, `test-mipi-motion.py`, emitter on/off, AE convergence. **[E]** |
| **streaming/** | 2 py | YES | format-specific streams | MED | jpeg / y16 format tests. **[E]** |
| **metadata/ image-quality/ calib/ d500/ rec-play/ intrinsics/ extrinsics/ config/ tools/ wrappers/ debug_protocol/ fw/ fw-logs/ dfu/ memory/ algo/** | ~50 py + cpp | YES (live ones) | mostly single-stream / control / file | LOW–MED | Largely single-stream, control, calibration, or file-based. Lower risk. **[E]** counts. |

**Bottom line for unit-tests/live:** ~120+ pyrealsense2/Catch2 tests; **all need a GB10
pyrealsense2 build to execute**, and the `hw-reset`, `options` (stress), `frames`
(start-stop), `multi_devices`, `hdr`, `camera-sync`, `syncer` categories are
**multistream/stress/reset → must be treated as eyes-open / controlled-HIL-only on GB10.**
Do NOT run the live suite unattended on GB10.

---

## 4. GB10 HIL / validation scripts (Python+bash; pyrealsense2) — the bespoke consumers

Two copies: `scripts/gb10/` (in-repo, canonical) and `~/realsense-gb10-validation/bin/`
(operational). Both import `pyrealsense2`. **[E]** docstrings read this session.

| Script | Single / Multistream / Stress | Controller-risk | Notes (from its own docstring **[E]**) |
|---|---|---|---|
| **rs-gb10-hil-multistream.py** | **DUAL depth+color@60 + align** | **CRITICAL (self-labeled "DANGEROUS — can kill the USB controller")** | The incident-#3 config, intentional. Eyes-open + tripwire only. |
| **rs-gb10-soak.py** | **phased: single→dual+align→start/stop churn→3-stream** | **CRITICAL (self-labeled DANGEROUS)** | Long-soak; aborts on first `-110`/`HC died`. |
| **rs-gb10-churn-test.py** | context/pipeline **destroy+recreate churn** (dual) | **CRITICAL** | Reproduces death-#2; guarded by `RS2_GB10_REFUSE_REACQUIRE=1` (P7 refuses re-acquire). |
| **rs-gb10-stress-matrix.py** | **concurrent depth+color+IR @60–90** matrix | **CRITICAL** | Explicit DANGER_RE kernel-tripwire built in. |
| **rs-gb10-hil.py / .sh** | default **SINGLE-STREAM**; dual gated behind `--allow-dual` | LOW (default) / CRITICAL (--allow-dual) | Backend-aware (rsusb/v4l2); USB-2 guard + controller-alive precheck. **Safe by default.** |
| rs-gb10-hil-advanced.py / rs-gb10-hil-suite.py / rs-gb10-quality-hil.py | quality-hil = **SINGLE depth+color** ("no controller risk") | LOW | Single-stream quality (PSNR/SSIM/NVENC). |
| rs-gb10-keepongpu-py.py / rs-gb10-keepongpu-viewer.cpp | **SINGLE depth** (GL keep-on-GPU, no D2H) | LOW–MED | Needs a **gl** build (py3.14 / build-gb10-gl); py3.12 HIL build lacks `rs.gl`. **[E]** docstring. |
| rs-gb10-gpu-pipeline.py / rs-gb10-pc-zerocopy.py / rs-gb10-p7-confirm.py | depth (+pc) | LOW–MED | GPU pipeline / zero-copy validators. |
| rs-gb10-fw-update.py / rs-gb10-trt-probe.py / rs-gb10-conv-cache-bench.py / rs-gb10-align-bench.py | control / single / bench | LOW–MED | align-bench touches align (≥2 streams) → MED. |
| rs-gb10-ros2-hil.py / rs-gb10-nonheadless-verify.py | depth (ROS2 path / display) | LOW–MED | |
| rs_gpu_preview.py | helper (synthetic selftest; consumes frames passed in) | NONE standalone | cv2.cuda colorize/resize; not a camera opener itself. **[E]** |

All of these **need a GB10 pyrealsense2 build on `PYTHONPATH`** (they run under
`realsense-gb10-env` / a build's `Release/`). They are GB10-aware by construction and the
dangerous ones ship their own tripwire.

---

## 5. ROS2 — `realsense2_camera` consumer

Workspace: `~/realsense-gb10-validation/ros2-ws` (colcon; `src/realsense-ros`).

| Consumer | Lang | Needs GB10 rebuild? | Single / Multistream | Controller-risk | Evidence |
|---|---|---|---|---|---|
| **realsense2_camera node** (`librealsense2_camera.so`) | C++ | **node built; links the 2.58 fork at runtime via LD_LIBRARY_PATH override** | **node default = MULTISTREAM (depth+color+IR+IMU)** | **CRITICAL by default** | `ldd librealsense2_camera.so` → `librealsense2.so.2.58 → /opt/vigil/opt/...gb10/lib` **[E]** |
| `launch_depth_only.sh` / `ros2-launch-depth-minimal.sh` | bash launch | uses fork via `LD_LIBRARY_PATH` **[E]** | **SINGLE depth 848×480×30** (all other streams forced off) | **LOW (proven-safe)** | depth-minimal is **HIL-PROVEN 2026-06-05: 708 frames, 0 drops, controller GREEN** (docstring **[E]**) |
| stock `ros2 launch realsense2_camera rs_launch.py` (defaults) | bash | — | **MULTISTREAM all-streams** | **CRITICAL — do not run on GB10** | Launch scripts explicitly warn: default all-streams kills the xHCI controller. **[E]** |

**Key:** the node itself is fine (links the fork); the danger is the **default launch config**.
The two `*depth-only*` / `*depth-minimal*` launch wrappers are the safe, proven path. The
stock `rs_launch.py` defaults are multistream and must never be run directly on GB10.

> Note: apt `ros-jazzy-librealsense2 2.57.7` is installed and is exactly what these
> launch scripts **override** with the `LD_LIBRARY_PATH` prefix so the node binds 2.58, not 2.57. **[E]**

---

## 6. Python wrappers & pyrealsense2 importers (`wrappers/python`, examples)

`pyrealsense2` is built per-tree. Builds present **[E]**: `build-gb10-full` (gl+cuda),
`build-gb10-nocuda`, `build-gb10-gl`, `build-gb10-py313`, `build-gb10-py314` (gl),
plus the installed SDK `lib/python3.12`. **Which build's `pyrealsense2` is on `PYTHONPATH`
decides GB10-safety** (the py3.12 HIL build has examples/gl OFF; py3.14 has gl — **[E]**).

| Consumer (`wrappers/python/examples/…`) | Needs GB10 build? | Single / Multistream | Controller-risk | Notes |
|---|---|---|---|---|
| **box_dimensioner_multicam/** (`box_dimensioner_multicam_demo.py` + `realsense_device_manager.py`) | YES (pyrealsense2) | **MULTI-DEVICE MULTISTREAM** | **HIGH** | Device-manager opens **all connected D4xx** with depth+IR/color. Worst-case bandwidth. **[E]** dir listing. |
| **pyglet_pointcloud_viewer.py** / **opencv_pointcloud_viewer.py** | YES | **MULTISTREAM (depth+color)** | **MED–HIGH** | Pointcloud viewers open depth+color. **[I]** (standard rs pointcloud pattern) |
| align-depth2color.py | YES | **MULTISTREAM (depth+color+align)** | **HIGH** | Align config. **[E]** present |
| opencv_viewer_example.py / python-tutorial-1-depth.py | YES | single (depth) / minimal | LOW | **[E]** present |
| frame_queue_example.py / embedded_filters.py / export_ply_example.py | YES | single–dual | LOW–MED | |
| read_bag_example.py / numpy_to_frame.py / align-with-software-device.py | YES | **no live device** (bag / synthetic) | **SAFE** | file/synthetic |
| python-rs400-advanced-mode / d500_triggered_calibration / depth_*calibration | YES | control / single | LOW | calibration/control |
| **tensorflow/tools/convert_to_bag.py** | YES (pyrealsense2) | **no live device** (converts to `.bag`) | **SAFE** | offline conversion. **[E]** present |
| Other wrappers (opencv / pcl / open3d / openvino / unity / unreal / csharp / matlab) | would need fork build to link 2.58 | mixed | varies | **Not separately built on this box** beyond python; listed for completeness **[E]** (dir listing). |

---

## 7. posebench test bed (`~/realsense-gb10-validation/posebench`)

| Consumer | Lang | Needs GB10 build? | Single / Multistream | Controller-risk | Evidence |
|---|---|---|---|---|---|
| **posebench** (`run_testbed.py`, `infer/depth_lift.py`, `telemetry.py`, `infer/test_depth_lift.py`) | Python | **YES** — runs under `LD_LIBRARY_PATH=build-gb10-full/Release` **[E]** | **RGB+D (2-stream) eyes-open** when integrated | **MED–HIGH (Phase-1)** | README: "RGB+D + infer + render is 2-stream/eyes-open (GB10 xHCI death risk) — arm the `hil_common` tripwire." **[E]** Phase-0 stages all proven in isolation; capture stage is the camera opener. |

---

## 8. Other /opt consumers found by sweep (`ldd … | grep librealsense2`) **[E]**

| Path | What | Lib version | Notes |
|---|---|---|---|
| `/opt/ros/jazzy/bin/rs-*` (full toolset) + `…/realsense-viewer` | apt 2.57 SDK tools | **2.57** | Stock, **no USB tuning** — the trap set (§1). |
| `/opt/ros/jazzy/.../librealsense2_camera` (ros pkg) | apt realsense2_camera | 2.57 | Overridden by the workspace build + LD override; do not invoke stock. |
| `/opt/vigil/build/librealsense-…gb10/_out/librs2driver.so` | GB10 fork driver shim | 2.58 | Build artifact of the fork. |
| `/opt/vigil/build/…gb10/gb10-smoke/smoke` | GB10 build **smoke test** binary | 2.58 | Links the fork; smoke check. |
| `/opt/vigil/build/…gb10/Release/realsense-viewer` + `pyrealsense2.cpython-312…so` | fork build-tree copies | 2.58 | Same as installed SDK; build-tree originals. |
| `/opt/vigil-spark/…` | **OWNED BY OTHER AGENT** | — | Noted, not enumerated here per task scope. |

---

## Summary — counts per class & GB10 verdict

| Class | # consumers | Need GB10 rebuild / re-point? | Multistream / controller-risk count |
|---|---|---|---|
| SDK tools (fork bin/) | ~50 binaries | Fork=DONE; **apt `/opt/ros/jazzy/bin/` copies need re-point** | viewer, depth-quality, data-collect, benchmark = HIGH |
| Examples (C++) | ~20 | Fork=DONE | **≥9 MULTISTREAM** (capture/multicam/align/measure/hdr/pointcloud/motion/save-to-disk/align-advanced) |
| unit-tests/live | ~120+ tests | **ALL need GB10 pyrealsense2 build** | **7 categories** multistream/stress/reset (hw-reset, options-stress, frames-churn, multi_devices, hdr, camera-sync, syncer) = CRITICAL/HIGH |
| GB10 HIL scripts | ~25 | All need GB10 build (by design) | **4 self-labeled DANGEROUS** (hil-multistream, soak, churn, stress-matrix) |
| ROS2 | 1 node + 3 launchers | Node links fork; **stock launch defaults = CRITICAL** | node default MULTISTREAM = CRITICAL; depth-only/minimal = SAFE (HIL-proven) |
| Python wrappers / examples | ~20 + box_dimensioner | Need GB10 pyrealsense2 (per build tree) | box_dimensioner (multi-device), pointcloud viewers, align = HIGH |
| posebench | 1 testbed | YES (build-gb10-full) | RGB+D eyes-open = MED–HIGH |

### SAFE on GB10 (run freely, single-stream or no-device)
- rs-enumerate-devices, rs-fw-logger/terminal/dds/eth (control); rs-convert, rs-rosbag-inspector, rs-record-playback (file).
- `ros2-launch-depth-minimal.sh` / `launch_depth_only.sh` (**HIL-PROVEN single depth, 0 drops, controller GREEN**).
- `rs-gb10-hil.py` default matrix (single-stream), `rs-gb10-quality-hil.py` (single).
- Bag/synthetic python examples (read_bag, convert_to_bag, numpy_to_frame, software-device).

### REQUIRE the eyes-open envelope (controlled HIL + tripwire only)
- **realsense-viewer** (multistream-by-default; *especially* the apt 2.57 copy — re-point to fork first).
- All multistream examples (capture/multicam/align/measure/hdr/pointcloud/motion).
- unit-tests/live: hw-reset, options-stress, frames start-stop, multi_devices, hdr, camera-sync, syncer.
- gb10 DANGEROUS scripts (hil-multistream, soak, churn, stress-matrix) — already self-guarded.
- Stock `ros2 launch … rs_launch.py` defaults — **never run directly on GB10**.
- box_dimensioner_multicam (multi-device), posebench Phase-1 (RGB+D).

### Single highest-value "app worth making GB10-safe"
**The `realsense2_camera` ROS2 node** (`~/realsense-gb10-validation/ros2-ws`, links the
2.58 fork). It is the real integration target for `vigil`-class robotics, it is the only
consumer with a **proven** GB10-safe path (`ros2-launch-depth-minimal.sh`: 708 frames,
0 drops, controller GREEN, 2026-06-05), and the work to make its **multi-stream** config
safe (depth+color/IMU without controller death) is exactly the GB10 USB-tuning frontier.
A close runner-up is **posebench** (the pose pipeline) — but it depends on the same
RGB+D multi-stream safety the ROS node needs. Hardening the ROS node's multistream
envelope unblocks both.

---
*Evidence captured 2026-06-05 via `ldd`, `dpkg -l`, dir listings, and source/docstring
reads. `[E]` = observed this session; `[I]` = inferred from standard librealsense usage
patterns. No camera opened, no git operations, read-only except this file.*
