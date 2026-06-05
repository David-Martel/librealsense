#!/usr/bin/env bash
# launch_depth_only.sh — SAFE depth-only RealSense launch for GB10 (DGX Spark)
#
# SAFETY CONSTRAINTS:
#   - Single stream (depth only): conservative safe envelope for GB10 USB controller.
#   - Multi-stream (default all-streams) is NOT safe on GB10 — it causes xHCI controller
#     death under high-bandwidth conditions. Always use this script, never the default launch.
#   - DO NOT add enable_color:=true, enable_gyro:=true, enable_accel:=true without first
#     verifying USB controller stability on the GB10 bus that the camera is attached to.
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
