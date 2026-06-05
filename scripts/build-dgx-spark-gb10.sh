#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${LRS_GB10_BUILD_DIR:-/opt/vigil/build/librealsense-v2.58.1-dgx-spark-gb10}"
PREFIX="${LRS_GB10_PREFIX:-/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
CUDA_ARCH="${LRS_GB10_CUDA_ARCH:-121}"
JOBS="${LRS_GB10_JOBS:-$(nproc)}"
PYTHON_EXECUTABLE="${PYTHON_EXECUTABLE:-$(command -v python3)}"
PYTHON_VERSION="$("$PYTHON_EXECUTABLE" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_INSTALL_DIR="${LRS_GB10_PYTHON_INSTALL_DIR:-$PREFIX/lib/python${PYTHON_VERSION}/site-packages/pyrealsense2}"
# pyrealsense2 ABI guard + venv pin (REPRODUCIBILITY). Modern CMake FindPython can silently grab a
# different interpreter (e.g. ~/.local/bin/python3 == a 3.15 pre-release) for Development.Module even
# when PYTHON_EXECUTABLE points at a venv 3.12 — producing a pyrealsense2.so that won't import in the
# venv (undefined symbol _PyThreadState_UncheckedGet). Pin FindPython to THIS interpreter's prefix.
PYTHON_PREFIX="$("$PYTHON_EXECUTABLE" -c 'import sys; print(sys.prefix)')"
PYTHON_PRERELEASE="$("$PYTHON_EXECUTABLE" -c 'import sys; print("1" if sys.version_info.releaselevel != "final" else "0")')"
if [[ "$PYTHON_PRERELEASE" == "1" ]]; then
  echo "ERROR: PYTHON_EXECUTABLE=$PYTHON_EXECUTABLE is a pre-release Python ($PYTHON_VERSION); its ABI" >&2
  echo "       differs from stable cpython and pyrealsense2 built against it will not import in a" >&2
  echo "       stable venv. Point PYTHON_EXECUTABLE at a stable venv (e.g. a uv venv 3.12):" >&2
  echo "       PYTHON_EXECUTABLE=/path/to/.venv/bin/python scripts/build-dgx-spark-gb10.sh" >&2
  exit 1
fi
GENERATOR="${LRS_GB10_GENERATOR:-Ninja}"
# Toolchain arch: default -mcpu=native (best perf on THIS host, but binaries are host-CPU-specific and
# GCC 13.3 silently degrades 'native' to an armv8-a baseline on Cortex-X925). Set LRS_GB10_REPRODUCIBLE=1
# for an explicit, portable, reproducible GB10 arch (-mcpu=cortex-x925) instead of 'native'.
if [[ "${LRS_GB10_REPRODUCIBLE:-0}" == "1" ]]; then
  ARCH_FLAG="${LRS_GB10_ARCH:--mcpu=cortex-x925}"
else
  ARCH_FLAG="${LRS_GB10_ARCH:--mcpu=native}"
