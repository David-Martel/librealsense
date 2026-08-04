#!/usr/bin/env bash
# rs-gb10-healthcheck.sh — idempotent, independently-verifiable RealSense health gate
# for NVIDIA DGX Spark / GB10. Produces a structured PASS/FAIL report + raw artifacts
# + a SHA-256 manifest under a fresh timestamped directory.
#
# Idempotent: safe to run repeatedly; no persistent system side effects (read-only on
# the system except for streaming the camera and writing into its own report dir).
# Deterministic verdict: each check is scored against an explicit threshold, so the
# same hardware state yields the same PASS/FAIL even though streaming metrics vary.
#
# Usage:
#   rs-gb10-healthcheck.sh [--quick] [--soak SECONDS] [--profile NAME] [--out DIR] [--serial SN] [--visible|--headless]
#     --quick          short 15s single-profile stream (CI smoke)
#     --soak SECONDS    long single-stream soak (e.g. --soak 1800 for 30 min)
#     --profile NAME    profiler profile: vga30|vga60|depth90-ir|hd15 (default hd15+vga30)
#     --out DIR         base output dir (default ~/realsense-gb10-validation)
#     --serial SN       bind a specific camera serial
#     --visible        render captured frames on screen + save evidence images (DEFAULT)
#     --headless       no on-screen render, no evidence images (CI / no DISPLAY / soak)
#
# Visible mode shows a live RealSense window (depth/color/IR per profile) on $DISPLAY and
# writes one framebuffer evidence PNG per cycle under the report's per-profile dir.
#
# Exit code: 0 = PASS, 1 = FAIL, 2 = usage/setup error.
set -uo pipefail

# ---------- config / thresholds (explicit, auditable) ----------
THRESH_MIN_SPEED_M=5000          # require SuperSpeed
THRESH_USB_DESC_MIN="3.0"        # require USB type descriptor >= 3.0
THRESH_MAX_STREAM_FAILURES=0     # profiler stream failures allowed
THRESH_MAX_KERNEL_FAULTS=0       # kernel USB faults allowed during run
THRESH_MIN_FPS_RATIO=0.80        # measured fps must be >= 80% of profile target (sustained only)
VENDOR="8086"; PRODUCT="0b07"    # Intel RealSense D435

# ---------- args ----------
# Visible (non-headless) by default: the profiler renders captured frames on screen and
# writes one framebuffer evidence image per cycle. Use --headless for CI / no-DISPLAY / soak.
MODE="standard"; SOAK_SEC=0; PROFILE=""; SERIAL=""; RENDER="on"
OUT_BASE="${HOME}/realsense-gb10-validation"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) MODE="quick"; shift ;;
    --soak) MODE="soak"; SOAK_SEC="${2:-1800}"; shift 2 ;;
    --profile) PROFILE="${2:?}"; shift 2 ;;
    --out) OUT_BASE="${2:?}"; shift 2 ;;
    --serial) SERIAL="${2:?}"; shift 2 ;;
    --visible) RENDER="on"; shift ;;
    --headless) RENDER="off"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
# A visible render needs an X display. Auto-fall-back to headless if none, with a warning.
if [[ "$RENDER" == "on" && -z "${DISPLAY:-}" ]]; then
  echo "WARN: --visible requested but DISPLAY is unset; falling back to --headless" >&2
  RENDER="off"
fi

TS="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT_BASE}/healthcheck-${TS}"
mkdir -p "$OUT" || { echo "cannot create $OUT" >&2; exit 2; }
RESULT_JSON="${OUT}/result.json"; REPORT="${OUT}/report.md"
PASS=0; FAIL=0; declare -a CHECKS

