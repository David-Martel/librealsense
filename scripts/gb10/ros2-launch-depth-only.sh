#!/usr/bin/env bash
# launch_depth_only.sh — SAFE depth-only RealSense launch for GB10 (DGX Spark)
#
# SUPERSEDED 2026-09-10 — this script's central claim is no longer true.
#
#   "Multi-stream is NOT safe on GB10" held from June 2026 until it was re-measured. Four
#   concurrent 1280x720@30 streams (depth + colour + IR1 + IR2) now run clean on BOTH Sparks:
#   734,008 frames, 0 dropped, 0 xHCI danger signatures, an 80-minute soak on spark-3066
#   across 40 pipeline restarts plus an equivalent run on spark-0060, on two different xHCI
#   controller instances. See docs/gb10/benchmarks.md section 15.
#
#   The June controller deaths were real; what changed since is at minimum the kernel, the
#   camera firmware, the SDK and the topology, and this result does not say which one fixed
#   it. Depth-only remains a perfectly good minimal profile — it is simply no longer the
#   only safe one, and multistream no longer needs the warning below.
#
# STILL TRUE:
#   - Run the kernel tripwire (scripts/gb10/rs-gb10-stress-matrix.py, which aborts on the
#     first danger signature) against any NEW configuration before trusting it.
#   - One camera per process; hold a single rs2::context for the process lifetime rather
#     than destroying and recreating it (see src/usb-tuning.h, controller-death #2).
#
# USAGE: run as a regular user (not root).  The parent process must not have the camera
#        open in any other process — concurrent opens crash the GB10 USB controller.
#
#   bash ~/realsense-gb10-validation/ros2-ws/launch_depth_only.sh [extra ros2 launch args]
#
# PROFILE: 848x480x30  — tested safe envelope from GB10 USB stability validation
#          (see ~/realsense-gb10-validation/ANALYSIS-20260602/)

set -eo pipefail  # NOT -u: ROS2 setup.bash references unbound vars (AMENT_TRACE_SETUP_FILES)

# ---- SDK: point runtime at our GB10 custom build (librealsense2 2.58.1) ----
# This overrides the apt librealsense2 2.57.7 that ships with ros-jazzy-librealsense2.
export LD_LIBRARY_PATH="/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# ---- ROS2 Jazzy environment ----
# shellcheck source=/opt/ros/jazzy/setup.bash
source /opt/ros/jazzy/setup.bash

# ---- Our colcon workspace (realsense2_camera + realsense2_camera_msgs) ----
# shellcheck source=/home/damartel/realsense-gb10-validation/ros2-ws/install/setup.bash
source /home/damartel/realsense-gb10-validation/ros2-ws/install/setup.bash

# ---- Confirm we are NOT about to load the wrong librealsense2 ----
# Checks LD_LIBRARY_PATH directly (not ldconfig, which ignores LD_LIBRARY_PATH and would
# always report the apt 2.57.7 path, causing false warnings on every correct launch).
GB10_SO="/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib/librealsense2.so.2.58.1"
if [[ ! -f "${GB10_SO}" ]]; then
    echo "[ERROR] GB10 SDK .so not found at expected path: ${GB10_SO}"
    echo "[ERROR] Cannot guarantee correct SDK will be loaded. Aborting."
    exit 1
fi

echo "[launch_depth_only] SDK: ${LD_LIBRARY_PATH%%:*}"
echo "[launch_depth_only] Launching depth-only (848x480x30), all other streams disabled."
echo "[launch_depth_only] Extra args: $*"

exec ros2 launch realsense2_camera rs_launch.py \
    enable_color:=false \
    enable_gyro:=false \
    enable_accel:=false \
    enable_infra1:=false \
    enable_infra2:=false \
    depth_module.depth_profile:=848x480x30 \
    "$@"
