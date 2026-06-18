# shellcheck shell=bash
# gb10-env.sh — single source of env truth for the GB10 RealSense tools. SOURCE it (don't execute):
#   source "$(dirname "${BASH_SOURCE[0]}")/gb10-env.sh"
# Sets LD_LIBRARY_PATH (pyrealsense2 from build-gb10-full + cv2's opencv & ffmpeg libs), PYTHONPATH
# (scripts/gb10 + the build + opencv site-packages), LRS_FFMPEG, DISPLAY. Override any input via the
# calling env before sourcing. Parameterized by LRS_PY_TAG so the py3.13/3.14 retarget is a one-var flip.
: "${LRS_VALIDATION_DIR:=$HOME/realsense-gb10-validation}"
: "${LRS_PY_TAG:=python3.12}"                 # opencv site-packages pyver + SDK build/venv selector; flip to python3.13/3.14 on retarget
: "${LRS_OPENCV_BASE:=/opt/gb10-cuda/install/opencv}"
: "${LRS_FFMPEG_BASE:=/opt/gb10-cuda/install/ffmpeg}"
: "${LRS_FFMPEG:=$LRS_FFMPEG_BASE/bin/ffmpeg}"

# Map LRS_PY_TAG -> its ABI-specific SDK build tree AND matching uv venv. Each Python minor produces a
# distinct pyrealsense2.cpython-<abi>.so, so the build tree and the interpreter MUST move together: a
# 3.13 interpreter loading a 3.12 .so fails with `undefined symbol _PyThreadState_*`. Flipping LRS_PY_TAG
# alone now retargets all three (opencv site-packages, build tree, venv). Override LRS_BUILD_SUBDIR /
# LRS_VENV explicitly to point elsewhere.
case "$LRS_PY_TAG" in
  python3.12) _GB10_DEF_BUILD="build-gb10-full";  _GB10_DEF_VENV=".venv" ;;
  python3.13) _GB10_DEF_BUILD="build-gb10-py313"; _GB10_DEF_VENV=".venv313" ;;
  python3.14) _GB10_DEF_BUILD="build-gb10-py314"; _GB10_DEF_VENV=".venv314" ;;
  *)          _GB10_DEF_BUILD="build-gb10-full";  _GB10_DEF_VENV=".venv" ;;
esac
: "${LRS_BUILD_SUBDIR:=$_GB10_DEF_BUILD}"
: "${LRS_VENV:=$LRS_VALIDATION_DIR/$_GB10_DEF_VENV/bin/python}"

_GB10_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # scripts/gb10
_GB10_FULL="$LRS_VALIDATION_DIR/$LRS_BUILD_SUBDIR/Release"

export LD_LIBRARY_PATH="$LRS_OPENCV_BASE/lib:$LRS_FFMPEG_BASE/lib:$_GB10_FULL${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$_GB10_ENV_DIR:$_GB10_FULL:$LRS_OPENCV_BASE/lib/$LRS_PY_TAG/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export LRS_FFMPEG LRS_VENV
export LRS_BUILD_RELEASE="$_GB10_FULL"   # the resolved <build>/Release dir, for consumers that need it

for _GB10_GUI_ENV in \
  "$HOME/dev/repos/vigil-spark/ops/vigil-network/scripts/vigil-gui-env.sh" \
  "$HOME/vigil-spark/ops/vigil-network/scripts/vigil-gui-env.sh"; do
  if [ -f "$_GB10_GUI_ENV" ]; then
    # shellcheck disable=SC1090
    . "$_GB10_GUI_ENV"
    break
  fi
done
export DISPLAY="${DISPLAY:-:1}"
if [ -z "${QT_QPA_PLATFORM:-}" ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then
  export QT_QPA_PLATFORM=wayland
fi
unset _GB10_GUI_ENV

# Loud ABI guard: if the resolved venv's Python minor doesn't match LRS_PY_TAG, the .so won't import.
# Sourced file -> warn (never exit, that would kill the caller's shell). Skipped if the venv is absent.
if [ -x "$LRS_VENV" ]; then
  # `|| true` so a broken venv python can't take down a `set -e` sourcer (this file is the advertised
  # single env source-of-truth; the ROS2 launchers run under `set -eo`). An empty tag just skips the warn.
  _GB10_VENV_TAG="$("$LRS_VENV" -c 'import sys;print(f"python{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  if [ -n "$_GB10_VENV_TAG" ] && [ "$_GB10_VENV_TAG" != "$LRS_PY_TAG" ]; then
    printf '!! gb10-env: ABI MISMATCH — LRS_PY_TAG=%s but LRS_VENV is %s (%s).\n' \
           "$LRS_PY_TAG" "$_GB10_VENV_TAG" "$LRS_VENV" >&2
    printf '!! pyrealsense2 from %s will fail to import. Set LRS_VENV to a %s interpreter.\n' \
           "$LRS_BUILD_SUBDIR" "$LRS_PY_TAG" >&2
  fi
fi