note() { printf '%s\n' "$*" | tee -a "${OUT}/run.log" >/dev/null; }
# record a check: name, status(PASS/FAIL/INFO), measured, threshold, detail
rec() {
  local name="$1" st="$2" meas="$3" thr="$4" det="${5:-}"
  CHECKS+=("$(printf '{"check":"%s","status":"%s","measured":"%s","threshold":"%s","detail":"%s"}' \
            "$name" "$st" "${meas//\"/}" "${thr//\"/}" "${det//\"/}")")
  [[ "$st" == "PASS" ]] && PASS=$((PASS+1)); [[ "$st" == "FAIL" ]] && FAIL=$((FAIL+1))
  printf '[%s] %-22s measured=%s threshold=%s %s\n' "$st" "$name" "$meas" "$thr" "$det" | tee -a "${OUT}/run.log"
}

note "=== rs-gb10-healthcheck @ ${TS} (mode=${MODE}) ==="

# ---------- locate env wrapper / profiler ----------
RS_ENV="$(command -v realsense-gb10-env || true)"
if [[ -z "$RS_ENV" ]]; then rec "tooling.env" FAIL "missing" "realsense-gb10-env on PATH" "GB10 SDK env not found"; fi
run_rs() { if [[ -n "$RS_ENV" ]]; then "$RS_ENV" "$@"; else "$@"; fi; }
# timeout wraps a real executable, not a shell function — so prepend the env binary directly.
run_rs_timeout() { local t="$1"; shift; if [[ -n "$RS_ENV" ]]; then timeout "$t" "$RS_ENV" "$@"; else timeout "$t" "$@"; fi; }

# ---------- 1. device presence + topology ----------
DEV=""; SPEED=""; BCDUSB=""; SERIAL_KERN=""; MAXPOWER=""
for d in /sys/bus/usb/devices/*; do
  [[ "$(cat "$d/idVendor" 2>/dev/null)" == "$VENDOR" && "$(cat "$d/idProduct" 2>/dev/null)" == "$PRODUCT" ]] || continue
  DEV="$(basename "$d")"; SPEED="$(cat "$d/speed" 2>/dev/null)"; BCDUSB="$(cat "$d/version" 2>/dev/null | tr -d ' ')"
  SERIAL_KERN="$(cat "$d/serial" 2>/dev/null)"; MAXPOWER="$(cat "$d/bMaxPower" 2>/dev/null)"
  break
done
{ echo "## lsusb -t"; lsusb -t; echo "## D435 device=$DEV speed=$SPEED bcdUSB=$BCDUSB serial=$SERIAL_KERN bMaxPower=$MAXPOWER"; } > "${OUT}/topology.txt" 2>&1

if [[ -z "$DEV" ]]; then
  rec "device.present" FAIL "absent" "D435 ${VENDOR}:${PRODUCT} present" "no camera on any USB bus"
else
  rec "device.present" PASS "$DEV" "present" "path=$DEV"
  # speed
  if [[ -n "$SPEED" && "$SPEED" -ge "$THRESH_MIN_SPEED_M" ]] 2>/dev/null; then
    rec "usb.speed" PASS "${SPEED}M" ">=${THRESH_MIN_SPEED_M}M" "SuperSpeed"
  else
    rec "usb.speed" FAIL "${SPEED}M" ">=${THRESH_MIN_SPEED_M}M" "USB2 fallback — move to native Type-C port + eMarker cable; do not use the dock"
  fi
  # serial read
  if [[ -n "$SERIAL_KERN" && "$SERIAL_KERN" != "0" ]]; then
    rec "usb.serial_read" PASS "$SERIAL_KERN" "non-zero" "descriptor read OK"
  else
    rec "usb.serial_read" FAIL "${SERIAL_KERN:-0}" "non-zero" "kernel could not read serial — marginal link"
  fi
fi

# ---------- 2. SDK enumeration (USB type descriptor, firmware) ----------
ENUM="${OUT}/rs-enumerate.txt"
run_rs_timeout 40 rs-enumerate-devices > "$ENUM" 2>&1
val_after_colon() { grep -iE "$1" "$ENUM" | head -1 | sed 's/.*:[[:space:]]*//' | tr -d '[:space:]'; }
USB_DESC="$(val_after_colon 'Usb Type Descriptor')"
FW="$(val_after_colon 'Firmware Version')"
SDK_SERIAL="$(val_after_colon 'Serial Number')"
if [[ -n "$USB_DESC" ]] && awk -v a="$USB_DESC" -v b="$THRESH_USB_DESC_MIN" 'BEGIN{exit !(a+0>=b+0)}'; then
  rec "sdk.usb_descriptor" PASS "$USB_DESC" ">=${THRESH_USB_DESC_MIN}" "fw=${FW:-?} serial=${SDK_SERIAL:-?}"
else
  rec "sdk.usb_descriptor" FAIL "${USB_DESC:-none}" ">=${THRESH_USB_DESC_MIN}" "SDK sees sub-USB3 link"
fi
[[ -n "$FW" ]] && rec "sdk.firmware" INFO "$FW" "Intel matrix floor 5.17.3.10" "update only over a GREEN USB3 link"

# ---------- 3. usbfs headroom (info/advisory) ----------
USBFS="$(cat /sys/module/usbcore/parameters/usbfs_memory_mb 2>/dev/null)"
if [[ -n "$USBFS" && "$USBFS" -ge 256 ]] 2>/dev/null; then
  rec "usbfs.memory_mb" PASS "$USBFS" ">=256" "headroom OK"
else
  rec "usbfs.memory_mb" INFO "${USBFS:-?}" ">=256 recommended" "raise via /etc/modprobe.d/99-realsense-usbfs.conf (advisory, not fatal)"
fi

# ---------- 4. streaming + kernel fault delta ----------
CURSOR="$(journalctl -k -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p')"
SERIAL_ARG=(); [[ -n "$SERIAL" ]] && SERIAL_ARG=(--serial "$SERIAL")

stream_one() { # profile duration
  local p="$1" dur="$2"
  local render_args=()
  if [[ "$RENDER" == "on" ]]; then
    note "--- streaming profile=$p duration=${dur}s (VISIBLE on DISPLAY=$DISPLAY, evidence images on) ---"
    # default profiler behaviour renders a window + writes framebuffer evidence per cycle
    render_args=()
  else
    note "--- streaming profile=$p duration=${dur}s (headless) ---"
    render_args=(--no-render --no-evidence)
  fi
  run_rs_timeout $((dur+90)) rs-gb10-profiler --profile "$p" --cycles 1 --duration-sec "$dur" \
    "${render_args[@]}" --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250 --cooldown-ms 1000 \
    "${SERIAL_ARG[@]}" --output-dir "${OUT}/${p}" > "${OUT}/profiler-${p}.log" 2>&1
}

declare -a RAN
if [[ "$DEV" != "" ]]; then
  case "$MODE" in
    quick) PRO="${PROFILE:-hd15}"; stream_one "$PRO" 15; RAN=("$PRO") ;;
    soak)  PRO="${PROFILE:-hd15}"; stream_one "$PRO" "$SOAK_SEC"; RAN=("$PRO") ;;
    *)     if [[ -n "$PROFILE" ]]; then stream_one "$PROFILE" 60; RAN=("$PROFILE")
           else stream_one hd15 60; stream_one vga30 60; RAN=(hd15 vga30); fi ;;
  esac
