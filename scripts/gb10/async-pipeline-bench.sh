#!/usr/bin/env bash
# P4 (#31): build + run the async-pipelining microbench — NO camera, needs the GPU.
#
# Compiles bench_async_pipeline.cu (linking the real cuda-pointcloud.cu + cuda-conversion.cu so the
# IDENTICAL library kernels run) with -O3 and -Werror all-warnings, then runs it. Compares, over many
# warm iterations at 848x480 and 1280x720 with synthetic input:
#   REF = shipped cached path (mode 1, pageable host + internal sync)
#   A   = pinned host, single stream, sync per frame (serialized H2D->kernel->D2H)
#   B   = pinned host, K-deep multi-stream, cudaMemcpyAsync (overlap copy/compute across frames)
# Reports steady-state fps + per-frame latency (mean/p50/p95) and a per-stage (H2D/kernel/D2H) table.
#
# Run via `just bench-async`  (or directly: bash scripts/gb10/async-pipeline-bench.sh).
set -uo pipefail
SG="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SG/../.." && pwd)"
NVCC="${NVCC:-/usr/local/cuda/bin/nvcc}"
TMP="$(mktemp -d /tmp/asyncpipe-XXXXXX)"
BIN="$TMP/bench_async_pipeline"

echo "== compiling async-pipeline microbench (nvcc -O3 -Werror) =="
# -Werror all-warnings: fail on ANY nvcc/ptxas warning.  -Xcompiler ...: strict host (gcc) warnings.
WARN="$("$NVCC" -DRS2_USE_CUDA -DRS2_GB10_PC_ZEROCOPY -DRS2_GB10_CONV_CACHE -O3 \
    -I"$REPO/include" -I"$REPO/src" -I"$REPO/src/cuda" \
    "$SG/bench_async_pipeline.cu" \
    "$REPO/src/cuda/cuda-pointcloud.cu" "$REPO/src/cuda/cuda-conversion.cu" \
    -Werror all-warnings -Xcompiler -Wall,-Wextra,-Werror \
    -o "$BIN" 2>&1)"
rc=$?
if [ -n "$WARN" ]; then echo "$WARN"; fi
if [ $rc -ne 0 ]; then echo "  [FAIL] nvcc compile (rc=$rc)"; rm -rf "$TMP"; exit 1; fi
echo "  [PASS] compiled -Werror clean"
echo

# Best-effort clock pinning so steady-state numbers are stable. Non-fatal if it needs root.
# Lock the SM to its max clock (idle GPU otherwise sits at ~300-700 MHz and skews short kernels).
PINNED="no"
LOCKER=""
if command -v nvidia-smi >/dev/null 2>&1; then
    MAXSM="$(nvidia-smi --query-gpu=clocks.max.sm --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
    if [ -n "$MAXSM" ]; then
        if nvidia-smi -lgc "0,$MAXSM" >/dev/null 2>&1; then PINNED="lgc(max=$MAXSM)"; LOCKER="nvidia-smi";
        elif sudo -n nvidia-smi -lgc "0,$MAXSM" >/dev/null 2>&1; then PINNED="sudo-lgc(max=$MAXSM)"; LOCKER="sudo nvidia-smi"; fi
    fi
fi
SMCLK="$(nvidia-smi --query-gpu=clocks.sm --format=csv,noheader 2>/dev/null | head -1 || true)"
echo "== running (clocks pinned: $PINNED, sm clock now: ${SMCLK:-unknown}) =="
if [ "$PINNED" = "no" ]; then
    echo "   NOTE: clocks NOT pinned (needs root: 'sudo nvidia-smi -lgc 0,<max>'). Numbers may"
    echo "   vary with DVFS; the long warmup mitigates but does not eliminate this."
fi
echo

# Background-sample the sustained SM clock during the run (disclosure: GB10 DVFS may ignore -lgc).
CLKLOG="$TMP/smclk.log"
( while :; do nvidia-smi --query-gpu=clocks.sm --format=csv,noheader,nounits 2>/dev/null | head -1; sleep 0.25; done ) >"$CLKLOG" 2>/dev/null &
SAMPLER=$!

"$BIN"
brc=$?

kill "$SAMPLER" 2>/dev/null || true
if [ -s "$CLKLOG" ]; then
    # min / max / last sustained SM clock observed while the bench ran
    awk 'NF{v[NR]=$1; if(min==""||$1<min)min=$1; if($1>max)max=$1; last=$1}
         END{if(NR)printf "\n== sustained SM clock during run: min=%s max=%s last=%s MHz (samples=%d) ==\n", min, max, last, NR}' "$CLKLOG"
fi

# Restore default clocks if we locked them.
if [ -n "$LOCKER" ]; then $LOCKER -rgc >/dev/null 2>&1 || true; fi

rm -rf "$TMP"
exit $brc
