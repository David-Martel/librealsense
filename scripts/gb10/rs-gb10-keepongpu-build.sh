#!/usr/bin/env bash
# Build the keep-on-GPU viewer against the installed GB10 SDK (consistent librealsense2 + -gl pair,
# with the USB mitigations needed for a live camera). Warnings-as-errors on our translation unit.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${LRS_GB10_SDK:-/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10}"
LIBDIR="$SDK/lib"
OUT="${LRS_KEEPONGPU_OUT:-$HOME/realsense-gb10-validation/rs-gb10-keepongpu-viewer}"
[ -f "$LIBDIR/librealsense2-gl.so" ] || { echo "FATAL: $LIBDIR/librealsense2-gl.so missing (need the GL-enabled SDK)"; exit 1; }
GLFW_CFLAGS="$(pkg-config --cflags glfw3 2>/dev/null || true)"
GLFW_LIBS="$(pkg-config --libs glfw3 2>/dev/null || echo -lglfw)"
g++ -std=c++17 -O2 -Wall -Wextra -Werror "$HERE/rs-gb10-keepongpu-viewer.cpp" \
    -isystem "$SDK/include" $GLFW_CFLAGS \
    -L"$LIBDIR" -Wl,-rpath,"$LIBDIR" \
    -lrealsense2-gl -lrealsense2 $GLFW_LIBS \
    -o "$OUT"
echo "Built (zero warnings under -Wall -Wextra -Werror): $OUT"
ldd "$OUT" | grep -iE "realsense2|glfw" || true
