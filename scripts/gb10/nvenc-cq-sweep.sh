#!/usr/bin/env bash
# nvenc-cq-sweep.sh — NVENC h264_nvenc constant-quality (cq) parameter sweep
# Measures output size, encode fps, and XPSNR quality for each cq × preset combo.
# Run from the repo root or any directory; paths are absolute.
#
# USAGE:
#   bash scripts/gb10/nvenc-cq-sweep.sh [input.mp4 [cq_list [preset_list]]]
#
# EXAMPLES:
#   bash scripts/gb10/nvenc-cq-sweep.sh ~/realsense-gb10-validation/keepongpu-rec.mp4
#   bash scripts/gb10/nvenc-cq-sweep.sh input.mp4 "19 23 26 29 33" "p4 p6"
#   bash scripts/gb10/nvenc-cq-sweep.sh input.mp4 "19 23 26 29 33" "p4"
#
# NOTES:
#   - Encode timing is wall-clock end-to-end (includes H.264 decode of input + NVENC encode).
#     Relative comparisons are valid; this is not a pure NVENC throughput number.
#   - XPSNR is measured against the source in a separate pass (not folded into encode time).
#   - Source is decoded then re-encoded: if source is already H.264 (typical --record output),
#     XPSNR measures re-encode loss relative to that lossy reference, not raw frames.
#   - cq rate control uses: -rc vbr -cq N -b:v 0  (the -b:v 0 suppresses the default target
#     bitrate; without it, NVENC ignores cq and produces flat sizes across the sweep).
#   - Encodes are sequential (one NVENC session at a time) to keep GPU load sane.
#   - GOP is fixed at 30 (1 s at 30 fps) across all encodes for fair comparison.
#   - Temporary encode files are written to /tmp and cleaned up after measurement.
#
# RATE-CONTROL VALIDATION:
#   After all encodes, the script checks that file sizes decrease monotonically as cq rises
#   within each preset.  A warning is printed if this invariant fails (indicates GPU driver
#   or NVENC state issue — rerun after rebooting or clearing NVENC state).

set -euo pipefail

# ── configurable defaults ────────────────────────────────────────────────────
INPUT="${1:-$HOME/realsense-gb10-validation/keepongpu-rec.mp4}"
CQ_LIST="${2:-19 23 26 29 33}"
PRESET_LIST="${3:-p4 p6}"
GOP=30           # I-frame interval; matches 1 s at 30 fps
TMPDIR_BASE=/tmp/nvenc-sweep-$$   # unique per run

