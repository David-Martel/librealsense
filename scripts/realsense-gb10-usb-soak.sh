#!/usr/bin/env bash
set -euo pipefail

ROOT_OUT="${RS_GB10_SOAK_ROOT:-$HOME/realsense-gb10-validation}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${RS_GB10_SOAK_OUT:-$ROOT_OUT/$STAMP}"
PROFILE="${RS_GB10_SOAK_PROFILE:-all}"
CYCLES="${RS_GB10_SOAK_CYCLES:-3}"
DURATION="${RS_GB10_SOAK_DURATION_SEC:-10}"
TIMEOUT_MS="${RS_GB10_SOAK_TIMEOUT_MS:-1000}"
FAIL_ON_WARNING="${RS_GB10_FAIL_ON_WARNING:-1}"
USBMON_SECONDS="${RS_GB10_USBMON_SECONDS:-0}"

mkdir -p "$OUT"

have() {
  command -v "$1" >/dev/null 2>&1
}

run_env() {
  if have realsense-gb10-env; then
    realsense-gb10-env "$@"
  else
    "$@"
  fi
}

snapshot() {
  local label="$1"
  {
    date
    uname -a
    printf '\n=== xhci/realsense status ===\n'
    if have realsense-rsusb-metal; then
      realsense-rsusb-metal status || true
    fi
    printf '\n=== lsusb -t ===\n'
    lsusb -t || true
    printf '\n=== usb-devices ===\n'
    usb-devices || true
    printf '\n=== rs-enumerate-devices -s ===\n'
    run_env rs-enumerate-devices -s || true
  } >"$OUT/$label.txt" 2>&1
}

realsense_bus() {
  for dev in /sys/bus/usb/devices/*; do
    [[ -r "$dev/idVendor" && -r "$dev/idProduct" && -r "$dev/busnum" ]] || continue
    if [[ "$(cat "$dev/idVendor")" == "8086" ]] && grep -qi 'RealSense' "$dev/product" 2>/dev/null; then
      cat "$dev/busnum"
      return 0
    fi
  done
  return 1
}

start_usbmon() {
  local bus="$1"
  [[ "$USBMON_SECONDS" -gt 0 ]] || return 0
  if [[ ! -r "/sys/kernel/debug/usb/usbmon/${bus}u" ]]; then
    sudo modprobe usbmon >/dev/null 2>&1 || true
  fi
  [[ -r "/sys/kernel/debug/usb/usbmon/${bus}u" ]] || return 0
  timeout "$USBMON_SECONDS" cat "/sys/kernel/debug/usb/usbmon/${bus}u" >"$OUT/usbmon-bus${bus}.log" 2>&1 &
  printf '%s\n' "$!" >"$OUT/usbmon.pid"
}

stop_usbmon() {
  [[ -r "$OUT/usbmon.pid" ]] || return 0
  local pid
  pid="$(cat "$OUT/usbmon.pid")"
  kill "$pid" >/dev/null 2>&1 || true
  wait "$pid" >/dev/null 2>&1 || true
}

summarize() {
  local log="$OUT/rs-gb10-profiler.log"
  local kernel="$OUT/kernel-after.txt"
  count_matches() {
    local pattern="$1"
    local file="$2"
    if [[ -r "$file" ]]; then
      grep -ci "$pattern" "$file" 2>/dev/null || true
    else
      printf '0\n'
    fi
  }
  count_kernel_matches() {
    local pattern="$1"
    local file="$2"
    if [[ -r "$file" ]]; then
      grep -Eci "$pattern" "$file" 2>/dev/null || true
    else
      printf '0\n'
    fi
  }
  {
    printf 'output=%s\n' "$OUT"
    printf 'profile=%s cycles=%s duration_sec=%s timeout_ms=%s\n' "$PROFILE" "$CYCLES" "$DURATION" "$TIMEOUT_MS"
    printf 'profiler_warnings=%s\n' "$(count_matches 'WARNING' "$log")"
    printf 'profiler_errors=%s\n' "$(count_matches 'ERROR' "$log")"
    printf 'control_transfer=%s\n' "$(count_matches 'control_transfer' "$log")"
    printf 'watchdog=%s\n' "$(count_matches 'uvc streamer watchdog' "$log")"
    printf 'kernel_xhci_failures=%s\n' "$(count_kernel_matches 'xHCI host controller not responding|HC died|Host halt failed|can.t setup|probe with driver xhci-hcd failed' "$kernel")"
    printf 'kernel_clear_halt=%s\n' "$(count_matches 'USBDEVFS_CLEAR_HALT' "$kernel")"
    printf 'kernel_disconnects=%s\n' "$(count_matches 'USB disconnect' "$kernel")"
    printf '\n=== profiler results ===\n'
    grep -E '^(RESULT|STREAM|SUMMARY)' "$log" 2>/dev/null || true
  } >"$OUT/summary.txt"
}

snapshot baseline

if ! realsense_bus >"$OUT/realsense-bus.txt"; then
  journalctl -k --since '30 minutes ago' --no-pager >"$OUT/kernel-after.txt" 2>&1 || true
  summarize
  printf 'No Intel RealSense camera is enumerated; wrote diagnostics to %s\n' "$OUT" >&2
  exit 1
fi

BUS="$(cat "$OUT/realsense-bus.txt")"
start_usbmon "$BUS"
trap stop_usbmon EXIT

journalctl -k --since now --no-pager >"$OUT/kernel-before.txt" 2>&1 || true
set +e
run_env rs-gb10-profiler \
  --profile "$PROFILE" \
  --cycles "$CYCLES" \
  --duration-sec "$DURATION" \
  --timeout-ms "$TIMEOUT_MS" \
  --output-dir "$OUT/profiler" \
  --pre-stop-drain-ms "${RS_GB10_PRE_STOP_DRAIN_MS:-1200}" \
  --pre-stop-settle-ms "${RS_GB10_PRE_STOP_SETTLE_MS:-250}" \
  --cooldown-ms "${RS_GB10_COOLDOWN_MS:-1000}" \
  >"$OUT/rs-gb10-profiler.log" 2>&1
profiler_rc=$?
set -e

stop_usbmon
snapshot post
journalctl -k --since "$(date -d '15 minutes ago' '+%F %T')" --no-pager >"$OUT/kernel-after.txt" 2>&1 || true
summarize

cat "$OUT/summary.txt"

if [[ "$profiler_rc" -ne 0 ]]; then
  exit "$profiler_rc"
fi

if [[ "$FAIL_ON_WARNING" == "1" ]]; then
  if grep -E '^(profiler_warnings|profiler_errors|control_transfer|watchdog|kernel_xhci_failures|kernel_clear_halt|kernel_disconnects)=' "$OUT/summary.txt" \
    | grep -Eq '=(0)?[1-9][0-9]*'; then
    exit 1
  fi
fi