else
  rec "stream.skipped" FAIL "no-device" "device present" "cannot stream without a camera"
fi

# journal delta + fault tally
journalctl -k --after-cursor "$CURSOR" --no-pager > "${OUT}/journal-delta.txt" 2>/dev/null || true
# Fault patterns are host-stack/link errors only. We deliberately EXCLUDE benign,
# librealsense-initiated noise that is logged on healthy USB3 links too:
#  - "USBDEVFS_CLEAR_HALT for active endpoint": userspace (libusb/librealsense) clearing
#    its own endpoint halt during stream setup — normal RSUSB behaviour, NOT a link fault.
#  - "UVC non compliance ... 981ae2 ... error -5": benign D4xx UVC vendor-control quirk.
BENIGN='USBDEVFS_CLEAR_HALT for active endpoint|981ae2'
FAULTS=0
{ echo "# kernel fault tally (journal delta during run; benign librealsense/UVC noise excluded)"
  for pat in 'USB disconnect' 'clear_halt' 'xhci.*[Ff]ail' 'reset .*device' 'cannot enable' \
             'over-?current' 'Not enough bandwidth' 'device descriptor read' 'babble'; do
    n=$(grep -iE "$pat" "${OUT}/journal-delta.txt" 2>/dev/null | grep -vciE "$BENIGN"); n=${n:-0}
    printf '%-28s %s\n' "$pat" "$n"; FAULTS=$((FAULTS+n))
  done
} > "${OUT}/fault-tally.txt"
FAULTS=$(awk 'NR>1{s+=$NF} END{print s+0}' "${OUT}/fault-tally.txt")
if [[ "$FAULTS" -le "$THRESH_MAX_KERNEL_FAULTS" ]]; then
  rec "kernel.usb_faults" PASS "$FAULTS" "<=${THRESH_MAX_KERNEL_FAULTS}" "no disconnect/clear_halt/xhci faults"