fi
NATIVE_FLAGS="${LRS_GB10_NATIVE_FLAGS:--O3 -DNDEBUG $ARCH_FLAG -ffunction-sections -fdata-sections}"
LINK_FLAGS="${LRS_GB10_LINK_FLAGS:--Wl,--gc-sections}"
CXX_STANDARD="${LRS_GB10_CXX_STANDARD:-20}"
WITH_DDS="${LRS_GB10_WITH_DDS:-ON}"
WITH_OPENMP="${LRS_GB10_WITH_OPENMP:-ON}"
WITH_IPO="${LRS_GB10_WITH_IPO:-OFF}"
WITH_EXTERNAL_LZ4="${LRS_GB10_EXTERNAL_LZ4:-OFF}"
FORCE_RSUSB="${LRS_GB10_FORCE_RSUSB:-ON}"
# Enable GB10-specific USB mitigations (P2 URB pool depth + P4 watchdog rate-limit + stop settle).
# Set LRS_GB10_USB_TUNING=0 to produce a vanilla build without the GB10 defaults baked in.
GB10_USB_TUNING="${LRS_GB10_USB_TUNING:-1}"
# CUDA cached-buffer ladders, PROMOTED TO DEFAULT (measured: pointcloud 3.3x faster, conversion
# ~NEON-parity; both byte-identical to baseline, max-abs-diff 0). Default ON here: the ladder is
# compiled in AND the runtime default is mode 1 (cached) -- see rs2_pc_mode()/rs2_conv_mode().
# These defines are #if-guarded, so an UPSTREAM cmake build without them is byte-identical; only this
# GB10 build profile bakes the cached path in. Set =0 to opt back out to the per-frame-malloc baseline.
GB10_PC_ZEROCOPY="${LRS_GB10_PC_ZEROCOPY:-1}"
GB10_CONV_CACHE="${LRS_GB10_CONV_CACHE:-1}"
# Opt-in to building the unit-test target alongside the SDK (off by default in GB10 builds to
# avoid requiring Catch2 unless the user explicitly wants tests).
BUILD_UNIT_TESTS="${LRS_GB10_BUILD_UNIT_TESTS:-OFF}"
# CUDA-enabled OpenCV prefix (GB10 build from /opt/gb10-cuda/install/opencv).
# When the cmake config is present at <prefix>/lib/cmake/opencv4/OpenCVConfig.cmake the build
# passes -DOpenCV_DIR pointing at that directory so the CV examples/wrappers (cv-helpers,
# depth-quality, KinFu) link the CUDA OpenCV 4.14 instead of the stock Ubuntu 4.6.0.
# To fall back to system OpenCV, point at a path that does not exist:
#   LRS_GB10_OPENCV_DIR=/dev/null/no-opencv  scripts/build-dgx-spark-gb10.sh configure
# (An empty string restores the default due to bash :- substitution semantics.)
LRS_GB10_OPENCV_DIR="${LRS_GB10_OPENCV_DIR:-/opt/gb10-cuda/install/opencv}"
MODE="all"

usage() {
  cat <<'USAGE'
Usage: scripts/build-dgx-spark-gb10.sh [mode]

Build librealsense for NVIDIA DGX Spark / GB10 class systems.

Modes:
  configure  Configure CMake only
  build      Build existing configured tree
  install    Install existing configured tree
  validate   Validate installed SDK, tools, pkg-config, and Python binding
  all        Configure, build, install, and validate (default)

Useful environment:
  LRS_GB10_BUILD_DIR       Build directory
  LRS_GB10_PREFIX          Install prefix
  LRS_GB10_CUDA_ARCH       CUDA arch, default 121; use 120 if NVCC rejects 121
  LRS_GB10_WITH_DDS        Enable RealDDS/FastDDS support, default ON
  LRS_GB10_WITH_OPENMP     Enable OpenMP, default ON
  LRS_GB10_WITH_IPO        Enable release IPO/LTO, default OFF
  LRS_GB10_EXTERNAL_LZ4    Use external LZ4 CMake package, default OFF
  LRS_GB10_FORCE_RSUSB     Force libusb/RSUSB backend, default ON.
                           Set OFF to validate the native Linux V4L2 backend.
  LRS_GB10_CXX_STANDARD    C++ standard for unpinned targets, default 20
  PYTHON_EXECUTABLE        Python ABI for pyrealsense2
  LRS_GB10_PYTHON_INSTALL_DIR
                           Python install dir, default under the GB10 prefix
  LRS_GB10_USB_TUNING      Bake in GB10 USB mitigations (RS2_GB10_USB_TUNING=1),
                           default 1 (ON). Set to 0 for a vanilla build.
  LRS_GB10_PC_ZEROCOPY     Pointcloud cached-buffer ladder, default 1 (ON +
                           runtime mode 1 = cached, 3.3x faster). Set 0 to opt out
                           (or RS2_PC_MODE=0 at runtime for the malloc baseline).
  LRS_GB10_CONV_CACHE      YUYV->color cached-buffer ladder, default 1 (ON +
                           runtime mode 1 = cached, ~NEON-parity). Set 0 to opt out
                           (or RS2_CONV_MODE=0 at runtime for the malloc baseline).
  LRS_GB10_BUILD_UNIT_TESTS
                           Pass BUILD_UNIT_TESTS to CMake so the Catch2 unit-test
                           targets are also built, default OFF.
  LRS_GB10_OPENCV_DIR      Path to a CUDA-enabled OpenCV install prefix.  When the
                           CMake config exists at <prefix>/lib/cmake/opencv4/ the
                           build passes -DOpenCV_DIR to that directory so the CV
                           examples and wrappers (cv-helpers, depth-quality, KinFu)
                           link the CUDA OpenCV instead of the stock Ubuntu 4.6.0.
                           Default: /opt/gb10-cuda/install/opencv (the GB10 CUDA
                           media stack built by the gb10-cuda Codex session).
                           To skip and fall back to whatever CMake finds on the
                           system, point at a non-existent path (bash :- means
                           empty string restores the default):
                             LRS_GB10_OPENCV_DIR=/dev/null/no-opencv ...
USAGE
}

