# librealsense — GB10 / DGX Spark build, test, and HIL recipes
# Standardized entry points for the David-Martel GB10 fork. Run `just` to list.
#
# Most recipes wrap the canonical scripts (scripts/, and the out-of-tree validation
# harness) so there is ONE place to invoke the build/test/HIL flow. Hardware (HIL)
# recipes are clearly marked; multi-stream HIL can kill the GB10 USB controller.

set shell := ["bash", "-uc"]

# --- configurable paths (override on the CLI, e.g. `just validation_dir=/x hil`) ---
repo_root      := justfile_directory()
validation_dir := env_var_or_default("LRS_VALIDATION_DIR", env_var("HOME") + "/realsense-gb10-validation")
venv_python    := validation_dir + "/.venv/bin/python"
hil_build_dir  := validation_dir + "/build-gb10-full"
opencv_cmake   := env_var_or_default("LRS_GB10_OPENCV_DIR", "/opt/gb10-cuda/install/opencv") + "/lib/cmake/opencv4"
cuda_libs      := "/opt/gb10-cuda/install/opencv/lib:/opt/gb10-cuda/install/ffmpeg/lib"
cuda_root      := env_var_or_default("CUDA_HOME", "/usr/local/cuda")

# Default: show the recipe list
default:
    @just --list

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# Production GB10 build (RSUSB + CUDA + tuning) via the canonical script; installs to /opt/vigil.
build:
    PYTHON_EXECUTABLE="{{venv_python}}" bash "{{repo_root}}/scripts/build-dgx-spark-gb10.sh"

# Non-installing full HIL build (CUDA + pyrealsense2 against the uv venv 3.12) into {{hil_build_dir}}.
# This is the build the HIL recipes consume. ~minutes on the GB10.
build-hil:
    #!/usr/bin/env bash
    set -euo pipefail
    export VIRTUAL_ENV="{{validation_dir}}/.venv"; export PATH="$VIRTUAL_ENV/bin:$PATH"
    cmake -S "{{repo_root}}" -B "{{hil_build_dir}}" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=20 -DCMAKE_CXX_STANDARD_REQUIRED=ON \
      -DCMAKE_C_FLAGS_RELEASE="-O3 -DNDEBUG -mcpu=native" -DCMAKE_CXX_FLAGS_RELEASE="-O3 -DNDEBUG -mcpu=native" \
      -DCUDA_TOOLKIT_ROOT_DIR="{{cuda_root}}" -DCMAKE_CUDA_COMPILER="{{cuda_root}}/bin/nvcc" \
      -DCMAKE_CUDA_ARCHITECTURES=121 \
      -DFORCE_RSUSB_BACKEND=ON -DBUILD_WITH_CUDA=ON -DBUILD_WITH_NEON=ON -DBUILD_WITH_CPU_EXTENSIONS=ON -DBUILD_WITH_OPENMP=ON \
      -DBUILD_PYTHON_BINDINGS=ON -DPYTHON_EXECUTABLE="{{venv_python}}" -DPython_EXECUTABLE="{{venv_python}}" \
      -DPython_ROOT_DIR="$VIRTUAL_ENV" -DPython_FIND_VIRTUALENV=ONLY \
      -DBUILD_TOOLS=ON -DBUILD_EXAMPLES=OFF -DBUILD_GRAPHICAL_EXAMPLES=OFF -DBUILD_UNIT_TESTS=OFF \
      -DCHECK_FOR_UPDATES=OFF -DRS2_GB10_USB_TUNING=1 -DOpenCV_DIR="{{opencv_cmake}}"
    cmake --build "{{hil_build_dir}}" --parallel "$(nproc)"
    echo "HIL build ready: {{hil_build_dir}}/Release/librealsense2.so (+ pyrealsense2)"

# Remove the HIL build dir.
clean-hil:
    rm -rf "{{hil_build_dir}}"

# ---------------------------------------------------------------------------
# Test (no hardware)
# ---------------------------------------------------------------------------

# Fast standalone gate for the usb-tuning policy helpers (g++ only, no SDK, no hardware).
test-fast:
    bash "{{repo_root}}/scripts/rs-gb10-test-usb-tuning.sh"

# Build + run the Catch2 usb-tuning unit tests (P2/P3/P4/P7) in a throwaway build dir.
test-unit:
    #!/usr/bin/env bash
    set -euo pipefail
    B="$(mktemp -d /tmp/lrs-unit-XXXXXX)"
    cmake -S "{{repo_root}}" -B "$B" -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DFORCE_RSUSB_BACKEND=ON -DBUILD_WITH_CUDA=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TOOLS=OFF \
      -DBUILD_UNIT_TESTS=ON -DRS2_GB10_USB_TUNING=1 -DCHECK_FOR_UPDATES=OFF >/dev/null
    cmake --build "$B" --target test-usb-tuning-usb-tuning --parallel "$(nproc)"
    "$B"/Release/test-usb-tuning-usb-tuning
    rm -rf "$B"