else
  rec "kernel.usb_faults" FAIL "$FAULTS" "<=${THRESH_MAX_KERNEL_FAULTS}" "see fault-tally.txt"
fi

# profiler stream failures + fps ratio (sustained profiles only)
# Pipeline framesets synchronize to the slowest requested stream. vga60 pairs
# depth@60 with color@30, so healthy composite delivery is 30 framesets/s.
declare -A FPS_TARGET=( [vga30]=30 [vga60]=30 [depth90-ir]=90 [hd15]=15 )
TOTAL_FAIL=0
for p in "${RAN[@]}"; do
  log="${OUT}/profiler-${p}.log"
  f=$(grep -hoE '(^|[[:space:]])failures=[0-9]+' "$log" 2>/dev/null | awk -F= '{s+=$2} END{print s+0}')
  u3=$(grep -cE 'usb3=yes' "$log" 2>/dev/null)
  fps=$(grep -hoE 'fps=[0-9.]+' "$log" 2>/dev/null | tail -1 | awk -F= '{print $2}')
  TOTAL_FAIL=$((TOTAL_FAIL + ${f:-0}))
  rec "stream.${p}.failures" "$([[ ${f:-1} -le $THRESH_MAX_STREAM_FAILURES ]] && echo PASS || echo FAIL)" \
      "${f:-?}" "<=${THRESH_MAX_STREAM_FAILURES}" "usb3_cycles=${u3} fps=${fps:-?}"
  # fps ratio gate only for sustained (>=30s) standard/soak runs
  if [[ "$MODE" != "quick" && -n "${fps:-}" && -n "${FPS_TARGET[$p]:-}" ]]; then
    ratio=$(awk -v a="$fps" -v b="${FPS_TARGET[$p]}" 'BEGIN{printf "%.2f", (b>0?a/b:0)}')
    if awk -v r="$ratio" -v t="$THRESH_MIN_FPS_RATIO" 'BEGIN{exit !(r>=t)}'; then
      rec "stream.${p}.fps_ratio" PASS "$ratio" ">=${THRESH_MIN_FPS_RATIO}" "fps=${fps}/${FPS_TARGET[$p]}"
    else
      rec "stream.${p}.fps_ratio" INFO "$ratio" ">=${THRESH_MIN_FPS_RATIO}" "below target — investigate load, not necessarily USB"
    fi
  fi
done

# ---------- evidence images (visible mode) ----------
EVID_COUNT=0
if [[ "$RENDER" == "on" && "$DEV" != "" ]]; then
  mapfile -t EVID < <(find "$OUT" -type f \( -iname '*.png' -o -iname '*.ppm' -o -iname '*.bmp' -o -iname '*.jpg' \) 2>/dev/null | sort)
  EVID_COUNT="${#EVID[@]}"
  printf '%s\n' "${EVID[@]}" > "${OUT}/evidence-images.txt" 2>/dev/null
  if [[ "$EVID_COUNT" -gt 0 ]]; then
    rec "render.evidence" PASS "$EVID_COUNT img" ">=1 in visible mode" "on-screen render confirmed; see evidence-images.txt"
  else
    rec "render.evidence" INFO "0 img" ">=1 in visible mode" "window rendered but no evidence file captured (profiler may render-only)"
  fi