if [[ $# -gt 0 ]]; then
  MODE="$1"
fi

case "$MODE" in
  -h|--help)
    usage
    exit 0
    ;;
  configure|build|install|validate|all) ;;
  *)
    usage >&2
    exit 2
    ;;
esac

have() {
  command -v "$1" >/dev/null 2>&1
}

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

prepare_dirs() {
  for dir in "$BUILD_DIR" "$PREFIX"; do
    if [[ ! -d "$dir" ]]; then
      run_privileged mkdir -p "$dir"
    fi
    if [[ ! -w "$dir" ]]; then
      run_privileged chown "$(id -un):$(id -gn)" "$dir"
    fi
  done
}

configure() {
  prepare_dirs

  local generator_args=()
  if [[ "$GENERATOR" == "Ninja" && -z "$(command -v ninja || true)" ]]; then
    GENERATOR="Unix Makefiles"
  fi
  generator_args=(-G "$GENERATOR")

  local launcher=""
  if have sccache; then
    launcher="sccache"
  elif have ccache; then
    launcher="ccache"
  fi

  local launcher_args=()
  if [[ -n "$launcher" ]]; then
    launcher_args=(
      -DCMAKE_C_COMPILER_LAUNCHER="$launcher"
      -DCMAKE_CXX_COMPILER_LAUNCHER="$launcher"
      -DCMAKE_CUDA_COMPILER_LAUNCHER="$launcher"
    )
  fi

  local enable_legacy_ccache="ON"
  if [[ -n "$launcher" ]]; then
    enable_legacy_ccache="OFF"
  fi

  # Wire the CUDA-enabled OpenCV if the install prefix has a valid cmake config.
  # The config file path was verified on 2026-06-03: OpenCV 4.14.0 at
  #   /opt/gb10-cuda/install/opencv/lib/cmake/opencv4/OpenCVConfig.cmake
  # Falls back to stock CMake OpenCV discovery when the prefix is absent or empty.
  local opencv_args=()
  local _opencv_cmake_dir="${LRS_GB10_OPENCV_DIR}/lib/cmake/opencv4"
  if [[ -n "${LRS_GB10_OPENCV_DIR:-}" && -f "${_opencv_cmake_dir}/OpenCVConfig.cmake" ]]; then
    opencv_args=(-DOpenCV_DIR="${_opencv_cmake_dir}")
    echo "LRS_GB10: using CUDA OpenCV from ${_opencv_cmake_dir}"
  elif [[ -n "${LRS_GB10_OPENCV_DIR:-}" ]]; then
    echo "LRS_GB10: WARNING: LRS_GB10_OPENCV_DIR='${LRS_GB10_OPENCV_DIR}' set but" \
         "${_opencv_cmake_dir}/OpenCVConfig.cmake not found; falling back to system OpenCV"
  fi

  # The KinFu example needs OpenCV's rgbd contrib module (opencv2/rgbd/kinfu.hpp). The GB10 CUDA OpenCV
  # build does NOT ship it, so a hard -DBUILD_CV_KINFU_EXAMPLE=ON fails the ENTIRE build at the rs-kinfu
  # target (observed: every pyver build, e.g. py3.14). Auto-detect the header; LRS_GB10_CV_KINFU=ON/OFF overrides.
  local build_cv_kinfu="${LRS_GB10_CV_KINFU:-}"
  if [[ -z "$build_cv_kinfu" ]]; then
    if [[ -f "${LRS_GB10_OPENCV_DIR}/include/opencv4/opencv2/rgbd/kinfu.hpp" ]]; then
      build_cv_kinfu=ON
    else
      build_cv_kinfu=OFF
      echo "LRS_GB10: opencv rgbd/kinfu.hpp not found -> BUILD_CV_KINFU_EXAMPLE=OFF (LRS_GB10_CV_KINFU=ON to force)"
    fi
  fi

  cmake -S "$ROOT" -B "$BUILD_DIR" "${generator_args[@]}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_C_FLAGS_RELEASE="$NATIVE_FLAGS" \
    -DCMAKE_CXX_FLAGS_RELEASE="$NATIVE_FLAGS" \
    -DCMAKE_CXX_STANDARD="$CXX_STANDARD" \
    -DCMAKE_CXX_STANDARD_REQUIRED=ON \
    -DCMAKE_EXE_LINKER_FLAGS_RELEASE="$LINK_FLAGS" \
    -DCMAKE_SHARED_LINKER_FLAGS_RELEASE="$LINK_FLAGS" \
    -DCMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE="$WITH_IPO" \
    -DCUDA_TOOLKIT_ROOT_DIR="$CUDA_HOME" \
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" \
    -DFORCE_RSUSB_BACKEND="$FORCE_RSUSB" \
    -DBUILD_WITH_CUDA=ON \
    -DBUILD_WITH_NEON=ON \
    -DBUILD_WITH_CPU_EXTENSIONS=ON \
    -DBUILD_WITH_OPENMP="$WITH_OPENMP" \
    -DBUILD_TOOLS=ON \
    -DBUILD_EXAMPLES=ON \
    -DBUILD_GRAPHICAL_EXAMPLES=ON \
    -DBUILD_GLSL_EXTENSIONS=ON \
    -DBUILD_CV_EXAMPLES=ON \
    -DBUILD_CV_KINFU_EXAMPLE="$build_cv_kinfu" \
    -DBUILD_PCL_EXAMPLES=ON \
    -DBUILD_PC_STITCHING=ON \
    -DBUILD_OPENNI2_BINDINGS=ON \
    -DBUILD_PYTHON_BINDINGS=ON \
    -DPYTHON_EXECUTABLE="$PYTHON_EXECUTABLE" \
    -DPython_EXECUTABLE="$PYTHON_EXECUTABLE" \
    -DPython_ROOT_DIR="$PYTHON_PREFIX" \
    -DPython_FIND_VIRTUALENV=ONLY \
    -DPYTHON_INSTALL_DIR="$PYTHON_INSTALL_DIR" \
    -DBUILD_ROSBAG2=ON \
    -DBUILD_WITH_DDS="$WITH_DDS" \
    -DUSE_EXTERNAL_LZ4="$WITH_EXTERNAL_LZ4" \
    -DENABLE_CCACHE="$enable_legacy_ccache" \
    -DENABLE_EASYLOGGINGPP_ASYNC=ON \
    -DCHECK_FOR_UPDATES=OFF \
    -DRS2_GB10_USB_TUNING="$GB10_USB_TUNING" \
    -DRS2_GB10_PC_ZEROCOPY="$GB10_PC_ZEROCOPY" \
    -DRS2_GB10_CONV_CACHE="$GB10_CONV_CACHE" \
    -DBUILD_UNIT_TESTS="$BUILD_UNIT_TESTS" \
    "${launcher_args[@]}" \
    "${opencv_args[@]}"
}

