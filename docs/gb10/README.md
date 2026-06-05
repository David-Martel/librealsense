# GB10 / DGX Spark — librealsense findings, tooling, and build options

Index for the David-Martel librealsense fork's NVIDIA DGX Spark (GB10, aarch64, CUDA 13) work:
USB-controller hardening, CUDA/GPU acceleration of the capture→process→render pipeline, and a
RealSense→3D-pose→render test bed. All findings are HIL-measured on host spark-3066 (D435, FW 5.15.1.55).

**Current master HEAD: `e657daba0` (tag `v2.58.1-gb10.1`). Run `just build-tag` for the live state.**

## Opt-in build defines (all OFF upstream = byte-identical; gate via `scripts/build-dgx-spark-gb10.sh`)
| CMake define | env (build script) | runtime | effect |
|---|---|---|---|
| `RS2_GB10_USB_TUNING` | `LRS_GB10_USB_TUNING` (default 1) | — | P2 deeper URB pool + P4 gentler stop + P7 re-acquire guard (multistream xHCI mitigations) |
| `RS2_GB10_PC_ZEROCOPY` | `LRS_GB10_PC_ZEROCOPY` (default **1**) | `RS2_PC_MODE` (default **1**) | pointcloud cached device buffers → **3.3× faster, > NEON** (mode 2 = managed, slower; mode 0 = baseline) |
| `RS2_GB10_CONV_CACHE` | `LRS_GB10_CONV_CACHE` (default **1**) | `RS2_CONV_MODE` (default **1**) | YUYV→color cached buffers → kernel 5×, **end-to-end ~NEON-parity** (mode 0 = malloc baseline) |
Cached ladders are **PROMOTED TO DEFAULT** on the GB10 build (mode 1) — byte-identical to baseline
(verified max-abs-diff 0), `#if`-guarded so an upstream build is still byte-identical, and process-static
pools leak at exit by design (no static-teardown `cudaFree` → no shutdown crash). `RS2_*_MODE=0` opts back
to the per-frame-malloc baseline. `rs.align` is already cached upstream (no flag — it's the reference impl).

## Findings docs

Docs dated 2026-06-03 are **historical snapshots** from the initial analysis session; their measured data
(xHCI root cause, per-op benchmarks, HIL results) remains valid but some open-item claims have since been
resolved — see the banner at the top of each where applicable, and the current-state docs below.
Living/current docs have no date suffix.

| doc | topic | status |
|---|---|---|
| [accel-validation](accel-validation-2026-06-05.md) | **CURRENT — live CUDA-vs-CPU validation:** pointcloud **1.8×** (cached pools flipped it from 0.57× regression to a win), align 15–19×, keep-on-GPU render 3.43ms no-D2H = meaningful; colorize/conversion = honest parity. + enhanced non-headless live PASS + reliability-fix status | current |
| [reliability-audit](reliability-audit-2026-06-05.md) | **CURRENT — libusb error/recovery-path audit** (10 findings; 3 fixes applied: errno→sts classification, null-guard event-thread cb, catch-by-ref P7 reason) + 6 prioritized camera-HIL reliability tests | current |
| [gb10-hardening-optimization-proposals](gb10-hardening-optimization-proposals-2026-06-05.md) | **CURRENT — ranked hardening/perf proposals (H1–H10) + status.** LANDED + HIL-proven: single-opener lock, H1 safe-stop, F5 atomic, F6 NDEBUG-safe teardown. NEXT: H3 wedge-watchdog, **H4 reconfigure-without-stop**, H6/H7/H8/H9/H10, lock fail-fast variant | current — **the next-steps roadmap** |
| [vigil-realsense-reliability](vigil-realsense-reliability-2026-06-05.md) | **CURRENT — vigil RealSense bug/race smoke-out.** >2-stream verdict = **NO** (airtight: only color+depth 640×480, no IR/IMU/HDR/record/multi-cam); bugs B1 (uncapped stop→start churn = recovery triggers the death), B3 (cross-thread double-stop), B2 (`__restarting` stuck); single-opener-lock recommendation (now landed) | current (vigil = read-only; recommendations) |
| [vigil-spark-integration](vigil-spark-integration.md) | **CURRENT — vigil ↔ GB10 fork integration map.** vigil = pyrealsense2-direct, py3.12, 2-stream, CPU align every frame. Top wins via ONE SDK swap: CUDA align 15–19× + USB mitigations. Phase-1 adoption path + robustness gaps | current (recommendation) |
| [librealsense-consumers-inventory](librealsense-consumers-inventory-2026-06-05.md) | **CURRENT — every librealsense consumer on the box + rebuild/multistream needs.** Two SDK trees (GB10 fork 2.58 vs apt 2.57 — the name trap); unit-tests/live (~120, multistream/stress); ≥9 multistream examples; the realsense2_camera ROS2 node = highest-value GB10-safe target | current |
| [FINDINGS](FINDINGS-2026-06-03.md) | xHCI controller-death root cause (NVIDIA Stop-Endpoint defect) + P2/P3/P4/P7 mitigations | historical snapshot — root cause current; open items see ENHANCEMENT-TARGETS |
| [HIL-RESULTS](HIL-RESULTS-2026-06-03.md), [HIL-MULTISTREAM](HIL-MULTISTREAM-2026-06-03.md) | single/multi-stream HIL survival envelope (eyes-open vs safe) | historical snapshot |
| [HIL-SOAK-AND-ACCEL](HIL-SOAK-AND-ACCEL-2026-06-03.md) | long-soak + **per-op CUDA reality** (align 15-19×; pointcloud cached 3.3×; conversion neutral) | historical snapshot |
| [CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES](CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES-2026-06-03.md) | full CUDA/GPU dependency map + graded opportunities (no NVENC/cuDNN/NPP/TRT in core) | historical snapshot |
| [GPU-PIPELINE-ARCHITECTURE](GPU-PIPELINE-ARCHITECTURE-2026-06-03.md) | USB→process→display→file GPU map; **shared-memory finding** (cache buffers, not zero-copy); GL; TRT probe | historical snapshot — NVENC cq=23/p4 default now set (see nvenc-cq-sweep); keep-on-GPU chain measured (see ROS2-GL-PINNED-FINDINGS) |
| [QUALITY-RESULTS](QUALITY-RESULTS-2026-06-03.md) | NVENC encode fidelity (SSIM/XPSNR); initial cq=23 noted as under-tuned | historical snapshot — cq sweep completed; see [nvenc-cq-sweep](nvenc-cq-sweep.md) for final recommendation |
| [POSE-PIPELINE-TESTBED-PLAN](POSE-PIPELINE-TESTBED-PLAN-2026-06-03.md) | RealSense→3D-pose→render test bed — **live-proven on hardware** | historical snapshot — Phase-1 DONE |
| [FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE](FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE-2026-06-03.md) | fork-vs-upstream diff + firmware (downgrade device-blocked; HOLD 5.15.1.55) | historical snapshot — current |
| [ROS2-GL-PINNED-FINDINGS](ROS2-GL-PINNED-FINDINGS-2026-06-05.md) | parallel-agent results: ROS2 Jazzy builds vs GB10 SDK; keep-on-GPU GL measured win (1–7 ms/frame at 720p+); pinned-mem DROPPED (harmful on coherent memory); py3.13/3.14 re-target plan | current |
| [ros2-stream-start-analysis](ros2-stream-start-analysis.md) | ROS2 `#26` SOLVED — depth-only streams 4/4 at 30 fps, 0 drops; H1 (manual-exposure-under-AE) REFUTED by 8-run live A/B; fix is the minimal-config param **combination**, no single override isolated | current — do not edit |
| [nvenc-cq-sweep](nvenc-cq-sweep.md) | NVENC CQ sweep by XPSNR: **cq=23/p4 is the deployed default** for `--record` (knee of quality/size curve; 39.1 dB XPSNR-Y, 10.9× realtime) | current |
| [p4-async-pipelining](p4-async-pipelining.md) | P4 async pipelining (#31) — measured **NO-GO** for single-camera real-time: op is already 80–270× camera rate and <2.5% of frame budget; overlap collapses at higher res on unified memory | current |
| [ENHANCEMENT-TARGETS](ENHANCEMENT-TARGETS-2026-06-05.md) | graded open items — R1-R4+P1+U3+O3+#26/#31/#32-NVENC all DONE; remaining: P2 NEON filters, O1 Kilted ROS2, O2 py3.14 | current |
| [benchmarks](benchmarks.md) | benchmark summary (in progress — being populated by benchmark agent) | pending |
| [nvidia-escalation/](nvidia-escalation/) | drafted NVIDIA bug report for the xHCI defect | historical |

## Tooling — `../../scripts/gb10/` (driven by the repo-root `justfile`; run `just`)
Build: `just build` / `build-hil`. Tests (no HW): `just test`. HIL (needs D435 on USB-3): `just hil-*`.
Key benchmarks: `hil-align-bench`, `hil-cuda-bench`, `hil-pc-zerocopy`, `hil-gpu-pipeline [--convert-only]`,
`rs-gb10-conv-cache-bench.py`, `hil-quality`, `trt-probe`. Firmware status: `just fw-status`.
Additional recipes (all no-camera where noted):
- `just test-cached` — CUDA cached-pool byte-identity tests (GPU, no camera; R4)
- `just gb10-doctor` — one-command runtime preflight: toolchain, pyrealsense2, cv2/opencv/ffmpeg, CUDA, GL SDK, DISPLAY, NVENC, controller health, camera+USB-3 (U3)
- `just hil-keepongpu [--record m.mp4] [--stream color]` — P1 keep-on-GPU depth viewer: `gl::colorizer` output stays a GL texture, drawn straight to screen (no D2H); `--record` uses **NVENC cq=23/p4** (see [nvenc-cq-sweep](nvenc-cq-sweep.md)); R2 teardown-order fixed
- `just ros2-hil` — ROS2 HIL wrapper (default: offline self-test, CI-safe; `--live` for operator-run); depth-only streams 4/4 @ 30 fps, 0 drops (#26 SOLVED — see [ros2-stream-start-analysis](ros2-stream-start-analysis.md))
- `just bench-async` — P4 async-pipelining microbench (no camera, GPU); **measured NO-GO** for single-camera real-time; see [p4-async-pipelining](p4-async-pipelining.md)
- `just nvenc-sweep [INPUT=…]` — NVENC CQ/preset quality sweep (no camera); see [nvenc-cq-sweep](nvenc-cq-sweep.md)
- `just build-info [build_dir]` — emit/inspect `BUILD_PROVENANCE.json` (git tag+commit, actual compile options, toolchain, `.so` sha, reproduce command)
- `just build-tag` — current source state (`git describe --tags --dirty`)
See [`../../scripts/gb10/README.md`](../../scripts/gb10/README.md) for every tool + its safety class.

## Build provenance & reproducibility
Every GB10 build records exactly how it was made so it can be reproduced on another system, and so you
can verify a binary reflects the latest source.
- **Tag:** the source tree is tagged `v2.58.1-gb10.1` (upstream 2.58.1 + the David-Martel GB10 build). Current
  head: `e657daba0`. `just build-tag` (= `git describe --tags --dirty`) shows the live state with any
  `-dirty`/`-N-g<sha>` suffix.
- **Manifest:** `just build-info [build_dir]` writes `BUILD_PROVENANCE.json` next to the binary, capturing the
  git describe+commit, the **actual** compile options (read from the build's `CMakeCache.txt` — `RS2_GB10_*`,
  CUDA/NEON/RSUSB, cuda-arch, build type), the runtime-mode defaults (`RS2_PC_MODE`/`RS2_CONV_MODE=1`), the
  toolchain (gcc/CUDA versions), the `.so` sha + build time, whether it **reflects the latest source**, and a
  one-line `reproduce` command. `scripts/build-dgx-spark-gb10.sh` emits it automatically after `build`/`install`/`all`.
- **Reproduce elsewhere:** read the manifest's `reproduce` field — checkout that commit, match the toolchain
  (CUDA + gcc versions), and run the build script with the same `LRS_GB10_*` env. The opt-in defines are
  `#if`-guarded so an upstream build without them is byte-identical.

## Safety model (read before any HIL)
Single-stream (depth or color alone) is the conservative-safe envelope. Multi-stream / churn / soak are
**eyes-open**: on the death-era camera/firmware/topology they killed the xHCI controller (reboot to recover).
Every HIL tool arms a journal tripwire and aborts on `HC died`. Never open the camera from two processes at
once. The underlying defect is NVIDIA's (see nvidia-escalation/); these tools reduce the trigger surface.