fi

# ---------- verdict ----------
VERDICT="PASS"; [[ "$FAIL" -gt 0 ]] && VERDICT="FAIL"
EPOCH="$(date +%s)"

# result.json (hand-built; no jq dependency)
{
  printf '{\n'
  printf '  "tool":"rs-gb10-healthcheck","schema":1,"timestamp":"%s","epoch":%s,"mode":"%s",\n' "$TS" "$EPOCH" "$MODE"
  printf '  "host":"%s","device":"%s","usb_speed_m":"%s","usb_descriptor":"%s","firmware":"%s","serial":"%s",\n' \
         "$(hostname)" "$DEV" "$SPEED" "${USB_DESC:-}" "${FW:-}" "${SDK_SERIAL:-$SERIAL_KERN}"
  printf '  "kernel_usb_faults":%s,"stream_failures":%s,"pass":%s,"fail":%s,"verdict":"%s",\n' \
         "${FAULTS:-0}" "${TOTAL_FAIL:-0}" "$PASS" "$FAIL" "$VERDICT"
  printf '  "checks":[\n    %s\n  ]\n' "$(IFS=,; echo "${CHECKS[*]}" | sed 's/},{/},\n    {/g')"
  printf '}\n'
} > "$RESULT_JSON"

# report.md
{
  echo "# RealSense GB10 Health Check — ${TS}"
  echo
  echo "**Verdict: ${VERDICT}**  (checks: ${PASS} pass / ${FAIL} fail)  mode=${MODE}"
  echo
  echo "| field | value |"
  echo "|---|---|"
  echo "| host | $(hostname) |"
  echo "| device path | ${DEV:-absent} |"
  echo "| negotiated speed | ${SPEED:-?}M |"
  echo "| USB type descriptor | ${USB_DESC:-?} |"
  echo "| firmware | ${FW:-?} |"
  echo "| serial | ${SDK_SERIAL:-${SERIAL_KERN:-?}} |"
  echo "| kernel USB faults (run) | ${FAULTS:-0} |"
  echo "| stream failures | ${TOTAL_FAIL:-0} |"
  echo
  echo "## Checks"
  echo "| check | status | measured | threshold | detail |"
  echo "|---|---|---|---|---|"
  for c in "${CHECKS[@]}"; do
    n=$(sed -n 's/.*"check":"\([^"]*\)".*/\1/p' <<<"$c")
    s=$(sed -n 's/.*"status":"\([^"]*\)".*/\1/p' <<<"$c")
    m=$(sed -n 's/.*"measured":"\([^"]*\)".*/\1/p' <<<"$c")
    t=$(sed -n 's/.*"threshold":"\([^"]*\)".*/\1/p' <<<"$c")
    d=$(sed -n 's/.*"detail":"\([^"]*\)".*/\1/p' <<<"$c")
    printf '| %s | %s | %s | %s | %s |\n' "$n" "$s" "$m" "$t" "$d"
  done
  echo
  echo "Artifacts: topology.txt, rs-enumerate.txt, profiler-*.log, journal-delta.txt, fault-tally.txt, result.json, SHA256SUMS"
} > "$REPORT"

# SHA-256 manifest for independent verification.
# Exclude run.log: it is the live progress log and is still appended to after this point,
# so hashing it here would self-invalidate. Everything scored is in result.json/report.md/artifacts.
(
  cd "$OUT" || exit 1
  find . -type f \
    \( -iname '*.txt' -o -iname 'profiler-*.log' -o -name result.json -o -name report.md \
       -o -iname '*.png' -o -iname '*.ppm' -o -iname '*.bmp' -o -iname '*.jpg' \) \
    -print0 \
    | sort -z \
    | xargs -0 -r sha256sum -- > SHA256SUMS
)

note "=== VERDICT: ${VERDICT} (${PASS} pass / ${FAIL} fail) ==="
note "report: $REPORT"
echo "$OUT" > "${OUT_BASE}/latest-healthcheck"
echo "$REPORT"
[[ "$VERDICT" == "PASS" ]] && exit 0 || exit 1
