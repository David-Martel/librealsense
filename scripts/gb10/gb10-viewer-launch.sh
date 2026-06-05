#!/usr/bin/env bash
# GB10 RealSense Viewer launcher — the SINGLE source of env truth for the Desktop entry.
# Sets up the runtime (pyrealsense2 from build-gb10-full + cv2 needing opencv/lib AND ffmpeg/lib +
# DISPLAY + NVENC ffmpeg), does a friendly preflight, then opens the interactive debug viewer.
# Any args pass through to the viewer (e.g. --stream depth, --profile 6). `--validate` runs the
# automated PASS/FAIL display validation instead of the interactive viewer.
set -uo pipefail
SG="$(cd "$(dirname "$0")" && pwd)"
VENV="${LRS_VENV:-$HOME/realsense-gb10-validation/.venv/bin/python}"
OPENCV=/opt/gb10-cuda/install/opencv
FFLIB=/opt/gb10-cuda/install/ffmpeg/lib
FULL="$HOME/realsense-gb10-validation/build-gb10-full/Release"
TOOL="$SG/rs-gb10-nonheadless-verify.py"

export LD_LIBRARY_PATH="$OPENCV/lib:$FFLIB:$FULL"
export PYTHONPATH="$SG:$FULL:$OPENCV/lib/python3.12/site-packages"
export DISPLAY="${DISPLAY:-:1}"
export LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg

echo "============================================================"
echo " GB10 RealSense Viewer"
echo "============================================================"

# friendly preflight (don't dump a traceback on a double-click)
miss=""
[ -x "$VENV" ] || miss="venv python ($VENV)"
[ -e "$FULL/pyrealsense2.cpython-312-aarch64-linux-gnu.so" ] || miss="${miss:+$miss; }GB10 pyrealsense2 build ($FULL)"
if [ -n "$miss" ]; then
  echo "!! Missing runtime: $miss"; read -r -p "Press Enter to close..." _; exit 1
fi
if journalctl -k --no-pager -n 30 2>/dev/null | grep -qiE "HC died|not responding to stop"; then
  echo "!! The USB controller shows a prior death — please REBOOT before using the camera."
  read -r -p "Press Enter to close..." _; exit 1
fi
if ! lsusb 2>/dev/null | grep -qiE "Intel.*RealSense|8086:0b07"; then
  echo "!! No RealSense camera detected on USB. Plug it into a USB-3 port and retry."
  read -r -p "Press Enter to close..." _; exit 1
fi

mode="--interactive"
if [ "${1:-}" = "--validate" ]; then mode=""; shift; fi
echo " Controls: 1-9 profile  c/d color/depth  e emitter  a auto-exp  [ ] exposure"
echo "           - = gain  l/L laser  p preset  f freeze  r re-acquire  s snapshot  q quit"
echo "------------------------------------------------------------"
exec "$VENV" "$TOOL" $mode "$@"