# All no-hardware tests.
test: test-fast test-unit

# Unit-test the CUDA cached pools (pointcloud + conversion): byte-identical mode0-vs-mode1 + grow-only
# buffer correctness across a resolution sequence + mode selection. NO camera (needs the GPU).
test-cached:
    bash "{{repo_root}}/scripts/gb10/test-cached-pools.sh"

# Smoke-test the rendering/display tools (compile sweep + offline self-test + CLI contract).
# `just smoke-display` = offline only; `just smoke-display --live` adds a short on-screen validation.
smoke-display *ARGS:
    bash "{{repo_root}}/scripts/gb10/smoke-test-display.sh" {{ARGS}}

# ---------------------------------------------------------------------------
# HIL (HARDWARE — requires the D435 on a healthy USB-3 port)
# ---------------------------------------------------------------------------

# Pre-flight: controller alive, USB-3 link, camera free, no recent HC-died.
hil-preflight:
    bash "{{validation_dir}}/bin/rs-gb10-usb2-guard.sh" || true
    bash "{{validation_dir}}/bin/rs-gb10-healthcheck.sh"

# Single-stream HIL (SAFE envelope). BACKEND = rsusb | v4l2.
hil backend="rsusb":
    bash "{{validation_dir}}/bin/rs-gb10-hil.sh" --backend "{{backend}}" --build-dir "{{hil_build_dir}}/Release"

# P7 re-acquire guard confirm: single-context open under strict REFUSE (must NOT false-fire).
hil-p7:
    LD_LIBRARY_PATH="{{hil_build_dir}}/Release" PYTHONPATH="{{hil_build_dir}}/Release" \
      RS2_GB10_REFUSE_REACQUIRE=1 "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-p7-confirm.py"

# Frame/video QUALITY HIL: NVENC encode fidelity (ffmpeg ssim/psnr/xpsnr) + GPU-vs-CPU render +
# no-reference capture sharpness/exposure. Real measured data, single color stream (SAFE).
hil-quality:
    LD_LIBRARY_PATH="{{cuda_libs}}:{{hil_build_dir}}/Release" \
      PYTHONPATH="{{hil_build_dir}}/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg \
      "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-quality-hil.py"

# --- standardized HIL suite (hil_common.py + rs-gb10-hil-suite.py): idempotent + tripwire-guarded ---
hil_env := "LD_LIBRARY_PATH=" + cuda_libs + ":" + hil_build_dir + "/Release PYTHONPATH=" + repo_root + "/scripts/gb10:" + hil_build_dir + "/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg"

# Long-soak (phased single->dual->churn->quad + control-feature exercise). DANGER: eyes-open multi-stream.
# `just hil-soak` headless full; `just hil-soak --display` watch on screen; `--scale 0.3` shorter.
hil-soak *ARGS:
    {{hil_env}} DISPLAY="${DISPLAY:-:1}" "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-hil-suite.py" soak {{ARGS}}

# Capture->playback stress: record a rosbag2 .db3, replay real-time + max-speed, verify integrity (SAFE-ish).
hil-capture-playback *ARGS:
    {{hil_env}} DISPLAY="${DISPLAY:-:1}" "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-hil-suite.py" capture-playback {{ARGS}}

# librealsense CUDA-op benchmark (colorize+pointcloud) — run against both builds to compare; needs build-gb10-nocuda.
hil-cuda-bench:
    @echo "CUDA build:"; LD_LIBRARY_PATH="{{hil_build_dir}}/Release" PYTHONPATH="{{hil_build_dir}}/Release" LRS_BUILD_TAG=CUDA "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-cuda-bench.py"
    @echo "CUDA-OFF build (build-gb10-nocuda):"; LD_LIBRARY_PATH="{{validation_dir}}/build-gb10-nocuda/Release" PYTHONPATH="{{validation_dir}}/build-gb10-nocuda/Release" LRS_BUILD_TAG=NOCUDA "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-cuda-bench.py"

