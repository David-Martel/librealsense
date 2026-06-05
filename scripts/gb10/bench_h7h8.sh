#!/usr/bin/env bash
# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
#
# H7 + H8 measure-first feasibility benches (no camera).
#   H7: rs.align per-frame allocation overhead (cached vs counterfactual churn).
#   H8: USB-event vs CUDA CPU affinity/pinning jitter (PROXY).
#
# Usage: scripts/gb10/bench_h7h8.sh [h7|h8|all]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCR="$REPO/scripts/gb10"
WHICH="${1:-all}"

run_h7() {
    echo "### H7: align allocation microbench (nvcc) ###"
    command -v nvcc >/dev/null || { echo "nvcc not found; skipping H7"; return 0; }
    nvcc -O3 -std=c++14 -DRS2_USE_CUDA -Xcompiler -Wall,-Wextra,-Werror \
        -I"$REPO/include" -I"$REPO/src" -I"$REPO/src/cuda" \
        "$SCR/bench_h7h8_align_alloc.cu" -o "$SCR/bench_h7h8_align_alloc"
    "$SCR/bench_h7h8_align_alloc" "${H7_ITERS:-400}" "${H7_WARMUP:-50}"
}

run_h8() {
    echo "### H8: USB/CUDA affinity jitter microbench (g++) ###"
    g++ -O2 -std=c++14 -Wall -Wextra -Werror -pthread \
        "$SCR/bench_h7h8_affinity.cpp" -o "$SCR/bench_h7h8_affinity"
    # Warn if a heavy build is running (contaminates the tail).
    LOADAVG="$(awk '{print $1}' /proc/loadavg)"
    echo "[loadavg(1m)=$LOADAVG] — for a clean H8 tail this should be near-idle (< ~2)."
    "$SCR/bench_h7h8_affinity" "${H8_SECONDS:-8}" "${H8_CADENCE_US:-200}"
}

case "$WHICH" in
    h7) run_h7 ;;
    h8) run_h8 ;;
    all) run_h7; echo; run_h8 ;;
    *) echo "usage: $0 [h7|h8|all]"; exit 2 ;;
esac