build() {
  cmake --build "$BUILD_DIR" --parallel "$JOBS"
}

install_sdk() {
  run_privileged cmake --install "$BUILD_DIR"
}

validate() {
  export LD_LIBRARY_PATH="$PREFIX/lib:${LD_LIBRARY_PATH:-}"
  export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

  "$PREFIX/bin/rs-enumerate-devices" --version
  "$PREFIX/bin/rs-gb10-profiler" --list-profiles --output-dir "" --no-evidence
  "$PREFIX/bin/rs-dds-sniffer" --help >/dev/null
  "$PREFIX/bin/rs-dds-config" --help >/dev/null
  "$PREFIX/bin/rs-dds-adapter" --help >/dev/null
  pkg-config --modversion realsense2
  pkg-config --libs realsense2 | grep -F -- "-L$PREFIX/lib" >/dev/null

  local smoke_dir="$BUILD_DIR/gb10-smoke"
  mkdir -p "$smoke_dir"
  cat >"$smoke_dir/smoke.cpp" <<'CPP'
#include <librealsense2/rs.hpp>
#include <iostream>

int main()
{
    rs2::context ctx;
    auto devices = ctx.query_devices();
    std::cout << "devices=" << devices.size() << '\n';
    return 0;
}
CPP
  c++ -std=c++20 "$smoke_dir/smoke.cpp" $(pkg-config --cflags --libs realsense2) -o "$smoke_dir/smoke"
  "$smoke_dir/smoke"

  local py_pkg_dir py_site_dir
  py_pkg_dir="$(find "$PREFIX" -type d -path '*/site-packages/pyrealsense2' | head -n 1 || true)"
  if [[ -n "$py_pkg_dir" && -e "$py_pkg_dir/__init__.py" ]]; then
    py_site_dir="$(dirname "$py_pkg_dir")"
    PYTHONPATH="$py_site_dir:${PYTHONPATH:-}" "$PYTHON_EXECUTABLE" - <<'PY'
import pyrealsense2 as rs
print("pyrealsense2", getattr(rs, "__file__", "<built-in>"))
print("devices", len(rs.context().query_devices()))
PY
  else
    echo "pyrealsense2 shared object was not found under $PREFIX" >&2
    exit 1
  fi

  "$PREFIX/bin/rs-enumerate-devices" -s || true
}

clean() {
  # Idempotent clean: remove the build dir so the next configure is from scratch (reproducibility).
  echo "Removing build dir: $BUILD_DIR"
  rm -rf "$BUILD_DIR"
}

# Emit a build-provenance manifest (git tag+commit + compile options + toolchain) for reproducibility.
emit_provenance() {
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -x "$here/gb10/gb10-build-info.sh" ]]; then
    "$here/gb10/gb10-build-info.sh" "$BUILD_DIR" >/dev/null 2>&1 \
      && echo "[provenance] $BUILD_DIR/BUILD_PROVENANCE.json  ($(git -C "$here/.." describe --tags --always 2>/dev/null))"
  fi
}

case "$MODE" in
  configure) configure ;;
  build) build; emit_provenance ;;
  install) install_sdk; emit_provenance ;;
  validate) validate ;;
  clean) clean ;;
  all)
    [[ "${LRS_GB10_FRESH:-0}" == "1" ]] && clean
    configure
    build
    install_sdk
    validate
    emit_provenance
    ;;
esac
