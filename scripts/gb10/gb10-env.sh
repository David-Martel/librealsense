# gb10-env.sh — single source of env truth for the GB10 RealSense tools. SOURCE it (don't execute):
#   source "$(dirname "${BASH_SOURCE[0]}")/gb10-env.sh"
# Sets LD_LIBRARY_PATH (pyrealsense2 from build-gb10-full + cv2's opencv & ffmpeg libs), PYTHONPATH
# (scripts/gb10 + the build + opencv site-packages), LRS_FFMPEG, DISPLAY. Override any input via the
# calling env before sourcing. Parameterized by LRS_PY_TAG so the py3.13/3.14 retarget is a one-var flip.
: "${LRS_VALIDATION_DIR:=$HOME/realsense-gb10-validation}"
: "${LRS_PY_TAG:=python3.12}"                 # opencv site-packages pyver; flip to python3.13/3.14 on retarget
: "${LRS_OPENCV_BASE:=/opt/gb10-cuda/install/opencv}"
: "${LRS_FFMPEG_BASE:=/opt/gb10-cuda/install/ffmpeg}"
: "${LRS_FFMPEG:=$LRS_FFMPEG_BASE/bin/ffmpeg}"

_GB10_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # scripts/gb10
_GB10_FULL="$LRS_VALIDATION_DIR/build-gb10-full/Release"

export LD_LIBRARY_PATH="$LRS_OPENCV_BASE/lib:$LRS_FFMPEG_BASE/lib:$_GB10_FULL${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$_GB10_ENV_DIR:$_GB10_FULL:$LRS_OPENCV_BASE/lib/$LRS_PY_TAG/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export LRS_FFMPEG
export DISPLAY="${DISPLAY:-:1}"
