#!/usr/bin/env bash
# Emit a build-provenance manifest (BUILD_PROVENANCE.json) for a GB10 librealsense build.
# Captures the EXACT git state (tag + commit), compile options (read from the build's CMakeCache, not
# assumed), runtime-mode defaults, and toolchain versions — everything needed to reproduce the build on
# another system. Run after a build, or standalone to inspect/verify an existing build.
#
# Usage:  scripts/gb10/gb10-build-info.sh [BUILD_DIR] [OUT_JSON]
#         (defaults: BUILD_DIR=~/realsense-gb10-validation/build-gb10-full, OUT=<BUILD_DIR>/BUILD_PROVENANCE.json)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD="${1:-$HOME/realsense-gb10-validation/build-gb10-full}"
OUT="${2:-$BUILD/BUILD_PROVENANCE.json}"
CACHE="$BUILD/CMakeCache.txt"
cd "$REPO" || { echo "ERROR: cannot cd to repo root: $REPO" >&2; exit 1; }

opt() { grep -m1 "^$1[:=]" "$CACHE" 2>/dev/null | sed 's/^[^=]*=//' || true; }
gd=$(git describe --tags --always --dirty 2>/dev/null || echo unknown)
commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
dirty=$([ -n "$(git status --porcelain 2>/dev/null)" ] && echo true || echo false)
last_src=$(git log -1 --format='%h' -- src/ include/ CMakeLists.txt CMake/ third-party/ 2>/dev/null || echo unknown)
api=$(grep -hoE "RS2_API_(MAJOR|MINOR|PATCH)_VERSION +[0-9]+" include/librealsense2/rs.h 2>/dev/null | grep -oE "[0-9]+$" | paste -sd.)
gccv=$(gcc -dumpfullversion 2>/dev/null || echo unknown)
nvccv=$("${CUDA_HOME:-/usr/local/cuda}/bin/nvcc" --version 2>/dev/null | grep -oE "release [0-9.]+" | awk '{print $2}' || echo none)
so="$BUILD/Release/librealsense2.so"
so_sha=$([ -e "$so" ] && sha256sum "$so" 2>/dev/null | cut -c1-16 || echo none)
built_at=$([ -e "$so" ] && date -r "$so" -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)

# whether the built .so reflects the latest C++ source (count real recompiles, ignoring symlink steps)
if command -v ninja >/dev/null && [ -f "$BUILD/build.ninja" ]; then
    pending=$(ninja -C "$BUILD" -n 2>/dev/null | grep -cE "Building|Linking" || true)
else
    pending="?"
fi
reflects_latest=$([ "$last_src" != unknown ] && git merge-base --is-ancestor "$last_src" "$commit" 2>/dev/null && echo true || echo unknown)

mkdir -p "$(dirname "$OUT")"
BUILD="$BUILD" OUT="$OUT" GD="$gd" COMMIT="$commit" BRANCH="$branch" DIRTY="$dirty" LASTSRC="$last_src" \
API="$api" GCC="$gccv" NVCC="$nvccv" SOSHA="$so_sha" BUILT="$built_at" PENDING="$pending" REFLECTS="$reflects_latest" \
USB_TUNING="$(opt RS2_GB10_USB_TUNING)" PC_ZC="$(opt RS2_GB10_PC_ZEROCOPY)" CONV="$(opt RS2_GB10_CONV_CACHE)" \
CUDA_ON="$(opt BUILD_WITH_CUDA)" NEON="$(opt BUILD_WITH_NEON)" RSUSB="$(opt FORCE_RSUSB_BACKEND)" \
CUARCH="$(opt CMAKE_CUDA_ARCHITECTURES)" BTYPE="$(opt CMAKE_BUILD_TYPE)" \
python3 - <<'PY'
import json, os
d = {
  "schema": "gb10-build-provenance/1",
  "build_tag": os.environ["GD"],
  "git": {"describe": os.environ["GD"], "commit": os.environ["COMMIT"], "branch": os.environ["BRANCH"],
          "dirty": os.environ["DIRTY"] == "true", "last_source_commit": os.environ["LASTSRC"]},
  "api_version": os.environ["API"],
  "binary": {"librealsense2.so_sha256_16": os.environ["SOSHA"], "built_at_utc": os.environ["BUILT"],
             "reflects_latest_source": os.environ["REFLECTS"], "pending_recompiles": os.environ["PENDING"]},
  "compile_options": {
      "RS2_GB10_USB_TUNING": os.environ["USB_TUNING"], "RS2_GB10_PC_ZEROCOPY": os.environ["PC_ZC"],
      "RS2_GB10_CONV_CACHE": os.environ["CONV"], "BUILD_WITH_CUDA": os.environ["CUDA_ON"],
      "BUILD_WITH_NEON": os.environ["NEON"], "FORCE_RSUSB_BACKEND": os.environ["RSUSB"],
      "CMAKE_CUDA_ARCHITECTURES": os.environ["CUARCH"], "CMAKE_BUILD_TYPE": os.environ["BTYPE"]},
  "runtime_defaults": {"RS2_PC_MODE": 1, "RS2_CONV_MODE": 1,
                       "note": "cached paths are the compiled default; set RS2_*_MODE=0 to opt out"},
  "toolchain": {"gcc": os.environ["GCC"], "cuda": os.environ["NVCC"], "host_arch": os.uname().machine},
  "reproduce": ("git checkout %s && LRS_GB10_PC_ZEROCOPY=%s LRS_GB10_CONV_CACHE=%s "
                "scripts/build-dgx-spark-gb10.sh  # needs CUDA %s + gcc %s" ) % (
                os.environ["COMMIT"][:12], os.environ["PC_ZC"] or "1", os.environ["CONV"] or "1",
                os.environ["NVCC"], os.environ["GCC"]),
}
open(os.environ["OUT"], "w").write(json.dumps(d, indent=2) + "\n")
print(json.dumps(d, indent=2))
print("\nwrote:", os.environ["OUT"])
PY
