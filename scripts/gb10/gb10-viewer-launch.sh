#!/usr/bin/env bash
# GB10 RealSense Viewer launcher — the SINGLE source of env truth for the Desktop entry.
# Sets up the runtime (pyrealsense2 from build-gb10-full + cv2 needing opencv/lib AND ffmpeg/lib +
# DISPLAY + NVENC ffmpeg), does a friendly preflight, then opens the interactive debug viewer.
# Any args pass through to the viewer (e.g. --stream depth, --profile 6). `--validate` runs the
# automated PASS/FAIL display validation instead of the interactive viewer.
set -uo pipefail
SG="$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=scripts/gb10/gb10-env.sh
source "$SG/gb10-env.sh"   # single source of env truth FIRST (LD_LIBRARY_PATH/PYTHONPATH/DISPLAY/LRS_FFMPEG/LRS_VENV/LRS_BUILD_RELEASE)

VENV="${LRS_VENV:?gb10-env did not export LRS_VENV}"
FULL="$LRS_BUILD_RELEASE"
TOOL="$SG/rs-gb10-nonheadless-verify.py"

echo "============================================================"
echo " GB10 RealSense Viewer"
echo "============================================================"

# friendly preflight (don't dump a traceback on a double-click)
miss=""
[ -x "$VENV" ] || miss="venv python ($VENV)"
compgen -G "$FULL/pyrealsense2.cpython-*-aarch64-linux-gnu.so" >/dev/null || miss="${miss:+$miss; }GB10 pyrealsense2 build ($FULL)"
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
