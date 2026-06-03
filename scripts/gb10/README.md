# GB10 / DGX Spark RealSense tooling

Build, test, HIL-stress, quality-measure, and firmware tooling for the David-Martel librealsense
fork on NVIDIA DGX Spark / GB10 (ARM64). Most are driven by the repo-root **`justfile`** (`just`).
Findings these tools produced live in [`../../docs/gb10/`](../../docs/gb10/).

## Runtime environment (all HIL/quality tools need this)
- **pyrealsense2** from a GB10 build dir on `LD_LIBRARY_PATH` + `PYTHONPATH`
  (`~/realsense-gb10-validation/build-gb10-full/Release` = CUDA build; `build-gb10-nocuda` = CPU baseline).
- **uv venv CPython 3.12.3** (`~/realsense-gb10-validation/.venv`) — bare `python3` is a 3.15 pre-release
  whose ABI cannot load the cpython-312 binding. numpy pinned **1.26.4** (cv2 ABI).
- **CUDA OpenCV 4.14 + NVENC ffmpeg** at `/opt/gb10-cuda/install` — add `{opencv,ffmpeg}/lib` to
  `LD_LIBRARY_PATH` and the opencv `site-packages` to `PYTHONPATH` for `cv2`/quality/encode tools.
The `just` recipes set all of this; run tools by hand only with the same env.

## Build
| tool | what |
|------|------|
| `../build-dgx-spark-gb10.sh [configure\|build\|install\|validate\|clean\|all]` | Production GB10 build (RSUSB + CUDA + tuning + pyrealsense2). **Reproducibility knobs:** ABI guard refuses a pre-release Python (the 3.15-vs-3.12 trap) and pins `FindPython` to the interpreter's venv; `LRS_GB10_REPRODUCIBLE=1` swaps `-mcpu=native`→`-mcpu=cortex-x925` for portable binaries; `LRS_GB10_FRESH=1` (or `clean` mode) does a from-scratch build. Key env: `PYTHON_EXECUTABLE` (point at the venv), `LRS_GB10_USB_TUNING` (1=GB10 mitigations on, 0=vanilla), `LRS_GB10_OPENCV_DIR`. |
| `just build` / `just build-hil` | production install build / non-installing CUDA+pyrealsense2 HIL build into `build-gb10-full`. |
| `rs-gb10-test-usb-tuning.sh` (`just test-fast`) | g++-only standalone gate for the `usb-tuning.h` P2/P3/P4/P7 policy — no SDK, no hardware. |
| `just test-unit` | builds + runs the Catch2 `usb-tuning` unit tests. |

## Standardized HIL suite (idempotent + tripwire-guarded)
`hil_common.py` + `rs-gb10-hil-suite.py` — shared infra: pre-flight gate (refuses on dead controller /
no camera / USB-2 link), **continuous controller tripwire** (aborts + dumps kernel forensics on the
first `HC died`), timestamped artifact dir (`~/realsense-gb10-validation/hil-runs/<ts>-<test>/result.json`),
percentile stats, `--display` (non-headless on-screen render + `x11grab` proof).
| recipe | what | safety |
|--------|------|--------|
| `just hil-soak [--display] [--scale S]` | phased single→dual+align→start/stop churn→quad-stream soak (~5 min at scale 1.0) + control-feature exercise (emitter/laser/preset/auto-exposure — the `-110` trigger class) | **eyes-open multi-stream** (can kill controller → reboot); tripwire aborts |
| `just hil-capture-playback [--display]` | record→rosbag2 **`.db3`**→replay (real-time + max-speed loops), verify deterministic replay | single+dual record; tripwire |