# rs.align CUDA-vs-CPU benchmark (depth->color, vigil's per-frame op) — runs BOTH builds back-to-back
# on the same static scene + computes speedup. 2-stream/eyes-open: tripwire-armed, fixed config.
# Point the camera at a STATIC rigid scene before running. `--display` to watch (needs opencv on PATH).
hil-align-bench *ARGS:
    #!/usr/bin/env bash
    set -uo pipefail
    SG="{{repo_root}}/scripts/gb10"; OUT="$(mktemp -d /tmp/lrs-align-XXXXXX)"
    echo ">>> CUDA build (build-gb10-full):"
    LD_LIBRARY_PATH="{{cuda_libs}}:{{hil_build_dir}}/Release" \
      PYTHONPATH="$SG:{{hil_build_dir}}/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      DISPLAY="${DISPLAY:-:1}" LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg LRS_BUILD_TAG=CUDA \
      LRS_RESULT_JSON="$OUT/cuda.json" "{{venv_python}}" "$SG/rs-gb10-align-bench.py" {{ARGS}}
    echo ">>> CUDA-OFF build (build-gb10-nocuda):"
    LD_LIBRARY_PATH="{{cuda_libs}}:{{validation_dir}}/build-gb10-nocuda/Release" \
      PYTHONPATH="$SG:{{validation_dir}}/build-gb10-nocuda/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      DISPLAY="${DISPLAY:-:1}" LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg LRS_BUILD_TAG=NOCUDA \
      LRS_RESULT_JSON="$OUT/nocuda.json" "{{venv_python}}" "$SG/rs-gb10-align-bench.py" {{ARGS}}
    "{{venv_python}}" "$SG/_align_speedup.py" "$OUT/cuda.json" "$OUT/nocuda.json"
    rm -rf "$OUT"

# End-to-end GPU pipeline throughput (Finding A): USB-ingest+conversion -> align -> [colorize].
# Runs BOTH builds back-to-back + prints whether per-frame CUDA color conversion offloads the decode
# off the CPU or is a regression vs NEON. 2-stream/eyes-open: tripwire-armed, fixed config, static scene.
# `--colorize` adds the render stage; `--display` to watch. Default 1280x720x30 (stresses conversion).
hil-gpu-pipeline *ARGS:
    #!/usr/bin/env bash
    set -uo pipefail
    SG="{{repo_root}}/scripts/gb10"; OUT="$(mktemp -d /tmp/lrs-pipe-XXXXXX)"
    echo ">>> CUDA build (build-gb10-full):"
    LD_LIBRARY_PATH="{{cuda_libs}}:{{hil_build_dir}}/Release" \
      PYTHONPATH="$SG:{{hil_build_dir}}/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      DISPLAY="${DISPLAY:-:1}" LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg LRS_BUILD_TAG=CUDA \
      LRS_RESULT_JSON="$OUT/cuda.json" "{{venv_python}}" "$SG/rs-gb10-gpu-pipeline.py" {{ARGS}}
    echo ">>> CUDA-OFF build (build-gb10-nocuda):"
    LD_LIBRARY_PATH="{{cuda_libs}}:{{validation_dir}}/build-gb10-nocuda/Release" \
      PYTHONPATH="$SG:{{validation_dir}}/build-gb10-nocuda/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      DISPLAY="${DISPLAY:-:1}" LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg LRS_BUILD_TAG=NOCUDA \
      LRS_RESULT_JSON="$OUT/nocuda.json" "{{venv_python}}" "$SG/rs-gb10-gpu-pipeline.py" {{ARGS}}
    "{{venv_python}}" "$SG/_pipeline_compare.py" "$OUT/cuda.json" "$OUT/nocuda.json"
    rm -rf "$OUT"

# Pointcloud zero-copy attribution ladder (needs a build with -DRS2_GB10_PC_ZEROCOPY=1, e.g. build-gb10-full).
# Runs baseline / cached-device / cached-managed (modes 0/1/2) + NEON, with per-rung correctness vs a
# numpy CPU deproject. Single depth stream = SAFE. Shows WHY shipped CUDA pointcloud is slow (alloc churn).
hil-pc-zerocopy:
    #!/usr/bin/env bash
    set -uo pipefail
    SG="{{repo_root}}/scripts/gb10"; FULL="{{hil_build_dir}}/Release"; NOCUDA="{{validation_dir}}/build-gb10-nocuda/Release"
    echo "rung              correct   p50_ms"
    for spec in "BASELINE:$FULL:0" "CACHED-DEV:$FULL:1" "CACHED-MANAGED:$FULL:2" "NEON:$NOCUDA:0"; do
      IFS=: read tag dir mode <<< "$spec"
      r=$(LD_LIBRARY_PATH="$dir" PYTHONPATH="$SG:$dir" RS2_PC_MODE="$mode" LRS_BUILD_TAG="$tag" \
        "{{venv_python}}" "$SG/rs-gb10-pc-zerocopy.py" 2>&1 | grep -oE '"p50": [0-9.]+|"correct": (true|false)' | tr '\n' ' ')
      printf "  %-16s %s\n" "$tag" "$r"
    done
    @echo "Expect: CACHED-DEV ~3x faster than BASELINE and faster than NEON (alloc churn was the cost, not the copy)."

