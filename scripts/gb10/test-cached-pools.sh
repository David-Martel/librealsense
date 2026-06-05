#!/usr/bin/env bash
# R4: unit-test the GB10 CUDA cached pools (pointcloud + conversion) — NO camera, needs the GPU.
# Compiles test_cached_pools.cu with both cache defines, then for each pool runs mode 0 (baseline) and
# mode 1 (cached) over a GROW/SHRINK resolution sequence and asserts the outputs are byte-identical at
# every resolution. Proves: byte-identical caching, grow-only buffer correctness across size changes,
# and mode selection. Run via `just test-cached`.
set -uo pipefail
SG="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SG/../.." && pwd)"
NVCC="${NVCC:-/usr/local/cuda/bin/nvcc}"
TMP="$(mktemp -d /tmp/cp-XXXXXX)"
BIN="$TMP/test_cached_pools"
# grow to 720, shrink (reuse), grow back, different shape -> exercises cap-reuse + no-shrink + regrow
SEQ=(848x480 1280x720 424x240 1280x720 640x480)
fails=0

echo "== compiling cached-pool test harness (nvcc) =="
WARN="$("$NVCC" -DRS2_USE_CUDA -DRS2_GB10_PC_ZEROCOPY -DRS2_GB10_CONV_CACHE -O2 \
    -I"$REPO/include" -I"$REPO/src" -I"$REPO/src/cuda" \
    -Xcompiler -Wall \
    "$SG/test_cached_pools.cu" "$REPO/src/cuda/cuda-pointcloud.cu" "$REPO/src/cuda/cuda-conversion.cu" \
    -o "$BIN" 2>&1)"
rc=$?
echo "$WARN" | grep -iE "test_cached_pools.cu.*warning" && { echo "  [FAIL] warnings on the harness"; fails=$((fails+1)); }
[ $rc -eq 0 ] || { echo "  [FAIL] nvcc compile (rc=$rc)"; echo "$WARN" | tail -8; rm -rf "$TMP"; exit 1; }
echo "  [PASS] harness compiled"

for pool in conv pc; do
  echo "== $pool: mode0 (baseline) vs mode1 (cached) over ${SEQ[*]} =="
  if [ "$pool" = conv ]; then MV=RS2_CONV_MODE; else MV=RS2_PC_MODE; fi
  env "$MV"=0 "$BIN" "$pool" "$TMP/${pool}-m0" "${SEQ[@]}" >/dev/null || { echo "  [FAIL] mode0 run"; fails=$((fails+1)); continue; }
  env "$MV"=1 "$BIN" "$pool" "$TMP/${pool}-m1" "${SEQ[@]}" >/dev/null || { echo "  [FAIL] mode1 run"; fails=$((fails+1)); continue; }
  ok=1
  for idx in "${!SEQ[@]}"; do
    if cmp -s "$TMP/${pool}-m0-${idx}.bin" "$TMP/${pool}-m1-${idx}.bin"; then
      echo "  [PASS] ${SEQ[$idx]} byte-identical (cached == baseline)"
    else
      echo "  [FAIL] ${SEQ[$idx]} DIFFERS (cache corrupts at this resolution)"; ok=0; fails=$((fails+1))
    fi
  done
  [ $ok -eq 1 ] && echo "  $pool: grow-only + byte-identical + mode-select OK"
done

rm -rf "$TMP"
echo
if [ $fails -eq 0 ]; then echo "CACHED-POOLS TEST: PASS"; else echo "CACHED-POOLS TEST: FAIL ($fails)"; fi
exit $fails