## Individual HIL / quality tools
| tool | recipe | what |
|------|--------|------|
| `rs-gb10-p7-confirm.py` | `just hil-p7` | single-context open under `RS2_GB10_REFUSE_REACQUIRE=1` — proves the P7 re-acquire guard doesn't false-fire |
| `rs-gb10-hil-advanced.py` | `just hil-advanced` | single-stream CUDA colorize/pointcloud + cv2.cuda + post-proc + NVENC, per-op latency |
| `rs-gb10-quality-hil.py` | `just hil-quality` | NVENC encode fidelity (ffmpeg ssim/psnr/xpsnr vs lossless) + GPU-vs-CPU render + no-reference sharpness/exposure |
| `rs-gb10-nonheadless-verify.py` | `just hil-nonheadless` | paint frames on `$DISPLAY`, `x11grab` proof that real pixels render |
| `rs-gb10-cuda-bench.py` | `just hil-cuda-bench` | librealsense colorize+pointcloud timing; run vs `build-gb10-nocuda` to compare CUDA-vs-CPU (`LRS_BENCH_W/H` set resolution) |
| `rs-gb10-align-bench.py` (+`_align_speedup.py`) | `just hil-align-bench` | **rs.align depth→color CUDA-vs-CPU** — the load-bearing op vigil runs every frame. Runs BOTH builds back-to-back on the same static scene + prints the speedup. **Measured 15–19× CUDA faster** (CUDA p50 ≈0.29 ms, CPU 4.3–5.7 ms). 2-stream/eyes-open: tripwire-armed, fixed config, no control-feature toggles. `LRS_ALIGN_TO=depth` for the reverse direction; `--display` to watch. |
| `rs-gb10-gpu-pipeline.py` (+`_pipeline_compare.py`) | `just hil-gpu-pipeline` | **End-to-end USB→convert→align→[colorize] throughput** + per-frame CPU-time attribution, full-vs-nocuda. `--convert-only` isolates the YUYV→RGB conversion (single-stream SAFE) → **Finding A: CUDA conversion is CPU-neutral vs NEON, not a regression**. `--colorize`/`--display` add stages. |
| `rs-gb10-pc-zerocopy.py` | `just hil-pc-zerocopy` | **Pointcloud zero-copy attribution ladder** (baseline/cached-device/cached-managed + NEON), each correctness-checked vs a numpy deproject. Needs a build with `-DRS2_GB10_PC_ZEROCOPY=1`. **Result: per-frame `cudaMalloc` churn was the cost — cached device buffers = 3.3× faster, beats NEON; managed memory slower.** Single depth stream = SAFE. |
| `rs-gb10-trt-probe.py` | `just trt-probe` | **TensorRT capability probe** (NOT an SDK integration) — synthesizes a small depth-filter CNN at depth resolution and times it with `trtexec`. Characterizes NN headroom for a *future* learned depth-filter stage. **Measured: 5-conv filter @848×480 = 0.9 ms FP32, 37× headroom @30 fps.** Needs `onnx` in the venv + `trtexec`. |
| `rs-gb10-soak.py`, `rs-gb10-multistream.py`, `rs-gb10-churn-test.py` | — | predecessors of the suite (kept for reference; prefer the suite) |

## Firmware
| tool | what |
|------|------|
| `rs-gb10-fw-update.py` (`just fw-status`) | reports every linked camera's firmware vs the **latest D400 production firmware 5.17.0.10**; gated flash with `--flash --image <Signed_Image_UVC_5_17_0_10.bin>` (download from <https://dev.realsenseai.com/docs/firmware-releases-d400/>). **Safety:** refuses USB-2 links (brick risk), refuses on a dead controller, **refuses a downgrade** (parses image version) without `--allow-downgrade`, backs up current fw first, dry-run by default. **Context:** the death-era camera ran old FW 5.13.0.55; updating forward is a *candidate* (unconfirmed) `-110`-trigger fix (see `docs/gb10/FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE`). |

**Firmware downgrade — verified from `src/fw-update/fw-update-device.cpp`:** the **host** does no version
check (`rs-fw-update -f` streams any signed image), but the **device** enforces anti-rollback in DFU mode
(`dfu_is_locked` + `fw_highest_version`); a **DFU-locked** unit rejects anything not *higher* than the
highest-ever-installed (throws "Device is locked for update… use firmware version higher than: …",
`:242`). **Production D435s ship locked → downgrade is forward-only / blocked by the device;** only an
unlocked/dev unit can be downgraded. Consequence: the 2×2 firmware-attribution test likely **cannot** put
the current (locked) unit back on 5.13.0.55 — it needs the original old unit. **Current decision: HOLD on
firmware (stay on 5.15.1.55); do not flash** while benchmarking/robustness/ROS2-integration work proceeds.

## Safety model (read before HIL)
- **Single-stream** (depth or color alone) is the conservative-safe envelope for production-critical use.
- **Multi-stream / churn / soak** are **eyes-open**: on the death-era camera/firmware/topology they killed
  the xHCI controller (reboot to recover). The current unit (FW 5.15.1.55, clean USB-3 bus) has survived
  them with zero `-110`, but that result is **confounded** (unit + firmware + topology + mitigations all
  changed) — see the firmware doc. Every suite run arms the tripwire and aborts on `HC died`.
- The underlying defect (GB10 xHCI cannot complete a Stop-Endpoint after a `-110`) is **NVIDIA's**;
  see `docs/gb10/nvidia-escalation/`. These tools reduce the trigger surface, they cannot fix the crash.
