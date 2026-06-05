#!/usr/bin/env bash
# rs-gb10-usbmon-capture.sh — low-level USB protocol capture for RealSense debugging.
#
# Uses the in-kernel usbmon facility (the native USB packet/URB sniffer) plus xhci-hcd
# ftrace to catch USB errors at the protocol layer when they occur. No external sniffer
# needed; if tshark/dumpcap are present, a pcap is captured too.
#
# Subcommands:
#   start  <bus> <outdir> <max_seconds>   begin time-bounded capture (self-terminating)
#   stop   <outdir>                        snapshot xhci ftrace, finalize
#   forensics <outdir> <journal-delta>     correlate journal faults <-> usbmon error URBs
#   errors <outdir>                        print/refresh the error-completion tally
#
# Error completions are URB callbacks ('C') whose status is a negative errno:
#   -32 EPIPE(stall)  -71 EPROTO  -75 EOVERFLOW  -84 EILSEQ  -108 ESHUTDOWN
#   -110 ETIMEDOUT    -121 EREMOTEIO  -2 ENOENT  -19 ENODEV   (+ "babble" in dmesg)
set -uo pipefail
SUDO="sudo -n"
MON=/sys/kernel/debug/usb/usbmon
TRACE=/sys/kernel/debug/tracing
FULL_CAP_BYTES=25000000   # cap full URB trace at ~25 MB

ensure_usbmon() {
  $SUDO modprobe usbmon 2>/dev/null || true
  mount | grep -q debugfs || $SUDO mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
  # /sys/kernel/debug is root-only (0700) — test existence THROUGH sudo, not as the user.
  $SUDO test -e "$MON/0u" || { echo "usbmon unavailable (need root/debugfs)"; return 1; }
}

cmd_start() {
  local bus="$1" out="$2" dur="$3"
  mkdir -p "$out"
  ensure_usbmon || { echo "usbmon=unavailable" > "$out/usbmon-status.txt"; return 1; }
  echo "bus=$bus max_seconds=$dur node=$MON/${bus}u" > "$out/usbmon-status.txt"

  # 1) size-capped FULL URB trace for this bus (forensic context)
  ( $SUDO timeout "$dur" cat "$MON/${bus}u" 2>/dev/null | head -c "$FULL_CAP_BYTES" \
      > "$out/usbmon-bus${bus}-full.txt" ) &
  echo $! > "$out/.usbmon-full.pid"

  # 2) live ERROR-completion filter (small, high-signal): 'C' callback with negative status
  ( $SUDO timeout "$dur" cat "$MON/${bus}u" 2>/dev/null \
      | awk '$3=="C" && $5 ~ /^-[0-9]/ {print; fflush()}' \
      > "$out/usbmon-bus${bus}-errors.txt" ) &
  echo $! > "$out/.usbmon-err.pid"

  # 3) xhci-hcd ftrace (controller-level errors: command/transfer/halt events)
  if [[ -d "$TRACE/events/xhci-hcd" ]]; then
    $SUDO sh -c "echo 0 > $TRACE/tracing_on; echo > $TRACE/trace; echo 1 > $TRACE/events/xhci-hcd/enable; echo 1 > $TRACE/tracing_on" 2>/dev/null \
      && echo "xhci-ftrace=on" >> "$out/usbmon-status.txt" || echo "xhci-ftrace=unavailable" >> "$out/usbmon-status.txt"
  fi

  # 4) optional pcap if a USB-capable sniffer is installed
  if command -v dumpcap >/dev/null 2>&1; then
    ( $SUDO timeout "$dur" dumpcap -i "usbmon${bus}" -w "$out/usbmon-bus${bus}.pcap" >/dev/null 2>&1 ) &
    echo $! > "$out/.usbmon-pcap.pid"; echo "pcap=on(dumpcap)" >> "$out/usbmon-status.txt"
  elif command -v tshark >/dev/null 2>&1; then
    ( $SUDO timeout "$dur" tshark -i "usbmon${bus}" -w "$out/usbmon-bus${bus}.pcap" >/dev/null 2>&1 ) &
    echo $! > "$out/.usbmon-pcap.pid"; echo "pcap=on(tshark)" >> "$out/usbmon-status.txt"
  else
    echo "pcap=unavailable(no tshark/dumpcap)" >> "$out/usbmon-status.txt"
  fi
  echo "started usbmon capture (bus $bus, <=${dur}s) -> $out"
}

cmd_stop() {
  local out="$1"
  # snapshot + disable xhci ftrace
  if [[ -d "$TRACE/events/xhci-hcd" ]]; then
    $SUDO sh -c "cat $TRACE/trace > '$out/xhci-ftrace.txt' 2>/dev/null; echo 0 > $TRACE/events/xhci-hcd/enable; echo 0 > $TRACE/tracing_on" 2>/dev/null || true
  fi
  # capture processes are timeout-bounded; nudge any still alive
  for p in "$out"/.usbmon-*.pid; do [[ -e "$p" ]] || continue; kill "$(cat "$p")" 2>/dev/null || true; done
  sync
  cmd_errors "$out"
}

cmd_errors() {
  local out="$1"
  local ef; ef=$(ls "$out"/usbmon-bus*-errors.txt 2>/dev/null | head -1)
  local n=0; [[ -n "${ef:-}" ]] && n=$(grep -c . "$ef" 2>/dev/null)
  {
    echo "# usbmon error-completion tally"
    echo "error_urbs=$n"
    if [[ -n "${ef:-}" && "$n" -gt 0 ]]; then
      echo "## by errno:"
      awk '{print $5}' "$ef" | sort | uniq -c | sort -rn
      echo "## sample (first 20):"; head -20 "$ef"
    fi
    if [[ -s "$out/xhci-ftrace.txt" ]]; then
      echo "## xhci-hcd ftrace error/halt lines:"
      grep -iE 'halt|stall|error|fail|cmd_comp.*[1-9]|stop_ep|reset' "$out/xhci-ftrace.txt" 2>/dev/null | head -30
    fi
  } > "$out/usbmon-error-summary.txt"
  cat "$out/usbmon-error-summary.txt"
}

cmd_forensics() {
  local out="$1" jdelta="$2"
  local ef; ef=$(ls "$out"/usbmon-bus*-full.txt 2>/dev/null | head -1)
  {
    echo "# USB fault forensics — correlate kernel journal faults with usbmon URBs"
    echo
    echo "## genuine host-stack faults from journal delta:"
    grep -iE 'USB disconnect|xhci.*fail|reset .*device|cannot enable|Not enough bandwidth|over-?current|device descriptor read|babble|probe control : -[0-9]' "$jdelta" 2>/dev/null \
      | grep -viE 'USBDEVFS_CLEAR_HALT for active endpoint|981ae2' | head -40
    echo
    echo "## usbmon error-completion URBs (protocol-level):"
    cat "$out"/usbmon-bus*-errors.txt 2>/dev/null | head -60
    echo
    echo "## xhci-hcd controller trace (filtered):"
    grep -iE 'halt|stall|error|fail|stop_ep|reset|cmd_comp' "$out/xhci-ftrace.txt" 2>/dev/null | head -40
  } > "$out/usb-forensics.txt"
  echo "wrote $out/usb-forensics.txt"
}

case "${1:-}" in
  start)     shift; cmd_start "$@" ;;
  stop)      shift; cmd_stop "$@" ;;
  errors)    shift; cmd_errors "$@" ;;
  forensics) shift; cmd_forensics "$@" ;;
  *) sed -n '2,22p' "$0"; exit 2 ;;
esac
