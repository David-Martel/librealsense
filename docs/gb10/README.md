# GB10 / DGX Spark — librealsense findings, tooling, and build options

Index for the David-Martel librealsense fork's NVIDIA DGX Spark (GB10, aarch64, CUDA 13) work:
USB-controller hardening, CUDA/GPU acceleration of the capture→process→render pipeline, and a
RealSense→3D-pose→render test bed. All findings are HIL-measured on host spark-3066 (D435, FW 5.15.1.55).

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
| doc | topic |
|---|---|
| [FINDINGS](FINDINGS-2026-06-03.md) | xHCI controller-death root cause (NVIDIA Stop-Endpoint defect) + P2/P3/P4/P7 mitigations |
| [HIL-RESULTS](HIL-RESULTS-2026-06-03.md), [HIL-MULTISTREAM](HIL-MULTISTREAM-2026-06-03.md) | single/multi-stream HIL survival envelope (eyes-open vs safe) |
| [HIL-SOAK-AND-ACCEL](HIL-SOAK-AND-ACCEL-2026-06-03.md) | long-soak + **per-op CUDA reality** (align 15-19×; pointcloud cached 3.3×; conversion neutral) |
| [CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES](CUDA-ACCEL-SURFACE-AND-OPPORTUNITIES-2026-06-03.md) | full CUDA/GPU dependency map + graded opportunities (no NVENC/cuDNN/NPP/TRT in core) |
| [GPU-PIPELINE-ARCHITECTURE](GPU-PIPELINE-ARCHITECTURE-2026-06-03.md) | USB→process→display→file GPU map; **shared-memory finding** (cache buffers, not zero-copy); GL; TRT probe |
| [QUALITY-RESULTS](QUALITY-RESULTS-2026-06-03.md) | NVENC encode fidelity (SSIM/XPSNR) |
| [POSE-PIPELINE-TESTBED-PLAN](POSE-PIPELINE-TESTBED-PLAN-2026-06-03.md) | RealSense→3D-pose→render test bed — **live-proven on hardware** |
| [FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE](FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE-2026-06-03.md) | fork-vs-upstream diff + firmware (downgrade device-blocked; HOLD 5.15.1.55) |
| [ROS2-GL-PINNED-FINDINGS](ROS2-GL-PINNED-FINDINGS-2026-06-05.md) | parallel-agent results: ROS2 Jazzy builds vs GB10 SDK; keep-on-GPU GL measured win; pinned-mem DROPPED (harmful on coherent memory); py3.13/3.14 re-target plan |
| [ENHANCEMENT-TARGETS](ENHANCEMENT-TARGETS-2026-06-05.md) | graded open items — R1-R4+P1+U3 all DONE; remaining: P2 filters, P3 depth-format cache, P4 async, O1/O2/O3 ROS2/py |
| [nvidia-escalation/](nvidia-escalation/) | drafted NVIDIA bug report for the xHCI defect |

## Tooling — `../../scripts/gb10/` (driven by the repo-root `justfile`; run `just`)
Build: `just build` / `build-hil`. Tests (no HW): `just test`. HIL (needs D435 on USB-3): `just hil-*`.
Key benchmarks: `hil-align-bench`, `hil-cuda-bench`, `hil-pc-zerocopy`, `hil-gpu-pipeline [--convert-only]`,
`rs-gb10-conv-cache-bench.py`, `hil-quality`, `trt-probe`. Firmware status: `just fw-status`.
Additional recipes (all no-camera where noted):
- `just test-cached` — CUDA cached-pool byte-identity tests (GPU, no camera; R4)
- `just gb10-doctor` — one-command runtime preflight: toolchain, pyrealsense2, cv2/opencv/ffmpeg, CUDA, GL SDK, DISPLAY, NVENC, controller health, camera+USB-3 (U3)
- `just hil-keepongpu` — P1 keep-on-GPU depth viewer: `gl::colorizer` output stays a GL texture, drawn straight to screen (no D2H); R2 teardown-order fixed
- `just build-info [build_dir]` — emit/inspect `BUILD_PROVENANCE.json` (git tag+commit, actual compile options, toolchain, `.so` sha, reproduce command)
- `just build-tag` — current source state (`git describe --tags --dirty`; current: `v2.58.1-gb10.1-4-g935294c5c`)
See [`../../scripts/gb10/README.md`](../../scripts/gb10/README.md) for every tool + its safety class.

## Build provenance & reproducibility
Every GB10 build records exactly how it was made so it can be reproduced on another system, and so you
can verify a binary reflects the latest source.
- **Tag:** the source tree is tagged `v2.58.1-gb10.1` (upstream 2.58.1 + the David-Martel GB10 build). Current
  head: `v2.58.1-gb10.1-4-g935294c5c` (4 commits ahead of the tag). `just build-tag`
  (= `git describe --tags --dirty`) shows the current state (a `-dirty`/`-N-g<sha>` suffix means
  uncommitted/ahead of the tag).
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
