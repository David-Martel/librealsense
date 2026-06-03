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
      -DCUDA_TOOLKIT_ROOT_DIR=/usr/local/cuda -DCMAKE_CUDA_ARCHITECTURES=121 \
      -DFORCE_RSUSB_BACKEND=ON -DBUILD_WITH_CUDA=ON -DBUILD_WITH_NEON=ON -DBUILD_WITH_CPU_EXTENSIONS=ON -DBUILD_WITH_OPENMP=ON \
      -DBUILD_PYTHON_BINDINGS=ON -DPYTHON_EXECUTABLE="{{venv_python}}" -DPython_EXECUTABLE="{{venv_python}}" \
      -DPython_ROOT_DIR="$VIRTUAL_ENV" -DPython_FIND_VIRTUALENV=ONLY \
      -DBUILD_TOOLS=ON -DBUILD_EXAMPLES=OFF -DBUILD_GRAPHICAL_EXAMPLES=OFF -DBUILD_UNIT_TESTS=OFF \
      -DCHECK_FOR_UPDATES=OFF -DRS2_GB10_USB_TUNING=1 -DOpenCV_DIR="{{opencv_cmake}}"
    cmake --build "{{hil_build_dir}}" --parallel "$(nproc)"
    @echo "HIL build ready: {{hil_build_dir}}/Release/librealsense2.so (+ pyrealsense2)"

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

# NON-HEADLESS render verify: paint frames on $DISPLAY, x11grab the screen, prove real pixels (SAFE).
hil-nonheadless:
    DISPLAY="${DISPLAY:-:1}" LD_LIBRARY_PATH="{{cuda_libs}}:{{hil_build_dir}}/Release" \
      PYTHONPATH="{{hil_build_dir}}/Release:/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages" \
      LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg \
      "{{venv_python}}" "{{repo_root}}/scripts/gb10/rs-gb10-nonheadless-verify.py"

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

# Show the GB10 findings + open items.
findings:
    @sed -n '1,40p' "{{repo_root}}/docs/gb10/FINDINGS-2026-06-03.md"