# NON-HEADLESS display + LIVE-VIDEO validation on the MAIN display: renders the live stream to an
# on-screen window, x11grabs it, and PASS/FAILs on non-blank + displayed==captured (SSIM) + live (frames
# change) + framerate + recorded NVENC clip + controller-green. Standardized/idempotent (timestamped
# artifacts, tripwire). Default single COLOR stream (SAFE); `--depth` colorized depth; `--rgbd` (eyes-open).
hil-nonheadless *ARGS:
    DISPLAY="${DISPLAY:-:1}" LD_LIBRARY_PATH="{{cuda_libs}}:{{hil_build_dir}}/Release" \
      PYTHONPATH="{{repo_root}}/scripts/gb10:{{hil_build_dir}}/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg \
      "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-nonheadless-verify.py" {{ARGS}}

# Advanced single-stream HIL: CUDA colorize/pointcloud + cv2.cuda + NVENC + post-proc (SAFE).
hil-advanced:
    LD_LIBRARY_PATH="{{cuda_libs}}:{{hil_build_dir}}/Release" \
      PYTHONPATH="{{hil_build_dir}}/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-hil-advanced.py"

# ROS2 (Jazzy) single-stream node — SAFE config (depth only; default depth+color+IMU is multi-stream/lethal).
# Requires a colcon build of realsense-ros against this SDK first (see docs/gb10/HIL-RESULTS); depth-only params shown.
ros2-single:
    @echo "ros2 launch realsense2_camera rs_launch.py enable_color:=false enable_gyro:=false enable_accel:=false depth_module.depth_profile:=848x480x60"
    @echo "(Build realsense-ros against {{hil_build_dir}} first; default all-stream config is proven lethal on GB10.)"

# Parse-log self-test for the ROS2 HIL wrapper: offline, no camera, CI-safe.
# Parses the proven 2026-06-05 HIL log and asserts 708 frames / 30.03 fps / 0 drops / PASS.
# Override LOG= to check a different file.  `just ros2-hil --live` runs the live camera path.
ros2-hil *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    # NOTE: just does not populate $1/$@ for shebang recipes on this version — use {{ARGS}}
    # interpolation, and let the tool itself dispatch --live/--parse-log/--self-test.
    if [[ "{{ARGS}}" == --live* ]]; then
        {{hil_env}} "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-ros2-hil.py" {{ARGS}}
    elif [[ -z "{{ARGS}}" ]]; then
        "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-ros2-hil.py" --self-test
    else
        "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-ros2-hil.py" {{ARGS}}
    fi

# P4 (#31) async-pipelining microbench: shipped cached path vs double-buffered multi-stream. GPU only,
# NO camera. Verdict on file: NO-GO for the single-camera real-time path (op is already 80-270x the camera
# rate; overlap is bandwidth-capped on GB10 unified memory). See docs/gb10/p4-async-pipelining.md.
bench-async:
    bash "{{repo_root}}/scripts/gb10/async-pipeline-bench.sh"

# H7/H8 perf feasibility (no camera): H7 = align per-frame alloc, H8 = USB-thread<->CUDA affinity jitter.
# Verdict on file: BOTH NO-GO (align already caches buffers -> 0 cudaMalloc/frame; affinity shows no
# reproducible jitter win on GB10's 20 cores). See docs/gb10/h7h8-perf-feasibility-2026-06-05.md.
bench-h7h8 WHICH="all":
    bash "{{repo_root}}/scripts/gb10/bench_h7h8.sh" {{WHICH}}

# NEON+OpenMP depth-filter feasibility microbench (#32, no camera): scalar-vs-NEON-vs-NEON+OpenMP for the
# src/proc post-process filters (all pure scalar/unparallelized on aarch64; gcc doesn't autovec the branchy
# loops). Measured GO in isolation (threshold 5.3x, disparity 4.7x, temporal 3.5x NEON; spatial/decimation
# OpenMP) — but consumer-gated (vigil uses rs.align/CUDA, not these). See docs/gb10/neon-openmp-filters.md.
gb10-bench-filters:
    bash "{{repo_root}}/scripts/gb10/bench-filters.sh"