# ── ffmpeg location ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source gb10-env.sh to get LRS_FFMPEG + LD_LIBRARY_PATH
if [[ -f "$SCRIPT_DIR/gb10-env.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/gb10-env.sh" 2>/dev/null
fi
FFMPEG="${LRS_FFMPEG:-/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg}"

# ── pre-flight checks ────────────────────────────────────────────────────────
echo "=== nvenc-cq-sweep.sh — pre-flight ==="
echo "  FFMPEG   : $FFMPEG"
echo "  INPUT    : $INPUT"
echo "  CQ_LIST  : $CQ_LIST"
echo "  PRESETS  : $PRESET_LIST"
echo "  GOP      : $GOP"
echo ""

# Verify ffmpeg exists
if [[ ! -x "$FFMPEG" ]]; then
    echo "ERROR: ffmpeg not found at $FFMPEG" >&2
    exit 1
fi

# Verify NVENC is available
# Note: capture into a variable to avoid pipefail interaction when ffmpeg
# streams its encoder list (the left side of the pipe sometimes exits non-zero
# under pipefail even when output is valid; capture sidesteps this).
_ENCODERS=$("$FFMPEG" -hide_banner -encoders 2>&1 || true)
if ! echo "$_ENCODERS" | grep -q "h264_nvenc"; then
    echo "ERROR: h264_nvenc not available in $FFMPEG — NVENC unavailable, stopping." >&2
    echo "       (If GPU is present, check LD_LIBRARY_PATH for NVENC SDK .so files)" >&2
    exit 1
fi
echo "  h264_nvenc: AVAILABLE"

# Verify input exists
if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: input file not found: $INPUT" >&2
    exit 1
fi

# Probe input duration (seconds, float) via ffprobe-compatible -v quiet output.
# ffmpeg -i exits 1 (no output file specified) — || true prevents pipefail abort.
INPUT_DURATION=$(("$FFMPEG" -hide_banner -i "$INPUT" 2>&1 || true) | \
    grep Duration | sed 's/.*Duration: \([0-9:\.]*\).*/\1/' | \
    awk -F: '{ print ($1*3600)+($2*60)+$3 }')
INPUT_SIZE=$(stat -c%s "$INPUT")
echo "  Input duration : ${INPUT_DURATION}s"
echo "  Input size     : ${INPUT_SIZE} bytes"
echo ""

# ── setup ────────────────────────────────────────────────────────────────────
mkdir -p "$TMPDIR_BASE"
RESULTS_FILE="$TMPDIR_BASE/results.tsv"
echo -e "preset\tcq\tsize_bytes\tbitrate_kbps\tencode_s\tspeed_xRT\txpsnr_y\txpsnr_u\txpsnr_v\txpsnr_avg" \
    > "$RESULTS_FILE"

cleanup() {
    rm -rf "$TMPDIR_BASE"
}
trap cleanup EXIT

# ── sweep ────────────────────────────────────────────────────────────────────
echo "=== Sweep: $(echo "$PRESET_LIST" | wc -w) preset(s) × $(echo "$CQ_LIST" | wc -w) cq values ==="
echo ""

for PRESET in $PRESET_LIST; do
    for CQ in $CQ_LIST; do
        OUT="$TMPDIR_BASE/out_${PRESET}_cq${CQ}.mp4"
        echo "--- preset=$PRESET  cq=$CQ ---"

        # ── encode pass (timed) ──────────────────────────────────────────────
        T_START=$(date +%s%N)   # nanoseconds

        "$FFMPEG" -hide_banner -loglevel warning \
            -i "$INPUT" \
            -c:v h264_nvenc \
            -preset "$PRESET" \
            -rc vbr \
            -cq "$CQ" \
            -b:v 0 \
            -g "$GOP" \
            -an \
            -y "$OUT" 2>&1 | grep -v "^$" || true

        T_END=$(date +%s%N)

        # Encode wall time in seconds (float via awk)
        ENCODE_S=$(awk "BEGIN { printf \"%.3f\", ($T_END - $T_START) / 1e9 }")
        # Speed factor: video-seconds / wall-seconds (×real-time).
        # NOT frames/second — the column header is "speed_xRT" throughout.
        # True encode fps = (INPUT_DURATION * 30) / ENCODE_S, but ×RT is more useful here.
        ENCODE_SPEED_XRT=$(awk "BEGIN { printf \"%.1f\", $INPUT_DURATION / $ENCODE_S }")

        # ── file size & bitrate ──────────────────────────────────────────────
        SIZE=$(stat -c%s "$OUT")
        BITRATE_KBPS=$(awk "BEGIN { printf \"%.1f\", ($SIZE * 8) / ($INPUT_DURATION * 1000) }")

        echo "    size       : $SIZE bytes  (${BITRATE_KBPS} kb/s)"
        echo "    encode     : ${ENCODE_S}s  (${ENCODE_SPEED_XRT}× real-time)"

        # ── XPSNR quality measurement pass (separate, untimed) ───────────────
        # xpsnr filter: [distorted][reference] order.
        # loglevel=info is required — filter output is logged at info level, not warning.
        # Actual format from this ffmpeg build:
        #   [Parsed_xpsnr_0 @ 0x...] XPSNR  y: 35.9963  u: 32.8926  v: 32.9320  (minimum: 32.8926)
        # There is no separate "average" field; Y is the luminance XPSNR (primary metric).
        XPSNR_OUTPUT=$("$FFMPEG" -hide_banner -loglevel info \
            -i "$OUT" \
            -i "$INPUT" \
            -lavfi "[0:v][1:v]xpsnr" \
            -f null - 2>&1 || true)

        # Extract Y, U, V from the summary line.
        # Format: "XPSNR  y: 35.9963  u: 32.8926  v: 32.9320  (minimum: ...)"
        # Use awk token-scan: field after "y:" is Y, after "u:" is U, after "v:" is V.
        # grep -v to filter the line first; awk handles variable spacing.
        _XPSNR_LINE=$(echo "$XPSNR_OUTPUT" | grep "XPSNR" | grep -v "Stream\|xpsnr:default\|0:0" | tail -1)
        XPSNR_Y=$(echo "$_XPSNR_LINE" | awk '/XPSNR/ { for(i=1;i<=NF;i++) if($i=="y:") print $(i+1) }')
        XPSNR_U=$(echo "$_XPSNR_LINE" | awk '/XPSNR/ { for(i=1;i<=NF;i++) if($i=="u:") print $(i+1) }')
        XPSNR_V=$(echo "$_XPSNR_LINE" | awk '/XPSNR/ { for(i=1;i<=NF;i++) if($i=="v:") print $(i+1) }')
        [[ -z "$XPSNR_Y" ]] && XPSNR_Y="N/A"
        [[ -z "$XPSNR_U" ]] && XPSNR_U="N/A"
        [[ -z "$XPSNR_V" ]] && XPSNR_V="N/A"
        # No separate average in this ffmpeg build: compute 4:2:0 luma-weighted avg
        # (Y=4, U=1, V=1 over 6 total → matches perceptual weight of luminance channel)
        if [[ "$XPSNR_Y" != "N/A" && "$XPSNR_U" != "N/A" && "$XPSNR_V" != "N/A" ]]; then
            XPSNR_AVG=$(awk "BEGIN { printf \"%.4f\", (4*$XPSNR_Y + $XPSNR_U + $XPSNR_V) / 6 }")
        else
            XPSNR_AVG="N/A"
        fi

        echo "    XPSNR Y/U/V/w-avg : ${XPSNR_Y} / ${XPSNR_U} / ${XPSNR_V} / ${XPSNR_AVG} dB"

        # Append to results TSV
        echo -e "${PRESET}\t${CQ}\t${SIZE}\t${BITRATE_KBPS}\t${ENCODE_S}\t${ENCODE_SPEED_XRT}\t${XPSNR_Y}\t${XPSNR_U}\t${XPSNR_V}\t${XPSNR_AVG}" \
            >> "$RESULTS_FILE"

        echo ""
    done
done

# ── monotonic-size validation ─────────────────────────────────────────────────
echo "=== Monotonic size validation (size must decrease as cq rises within preset) ==="
MONO_OK=1
for PRESET in $PRESET_LIST; do
    PREV_SIZE=""
    PREV_CQ=""
    while IFS=$'\t' read -r prs cq sz _rest; do
        [[ "$prs" == "preset" ]] && continue   # header
        [[ "$prs" != "$PRESET" ]] && continue
        if [[ -n "$PREV_SIZE" && "$sz" -ge "$PREV_SIZE" ]]; then
            echo "  WARNING: preset=$PRESET  cq=$PREV_CQ→$cq  size did NOT decrease ($PREV_SIZE → $sz bytes)"
            echo "           Rate-control may not be working correctly (-b:v 0 fix may be needed)"
            MONO_OK=0
        fi
        PREV_SIZE="$sz"
        PREV_CQ="$cq"
    done < "$RESULTS_FILE"
    [[ "$MONO_OK" == "1" ]] && echo "  preset=$PRESET : OK (monotonically decreasing)"
done
echo ""

# ── results table ─────────────────────────────────────────────────────────────
echo "=== Results Table (speed_xRT = video-s / wall-s, i.e. ×real-time factor) ==="
printf "%-8s %4s %12s %12s %10s %10s %8s %8s %8s %8s\n" \
    "preset" "cq" "size(bytes)" "bitrate(kbps)" "encode_s" "speed_xRT" "xpsnr_Y" "xpsnr_U" "xpsnr_V" "xpsnr_avg"
printf "%s\n" "$(printf '%.0s-' {1..90})"
while IFS=$'\t' read -r prs cq sz br enc spd xy xu xv xa; do
    [[ "$prs" == "preset" ]] && continue
    printf "%-8s %4s %12s %12s %10s %10s %8s %8s %8s %8s\n" \
        "$prs" "$cq" "$sz" "$br" "$enc" "$spd" "$xy" "$xu" "$xv" "$xa"
done < "$RESULTS_FILE"
echo ""

# ── save TSV alongside script output ─────────────────────────────────────────
FINAL_TSV="$(dirname "$INPUT")/nvenc-cq-sweep-$(date +%Y%m%d-%H%M%S).tsv"
cp "$RESULTS_FILE" "$FINAL_TSV"
echo "Results saved: $FINAL_TSV"
echo ""
echo "=== Sweep complete ==="
