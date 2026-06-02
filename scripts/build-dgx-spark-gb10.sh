#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${LRS_GB10_BUILD_DIR:-/opt/vigil/build/librealsense-v2.58.1-dgx-spark-gb10}"
PREFIX="${LRS_GB10_PREFIX:-/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.2}"
CUDA_ARCH="${LRS_GB10_CUDA_ARCH:-121}"
JOBS="${LRS_GB10_JOBS:-$(nproc)}"
PYTHON_EXECUTABLE="${PYTHON_EXECUTABLE:-$(command -v python3)}"
PYTHON_VERSION="$("$PYTHON_EXECUTABLE" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTHON_INSTALL_DIR="${LRS_GB10_PYTHON_INSTALL_DIR:-$PREFIX/lib/python${PYTHON_VERSION}/site-packages/pyrealsense2}"
GENERATOR="${LRS_GB10_GENERATOR:-Ninja}"
NATIVE_FLAGS="${LRS_GB10_NATIVE_FLAGS:--O3 -DNDEBUG -mcpu=native -ffunction-sections -fdata-sections}"
LINK_FLAGS="${LRS_GB10_LINK_FLAGS:--Wl,--gc-sections}"
CXX_STANDARD="${LRS_GB10_CXX_STANDARD:-20}"
WITH_DDS="${LRS_GB10_WITH_DDS:-ON}"
WITH_OPENMP="${LRS_GB10_WITH_OPENMP:-ON}"
WITH_IPO="${LRS_GB10_WITH_IPO:-OFF}"
WITH_EXTERNAL_LZ4="${LRS_GB10_EXTERNAL_LZ4:-OFF}"
FORCE_RSUSB="${LRS_GB10_FORCE_RSUSB:-ON}"
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
    -DBUILD_CV_KINFU_EXAMPLE=ON \
    -DBUILD_PCL_EXAMPLES=ON \
    -DBUILD_PC_STITCHING=ON \
    -DBUILD_OPENNI2_BINDINGS=ON \
    -DBUILD_PYTHON_BINDINGS=ON \
    -DPYTHON_EXECUTABLE="$PYTHON_EXECUTABLE" \
    -DPYTHON_INSTALL_DIR="$PYTHON_INSTALL_DIR" \
    -DBUILD_ROSBAG2=ON \
    -DBUILD_WITH_DDS="$WITH_DDS" \
    -DUSE_EXTERNAL_LZ4="$WITH_EXTERNAL_LZ4" \
    -DENABLE_CCACHE="$enable_legacy_ccache" \
    -DENABLE_EASYLOGGINGPP_ASYNC=ON \
    -DCHECK_FOR_UPDATES=OFF \
    "${launcher_args[@]}"
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

case "$MODE" in
  configure) configure ;;
  build) build ;;
  install) install_sdk ;;
  validate) validate ;;
  all)
    configure
    build
    install_sdk
    validate
    ;;
esac