# NVENC cq quality sweep (#32, offline): sweep h264_nvenc cq x preset on a recorded clip, measure
# size/time/XPSNR -> recommended default cq=23 p4 (now the keep-on-GPU --record default). NO camera.
# Defaults INPUT to the recorded keepongpu clip in the validation dir. See docs/gb10/nvenc-cq-sweep.md.
nvenc-sweep INPUT="" CQ_LIST="19 23 26 29 33" PRESET_LIST="p4 p6":
    bash "{{repo_root}}/scripts/gb10/nvenc-cq-sweep.sh" "{{INPUT}}" "{{CQ_LIST}}" "{{PRESET_LIST}}"

# P1 keep-on-GPU viewer: live depth -> gl::colorizer (output stays a GL texture) -> drawn straight to the
# on-screen window (NO device->host readback) on the GB10 GPU. R2 teardown fixed (rs2_gl_shutdown_processing
# before context destroy -> clean exit, no SIGSEGV). Single depth stream = SAFE. `--duration N`, `--size WxH`.
hil-keepongpu *ARGS:
    bash "{{repo_root}}/scripts/gb10/rs-gb10-keepongpu-build.sh"
    DISPLAY="${DISPLAY:-:1}" "{{validation_dir}}/rs-gb10-keepongpu-viewer" {{ARGS}}

# Python keep-on-GPU viewer/validator via the rs.gl pybind binding (#30): depth -> rs.gl.colorizer
# (GL texture) -> drawn straight to the window, NO D2H. Runs under the GL-enabled py3.14 tree
# (build-gb10-py314 + .venv314 with glfw+PyOpenGL). `--validate` = headless GPU-frame proof (CI-safe,
# single depth stream); `--view --duration N` = visible viewer. `just hil-keepongpu-py --validate`
hil-keepongpu-py *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    B="{{validation_dir}}/build-gb10-py314/Release"
    [ -e "$B"/pyrealsense2.cpython-314-*.so ] || { echo "build-gb10-py314 (GL tree) missing — build it first"; exit 1; }
    DISPLAY="${DISPLAY:-:1}" PYTHONPATH="$B" LD_LIBRARY_PATH="$B" \
        "{{validation_dir}}/.venv314/bin/python" "{{repo_root}}/scripts/gb10/rs-gb10-keepongpu-py.py" {{ARGS}}

# DANGER: concurrent multi-stream stress — can KILL the USB controller (reboot to recover).
# Requires explicit opt-in flag. Arms the journal tripwire + forensics.
hil-stress-DANGER:
    @echo "WARNING: concurrent multi-stream is proven LETHAL on the GB10 xHCI (reboot recovers)."
    bash "{{validation_dir}}/bin/rs-gb10-hil.sh" --backend rsusb --allow-dual --build-dir "{{hil_build_dir}}/Release"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

# Firmware status for all linked cameras vs latest 5.17.0.10 (report-only; pass --flash --image to update).
fw-status *ARGS:
    LD_LIBRARY_PATH="{{hil_build_dir}}/Release" PYTHONPATH="{{hil_build_dir}}/Release" \
      LRS_RS_FW_UPDATE="{{hil_build_dir}}/Release/rs-fw-update" \
      "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-fw-update.py" {{ARGS}}

# TensorRT capability probe: synthesize a small depth-filter CNN and time it (no SDK integration).
# Characterizes GB10 NN headroom for a FUTURE learned depth-filter stage. Needs onnx in the venv + trtexec.
trt-probe *ARGS:
    "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-trt-probe.py" {{ARGS}}

# Emit/inspect the build-provenance manifest (git tag+commit + compile options + toolchain) for a build.
# Reproducibility: BUILD_PROVENANCE.json records exactly how a binary was built. Default = build-gb10-full.
build-info build_dir=hil_build_dir:
    bash "{{repo_root}}/scripts/gb10/gb10-build-info.sh" "{{build_dir}}"

# Show the current build tag (git describe) — what version/state the source tree is at.
build-tag:
    @git -C "{{repo_root}}" describe --tags --always --dirty

# Environment doctor: check every GB10 runtime prerequisite (toolchain, pyrealsense2, cv2 libs, CUDA,
# display, GL SDK, NVENC, controller health, camera presence) with PASS/WARN/FAIL. Does NOT open the camera.
gb10-doctor:
    bash "{{repo_root}}/scripts/gb10/gb10-doctor.sh"

# Show the GB10 findings + open items.
findings:
    @sed -n '1,40p' "{{repo_root}}/docs/gb10/FINDINGS-2026-06-03.md"
