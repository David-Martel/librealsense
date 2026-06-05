#!/usr/bin/env bash
# ros2-launch-depth-minimal.sh — VERIFIED depth-only launch for realsense2_camera on GB10
#
# ============================== VERIFIED — HIL-PROVEN 2026-06-05 ==============================
# This config makes realsense2_camera stream depth on GB10. Live HIL result (single-process, 25s):
#   708 depth frames Z16 848x480 @ 30.03 fps, ZERO drops, NO fatal error, controller GREEN, clean
#   release. The previously-FATAL control_transfer index 768 (0x0300) is reduced to 2 benign startup
#   EAGAIN warnings ("Resource temporarily unavailable, number: 11"). Log: ros2-depth-minimal-*.log.
#
# LIKELY CAUSE (consistent with H1 but NOT isolated — see docs/gb10/ros2-stream-start-analysis.md):
# index 768 (0x0300) == the D4xx DEPTH EXTENSION UNIT (depth_xu, unit 3, iface 0 — src/ds/ds-private.h:70).
# The leading hypothesis is that the stock node's MANUAL depth-XU exposure write (rs_launch.py declares
# depth_module.exposure=8500 → unwrapped param callback, sensor_params.cpp:71-74) WHILE auto-exposure is on
# is what the D435 fw 5.15.1.55 rejected. CAVEAT: this run is not proof — idx768 still fired (the SDK now
# rides the EAGAIN), the selector isn't logged, and this config ALSO disables initial_reset (a co-suspect).
# WHAT'S PROVEN: this minimal set (AE ON, no manual depth-XU control, no initial_reset) reliably streams.
# To pin the culprit, A/B one variable at a time (re-add only exposure:=8500; then only initial_reset:=true).
#
# SAFE-USE (the one-camera rule still applies):
#   RUN SERIALLY, SINGLE-PROCESS, no concurrent heavy load (xHCI controller death is load-induced).
# =====================================================================================
#
# USAGE (operator, manual, serial only):
#   bash ~/dev/repos/librealsense/scripts/gb10/ros2-launch-depth-minimal.sh [extra ros2 launch args]
#
# PROFILE: 848x480x30 — proven-safe envelope from GB10 USB stability validation.

set -eo pipefail  # NOT -u: ROS2 setup.bash references unbound vars (AMENT_TRACE_SETUP_FILES)

# ---- SDK: point runtime at our GB10 custom build (librealsense2 2.58.1) ----
# This overrides the apt librealsense2 2.57.7 that ships with ros-jazzy-librealsense2.
# Must be set BEFORE sourcing ROS so our GB10 .so wins the resolution order.
export LD_LIBRARY_PATH="/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# ---- Verbose SDK logging so the serial HIL run pins the failing transfer ----
# The libusb backend logs the failing control_transfer index (expect 768 / 0x0300 if the
# hypothesis is wrong and a depth-XU write still fires). See analysis doc §5.
export LRS_LOG_LEVEL=DEBUG

# ---- ROS2 Jazzy environment ----
# shellcheck source=/opt/ros/jazzy/setup.bash
source /opt/ros/jazzy/setup.bash

# ---- Our colcon workspace (realsense2_camera + realsense2_camera_msgs) ----
# shellcheck source=/home/damartel/realsense-gb10-validation/ros2-ws/install/setup.bash
source /home/damartel/realsense-gb10-validation/ros2-ws/install/setup.bash

# ---- Confirm we are NOT about to load the wrong librealsense2 ----
# Checks LD_LIBRARY_PATH directly (not ldconfig, which ignores LD_LIBRARY_PATH and would
# always report the apt path, causing false warnings on every correct launch).
GB10_SO="/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib/librealsense2.so.2.58.1"
if [[ ! -f "${GB10_SO}" ]]; then
    echo "[ERROR] GB10 SDK .so not found at expected path: ${GB10_SO}"
    echo "[ERROR] Cannot guarantee correct SDK will be loaded. Aborting."
    exit 1
fi

# ---- Optional: tee output to a HIL log next to the other validation runs ----
HIL_LOG="${HOME}/realsense-gb10-validation/ros2-depth-minimal-$(date +%Y%m%d-%H%M%S).log"

echo "[depth-minimal] *** VERIFIED depth-only config (HIL 2026-06-05: 30fps, 0 drops) — see docs/gb10/ros2-stream-start-analysis.md ***"
echo "[depth-minimal] SDK: ${LD_LIBRARY_PATH%%:*}"
echo "[depth-minimal] LRS_LOG_LEVEL=${LRS_LOG_LEVEL}"
echo "[depth-minimal] Log: ${HIL_LOG}"
echo "[depth-minimal] Depth-only 848x480x30, auto-exposure ON, no manual depth-XU controls."
echo "[depth-minimal] Extra args: $*"

# Minimal / override param set — rationale per line in the analysis doc §4.
#   - All non-depth streams disabled (depth-only safe envelope, matches known-good pyrealsense2).
#   - depth_module.enable_auto_exposure:=true  -> leave AE ON; do NOT push a manual exposure under it (H1 mitigation).
#   - inter_cam_sync_mode:=0 (default) -> new_val==default => no depth-XU EXT_TRIGGER write.
#   - depth_module.hdr_enabled:=false  -> matches device default; the forced init HDR-disable is caught/non-fatal.
#   - initial_reset:=false             -> do NOT hardware_reset the fragile xHCI controller at start.
#   - enable_sync:=false               -> single stream; avoid extra alignment-monitor churn.
# NOTE: depth_module.exposure is deliberately NOT passed. (Caveat: rs_launch.py still declares its
#       default 8500; if 0x0300 still fires, see analysis doc §5 for the next step.)
exec ros2 launch realsense2_camera rs_launch.py \
    enable_color:=false \
    enable_gyro:=false \
    enable_accel:=false \
    enable_infra1:=false \
    enable_infra2:=false \
    enable_sync:=false \
    initial_reset:=false \
    depth_module.depth_profile:=848x480x30 \
    depth_module.enable_auto_exposure:=true \
    depth_module.hdr_enabled:=false \
    depth_module.inter_cam_sync_mode:=0 \
    "$@" 2>&1 | tee "${HIL_LOG}"
