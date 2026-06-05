# GB10 / DGX Spark RealSense tooling

Build, test, HIL-stress, quality-measure, and firmware tooling for the David-Martel librealsense
fork on NVIDIA DGX Spark / GB10 (ARM64). Most are driven by the repo-root **`justfile`** (`just`).
Findings these tools produced live in [`../../docs/gb10/`](../../docs/gb10/).

## Runtime environment (all HIL/quality tools need this)
- **pyrealsense2** from a GB10 build dir on `LD_LIBRARY_PATH` + `PYTHONPATH`
  (`~/realsense-gb10-validation/build-gb10-full/Release` = CUDA build; `build-gb10-nocuda` = CPU baseline).
- **uv venv CPython 3.12.3** (`~/realsense-gb10-validation/.venv`) — bare `python3` is a 3.15 pre-release
  whose ABI cannot load the cpython-312 binding. numpy pinned **1.26.4** (cv2 ABI).
  - **Python 3.13 retarget (UV minimum):** `LRS_PY_TAG=python3.13` selects the canonical 3.13 SDK
    (`build-gb10-py313`, GB10 cached/USB customizations baked in, verified `import` + camera-enumerate)
    and the `.venv313` interpreter. cv2 there is the **stock `opencv-python` 4.11 wheel** (display/HUD
    only — the CUDA-OpenCV `cv2` binding is still 3.12; a 3.13 CUDA-OpenCV rebuild is the remaining gap).
    numpy stays pinned 1.26.4. Build a new pyver tree with `PYTHON_EXECUTABLE=…/.venv313/bin/python ../build-dgx-spark-gb10.sh`.
  - **Python 3.14:** `LRS_PY_TAG=python3.14` → canonical `build-gb10-py314` + `.venv314` (CPython 3.14.5). Full
    SDK tree built (pybind11 2.13.6 supports 3.14; **sccache**-accelerated) and **live camera-validated**
    (D435, 60 gapless depth frames, controller GREEN). `.venv314` has no numpy (not needed to build; numpy
    1.26.4 has no cp314 wheel) and no cv2 — so the cv2-based display tools (and `gb10-doctor`'s cv2 check)
    are 3.12/3.13-only; the binding + tools are fine. Builds with sccache for C++/CUDA (vcpkg N/A — deps are vendored).
- **CUDA OpenCV 4.14 + NVENC ffmpeg** at `/opt/gb10-cuda/install` — add `{opencv,ffmpeg}/lib` to
  `LD_LIBRARY_PATH` and the opencv `site-packages` to `PYTHONPATH` for `cv2`/quality/encode tools.
The `just` recipes set all of this; run tools by hand only with the same env.

## Build
| tool | what |
|------|------|
| `../build-dgx-spark-gb10.sh [configure\|build\|install\|validate\|clean\|all]` | Production GB10 build (RSUSB + CUDA + tuning + pyrealsense2). **Reproducibility knobs:** ABI guard refuses a pre-release Python (the 3.15-vs-3.12 trap) and pins `FindPython` to the interpreter's venv; `LRS_GB10_REPRODUCIBLE=1` swaps `-mcpu=native`→`-mcpu=cortex-x925` for portable binaries; `LRS_GB10_FRESH=1` (or `clean` mode) does a from-scratch build. Key env: `PYTHON_EXECUTABLE` (point at the venv), `LRS_GB10_USB_TUNING` (1=GB10 mitigations on, 0=vanilla), `LRS_GB10_OPENCV_DIR`. |
| `just build` / `just build-hil` | production install build / non-installing CUDA+pyrealsense2 HIL build into `build-gb10-full`. |
| `just test-fast` | g++-only standalone gate for the `usb-tuning.h` P2/P3/P4/P7 policy — no SDK, no hardware. |
| `just test-unit` | builds + runs the Catch2 `usb-tuning` unit tests. |
| `test_cached_pools.cu` + `test-cached-pools.sh` (`just test-cached`) | **cached-pool unit test** (R4): pointcloud + conversion **byte-identical mode0-vs-mode1 across a grow/shrink resolution sequence** (grow-only buffer correctness + mode select). GPU only, **no camera**. |
| `rs-gb10-conv-cache-bench.py`, `bench_conv_cache_kernel.cu`, `test_conv_cache_correctness.cu` | **conversion-cache bench + correctness** — end-to-end mode0 vs mode1 throughput (YUYV→RGB, 1280×720); kernel-isolated microbench; byte-identity verification. Results: ~25–30% end-to-end savings (CUDA to NEON-parity); kernel 5× in isolation. GPU only, **no camera**. |
| `gb10-env.sh` | **single env source-of-truth** — `source` it to set `LD_LIBRARY_PATH`/`PYTHONPATH`/`DISPLAY`/`LRS_FFMPEG`/`LRS_VENV`/`LRS_BUILD_RELEASE` (pyrealsense2 + cv2's opencv & ffmpeg libs). **`LRS_PY_TAG` is the one-var retarget:** `python3.13`/`3.14` maps the SDK build tree (`build-gb10-py313`), the uv venv (`.venv313`) **and** the opencv site-packages together — they share an ABI and must move as one. A **loud ABI guard** warns if the resolved venv's Python minor ≠ `LRS_PY_TAG` (the `undefined symbol _PyThreadState_*` trap). The launcher + smoke-test `source` it first, then resolve `VENV`/paths from it. |
| `gb10-build-info.sh` (`just build-info [dir]`, `just build-tag`) | **build provenance** — writes `BUILD_PROVENANCE.json` (git describe+commit, actual CMakeCache options, runtime-mode defaults, toolchain, `.so` sha, reflects-latest-source, reproduce cmd). Auto-emitted by the build script. Tag `v2.58.1-gb10.1`. |

## Standardized HIL suite (idempotent + tripwire-guarded)
`hil_common.py` + `rs-gb10-hil-suite.py` — shared infra: pre-flight gate (refuses on dead controller /
no camera / USB-2 link), **continuous controller tripwire** (aborts + dumps kernel forensics on the
first `HC died`), timestamped artifact dir (`~/realsense-gb10-validation/hil-runs/<ts>-<test>/result.json`),
percentile stats, `--display` (non-headless on-screen render + `x11grab` proof).
| recipe | what | safety |
|--------|------|--------|
| `just hil-soak [--display] [--scale S]` | phased single→dual+align→start/stop churn→quad-stream soak (~5 min at scale 1.0) + control-feature exercise (emitter/laser/preset/auto-exposure — the `-110` trigger class) | **eyes-open multi-stream** (can kill controller → reboot); tripwire aborts |
| `just hil-capture-playback [--display]` | record→rosbag2 **`.db3`**→replay (real-time + max-speed loops), verify deterministic replay | single+dual record; tripwire |
| `just ros2-hil` | **ROS2 depth-only HIL wrapper** (`rs-gb10-ros2-hil.py`): default = parser self-test against the proven 2026-06-05 log (offline, CI-safe); `just ros2-hil --live [--duration N]` = operator-run live launch (SAFE single-stream, 25 s default); `just ros2-hil --parse-log FILE` = parse any captured log. PASS iff frames>0, fps≈30, 0 fatals, controller GREEN. | default: **no hardware** (parse-log/self-test); `--live`: single depth stream = SAFE; tripwire armed |

## Individual HIL / quality tools
| tool | recipe | what |
|------|--------|------|
| `rs-gb10-p7-confirm.py` | `just hil-p7` | single-context open under `RS2_GB10_REFUSE_REACQUIRE=1` — proves the P7 re-acquire guard doesn't false-fire |
| `rs-gb10-hil-advanced.py` | `just hil-advanced` | single-stream CUDA colorize/pointcloud + cv2.cuda + post-proc + NVENC, per-op latency |
| `rs-gb10-quality-hil.py` | `just hil-quality` | NVENC encode fidelity (ffmpeg ssim/psnr/xpsnr vs lossless) + GPU-vs-CPU render + no-reference sharpness/exposure |
| `rs-gb10-nonheadless-verify.py` | `just hil-nonheadless [--interactive] [--depth] [--duration=S]` | **Non-headless display validation + interactive debug viewer on the MAIN display.** SMOOTH/flicker-free render (tight `wait_for_frames→imshow` loop; x11grab validation captures, journalctl tripwire, and NVENC writes all run OFF the render thread → verified 0 stutters, max gap ≈1 frame). **Always-on debug HUD:** wall fps + render dt, device frame number, hardware/sensor timestamp+domain, and live metadata (actual_exposure, gain, actual_fps, frame_counter, sensor/arrival timestamps). **Validate mode** (default): PASS/FAIL on non-blank + our-content (green-overlay) + live-video (grabs differ) + steady-fps + **smooth (stutter≤1)** + NVENC clip + controller-green; standardized/idempotent (hil_common preflight+tripwire+timestamped artifacts; 3/3 PASS @30fps). **`--interactive`:** keyboard hooks — `1-9` profile (size×fps), `c/d` color/depth, `e` emitter, `a` auto-exp, `[ ]` exposure, `- =` gain, `l/L` laser, `f` freeze, `s` snapshot, `q` quit. |
| `rs-gb10-cuda-bench.py` | `just hil-cuda-bench` | librealsense colorize+pointcloud timing; run vs `build-gb10-nocuda` to compare CUDA-vs-CPU (`LRS_BENCH_W/H` set resolution) |
| `rs-gb10-align-bench.py` (+`_align_speedup.py`) | `just hil-align-bench` | **rs.align depth→color CUDA-vs-CPU** — the load-bearing op vigil runs every frame. Runs BOTH builds back-to-back on the same static scene + prints the speedup. **Measured 15–19× CUDA faster** (CUDA p50 ≈0.29 ms, CPU 4.3–5.7 ms). 2-stream/eyes-open: tripwire-armed, fixed config, no control-feature toggles. `LRS_ALIGN_TO=depth` for the reverse direction; `--display` to watch. |
| `rs-gb10-gpu-pipeline.py` (+`_pipeline_compare.py`) | `just hil-gpu-pipeline` | **End-to-end USB→convert→align→[colorize] throughput** + per-frame CPU-time attribution, full-vs-nocuda. `--convert-only` isolates the YUYV→RGB conversion (single-stream SAFE) → **Finding A: CUDA conversion is CPU-neutral vs NEON, not a regression**. `--colorize`/`--display` add stages. |
| `rs-gb10-pc-zerocopy.py` | `just hil-pc-zerocopy` | **Pointcloud zero-copy attribution ladder** (baseline/cached-device/cached-managed + NEON), each correctness-checked vs a numpy deproject. Needs a build with `-DRS2_GB10_PC_ZEROCOPY=1`. **Result: per-frame `cudaMalloc` churn was the cost — cached device buffers = 3.3× faster, beats NEON; managed memory slower.** Single depth stream = SAFE. |
| `rs-gb10-trt-probe.py` | `just trt-probe` | **TensorRT capability probe** (NOT an SDK integration) — synthesizes a small depth-filter CNN at depth resolution and times it with `trtexec`. Characterizes NN headroom for a *future* learned depth-filter stage. **Measured: 5-conv filter @848×480 = 0.9 ms FP32, 37× headroom @30 fps.** Needs `onnx` in the venv + `trtexec`. |
| `rs-gb10-keepongpu-viewer.cpp` (+`rs-gb10-keepongpu-build.sh`) | `just hil-keepongpu [--record m.mp4] [--stream color]` | **P1 keep-on-GPU GL render path** (C++): live depth → `gl::colorizer` (output stays a GL texture) → drawn straight to the on-screen window, **NO D2H** (avoids the ~3 ms@720p readback the cv2 path pays). **R2 teardown FIXED** (`rs2::gl::shutdown_processing()` before context destroy → clean exit, no SIGSEGV). `--record` = **NVENC GPU-to-disk** (deployed default: `-rc vbr -cq 23 -b:v 0 -preset p4` per cq-sweep; see `docs/gb10/nvenc-cq-sweep.md`); always-on per-frame telemetry (ts/domain/exposure/fps/render-p50). Single depth stream = SAFE. Builds vs the installed GL SDK. |
| `rs-gb10-keepongpu-py.py` | `just hil-keepongpu-py [--validate \| --view]` | **P1 keep-on-GPU from PYTHON (#30)** via the new **`rs.gl`** pybind binding (`pyrealsense2` built with `realsense2-gl`/`BUILD_GLSL_EXTENSIONS`): make a GL context current (glfw) → `rs.gl.init_processing()` → `rs.gl.colorizer().process(depth)` (output stays a GL texture) → draw the texture straight to the window, **NO D2H**. `--validate` = headless GPU-frame proof (asserts `is_gpu_frame` + nonzero `texture_id` — the CPU-fallback discriminator; CI-safe, single stream); `--view` = visible viewer (live-verified ~29.6 fps, clean teardown). Same R2 lesson (`rs.gl.shutdown_processing()` while context current). Runs under any **GL-enabled** tree (needs `realsense2-gl` = `(BUILD_EXAMPLES OR BUILD_PC_STITCHING) AND BUILD_GLSL_EXTENSIONS`): **both** the py3.14 tree (`build-gb10-py314` + `.venv314`) **and** the 3.12 test-bed (`build-gb10-full` + `.venv`, rebuilt with examples ON) — both validated (`is_gpu_frame=True`, `texture_id=4`). Needs `glfw`+`PyOpenGL` in the venv. NOTE: keep-on-GPU is a SEPARATE GL path — routing the texture through `np.asanyarray`/cv2 round-trips host memory and defeats it. |
| `bench_depth_format_cuda.cu` | — | **depth/IR-format conversion CUDA microbench** (no camera): Y8I (both-IR, hot) + Y12I (calibration). Finding: same per-frame `cudaMalloc` churn as YUYV but **don't cache** (end-to-end plumbing-bound, like Finding A). |
| `bench_async_pipeline.cu` + `async-pipeline-bench.sh` | `just bench-async` | **P4 async-pipelining microbench** (no camera, GPU): double-buffered multi-CUDA-stream vs cached-baseline (REF/A/B), per-stage isolation, overlap-efficiency measurement. **Verdict: NO-GO** for single-camera real-time (op <2.5% of frame budget, overlap collapses at higher res on unified memory). See `docs/gb10/p4-async-pipelining.md`. Build is `-Werror` clean. |
| `bench_filters_neon.cpp` + `bench-filters.sh` | `just gb10-bench-filters` | **NEON+OpenMP depth-filter feasibility** (#32, no camera): scalar-vs-NEON-vs-NEON+OpenMP for the `src/proc` post-process filters. Finding: **all are pure scalar + unparallelized on aarch64** (no SSE→NEON shim; gcc doesn't autovec the branchy loops, objdump-confirmed), so NEON is a real **GO in isolation** (bit-identical: threshold 5.3×, disparity 4.7×, temporal 3.5×; spatial/decimation via OpenMP). **Consumer-gated:** vigil uses `rs.align` (CUDA 15–19×), not these filters — implementation deferred until a consumer needs them (NEON is oversubscription-free; OpenMP-in-filter risks it in a threaded pipeline). See `docs/gb10/neon-openmp-filters.md`. `-Werror` clean. |
| `nvenc-cq-sweep.sh` | `just nvenc-sweep [INPUT=…]` | **NVENC cq/preset quality sweep** (no camera): runs ffmpeg NVENC encode at multiple cq+preset values, measures XPSNR+size+realtime-factor, writes results TSV. **Deployed default cq=23/p4** (knee of quality/size curve; see `docs/gb10/nvenc-cq-sweep.md`). Auto-sources `gb10-env.sh` for `$LRS_FFMPEG`. |
| `rs-gb10-soak.py`, `rs-gb10-hil-multistream.py`, `rs-gb10-churn-test.py` | — | predecessors of the suite (kept for reference; prefer the suite) |

## Desktop launcher + smoke tests
- **`gb10-viewer-launch.sh`** — single source of env truth (pyrealsense2 from `build-gb10-full` + cv2's opencv/ffmpeg libs + DISPLAY + NVENC), friendly preflight (missing-runtime / dead-controller / no-camera messages instead of a traceback), then opens the interactive viewer. `--validate` runs the automated PASS/FAIL validation instead; extra args pass through. **Desktop entry:** `gb10-realsense-viewer.desktop` (template) → copy to `~/Desktop/`, `chmod +x`, `gio set … metadata::trusted true`. Desktop Actions: `Doctor` (gb10-doctor, camera-safe), `KeepOnGPU` (builds and runs the P1 viewer), `Viewer313` (py3.13 ABI retarget via `LRS_PY_TAG`).
- **`smoke-test-display.sh`** (`just smoke-display [--live]`) — compile sweep + the display tool's offline `--self-test` (argparse / profile clamp / validation math / HUD compose) + CLI contract (`--help`=0, bad-arg=2); `--live` adds one tripwire-guarded on-screen validation. Run before trusting a display-tool change.
- **`gb10-doctor.sh`** (`just gb10-doctor`) — one-command environment preflight (PASS/WARN/FAIL): toolchain, pyrealsense2 import, cv2's opencv+ffmpeg libs, CUDA, GL SDK, DISPLAY, NVENC, controller health, camera + USB-3 link. **Does not open the camera.** Run first on a new system/user.
- **`ros2-launch-depth-minimal.sh`** — **#26-proven** minimal ROS2 depth-only launch (848×480×30, AE on, no manual exposure push, no initial_reset): **streams 4/4 at 30 fps, 0 drops, controller GREEN** (see `docs/gb10/ros2-stream-start-analysis.md` for the full A/B results). Use this as the production ROS2 launch baseline. SAFE single-stream envelope.
- **`ros2-launch-depth-only.sh`** — earlier depth-only launch (superseded by `ros2-launch-depth-minimal.sh` for #26-proven operation; kept for reference).

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
