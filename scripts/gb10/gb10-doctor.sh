#!/usr/bin/env bash
# gb10-doctor — one-command GB10 RealSense environment preflight. Checks every runtime prerequisite a
# new system/user needs, with PASS/WARN/FAIL, so problems are self-diagnosable. Does NOT open the
# camera (only checks USB presence). Run via `just gb10-doctor`.
set -uo pipefail
VENV="${LRS_VENV:-$HOME/realsense-gb10-validation/.venv/bin/python}"
FULL="$HOME/realsense-gb10-validation/build-gb10-full/Release"
OPENCV=/opt/gb10-cuda/install/opencv
FFLIB=/opt/gb10-cuda/install/ffmpeg/lib
SDK=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10
CUDA="${CUDA_HOME:-/usr/local/cuda}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

fails=0; warns=0
P() { printf '  \033[32m[PASS]\033[0m %s\n' "$1"; }
W() { printf '  \033[33m[WARN]\033[0m %s\n' "$1"; warns=$((warns+1)); }
F() { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; fails=$((fails+1)); }

echo "==================== GB10 RealSense doctor ===================="

echo "-- toolchain & runtime --"
[ -x "$VENV" ] && { v=$("$VENV" -c 'import sys;print("%d.%d"%sys.version_info[:2])'); [ "$v" = 3.12 ] && P "venv python $v" || W "venv python is $v (expected 3.12 for the cpython-312 pyrealsense2)"; } || F "venv python missing ($VENV)"
[ -x "$CUDA/bin/nvcc" ] && P "CUDA $("$CUDA/bin/nvcc" --version | grep -oE 'release [0-9.]+' | awk '{print $2}')" || W "nvcc not found at $CUDA/bin (CUDA builds need it)"
command -v gcc >/dev/null && P "gcc $(gcc -dumpfullversion)" || F "gcc missing"

echo "-- librealsense GB10 build --"
SO="$FULL/pyrealsense2.cpython-312-aarch64-linux-gnu.so"
if [ -e "$SO" ]; then
  if LD_LIBRARY_PATH="$FULL" PYTHONPATH="$FULL" "$VENV" -c "import pyrealsense2 as r;assert r.__version__" 2>/dev/null; then
    P "pyrealsense2 imports ($(LD_LIBRARY_PATH=$FULL PYTHONPATH=$FULL "$VENV" -c 'import pyrealsense2 as r;print(r.__version__)' 2>/dev/null))"
  else F "pyrealsense2 present but fails to import (ABI mismatch? check venv python version)"; fi
else F "build-gb10-full pyrealsense2 missing — run scripts/build-dgx-spark-gb10.sh"; fi
[ -f "$REPO/.git/HEAD" ] && P "source tree $(git -C "$REPO" describe --tags --always --dirty 2>/dev/null)" || W "not a git checkout (no build tag)"

echo "-- cv2 / opencv / ffmpeg (display + quality tools) --"
if [ -d "$OPENCV/lib" ] && [ -d "$FFLIB" ]; then
  if LD_LIBRARY_PATH="$OPENCV/lib:$FFLIB" PYTHONPATH="$OPENCV/lib/python3.12/site-packages" "$VENV" -c "import cv2" 2>/dev/null; then
    P "cv2 imports (opencv/lib + ffmpeg/lib on LD_LIBRARY_PATH)"
  else F "cv2 import fails — need BOTH $OPENCV/lib AND $FFLIB on LD_LIBRARY_PATH (libswresample.so.6)"; fi
else W "gb10-cuda opencv/ffmpeg not at /opt/gb10-cuda/install (display/quality tools unavailable)"; fi
FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg; [ -x "$FFMPEG" ] || FFMPEG=$(command -v ffmpeg || true)
if [ -n "$FFMPEG" ]; then
  # the gb10-cuda ffmpeg needs its own libs to list encoders. Capture to a var (NOT piped to grep -q):
  # under `set -o pipefail`, grep -q's early-exit SIGPIPEs ffmpeg and the pipeline reads as failed.
  ENC="$(LD_LIBRARY_PATH="$FFLIB" "$FFMPEG" -hide_banner -encoders 2>/dev/null)"
  case "$ENC" in *h264_nvenc*) P "ffmpeg NVENC (h264_nvenc) available";; *) W "ffmpeg present but no h264_nvenc (GPU recording unavailable)";; esac
else W "no ffmpeg found"; fi

echo "-- OpenGL / display --"
[ -e "$SDK/lib/librealsense2-gl.so" ] && P "installed GL SDK present (keep-on-GPU viewer)" || W "no librealsense2-gl.so at $SDK/lib (just hil-keepongpu unavailable)"
if [ -n "${DISPLAY:-}" ]; then
  command -v xdpyinfo >/dev/null && { xdpyinfo >/dev/null 2>&1 && P "DISPLAY=$DISPLAY reachable ($(xdpyinfo 2>/dev/null | grep -m1 dimensions | awk '{print $2}'))" || W "DISPLAY=$DISPLAY set but not reachable"; } || P "DISPLAY=$DISPLAY set"
else W "DISPLAY unset (non-headless display tools need it; default :1)"; fi

echo "-- camera & controller (no device opened) --"
# capture to vars (not piped to grep -q) so `set -o pipefail` + grep's early-exit can't misreport
JRNL="$(journalctl -k --no-pager -n 40 2>/dev/null || true)"
case "$JRNL" in *"HC died"*|*"not responding to stop"*) F "USB controller shows a prior HC-died — REBOOT before any HIL";;
  *) P "controller GREEN (no recent HC-died / -110 storm)";; esac
USB="$(lsusb 2>/dev/null || true)"
if printf '%s' "$USB" | grep -qiE "Intel.*RealSense|8086:0b07"; then
  P "RealSense camera present on USB"
  UT="$(lsusb -t 2>/dev/null || true)"
  case "$UT" in *5000M*|*10000M*) P "a USB-3 (5000M+) link is present";; *) W "no USB-3 link seen in lsusb -t (USB-2 is death-prone — use a USB-3 port)";; esac
else F "no RealSense camera detected on USB"; fi

echo "=============================================================="
if [ $fails -eq 0 ]; then printf '\033[32mDOCTOR: HEALTHY\033[0m'; else printf '\033[31mDOCTOR: %d FAIL\033[0m' "$fails"; fi
[ $warns -gt 0 ] && printf ' (%d warn)' "$warns"; echo
exit $fails
