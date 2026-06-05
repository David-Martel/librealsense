#!/usr/bin/env bash
# rs-gb10-stress.sh — drive rs-gb10-stress-matrix.py across a resolution/fps matrix
# (60 fps + range of resolutions, concurrent depth/color/IR, plus 90/300 Hz high-rate),
# wrapping it with a kernel USB-fault journal delta and a SHA-256 manifest.
#
# Idempotent (fresh timestamped report dir, no persistent side effects beyond streaming).
# Verdict = PASS only if every matrix entry meets its fps/drop criteria AND zero genuine
# host-stack USB faults occur during the whole sweep (benign librealsense/UVC noise excluded).
#
# Usage: rs-gb10-stress.sh [--duration S] [--headless] [--usbmon] [--out DIR] [--serial SN]
#   --usbmon   capture low-level USB traffic (usbmon URB trace + error-completion filter
#              + xhci-hcd ftrace; pcap too if tshark/dumpcap present) and auto-run protocol
#              forensics if any fault fires. Needs sudo + debugfs.
set -uo pipefail
DUR=12; RENDER_FLAG=""; OUT_BASE="${HOME}/realsense-gb10-validation"; SERIAL=""; USBMON=0
while [[ $# -gt 0 ]]; do case "$1" in
  --duration) DUR="${2:?}"; shift 2 ;;
  --headless) RENDER_FLAG="--headless"; shift ;;
  --usbmon) USBMON=1; shift ;;
  --out) OUT_BASE="${2:?}"; shift 2 ;;
  --serial) SERIAL="${2:?}"; shift 2 ;;
  -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac; done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RS_ENV="$(command -v realsense-gb10-env || true)"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT_BASE}/stress-matrix-${TS}"; mkdir -p "$OUT" || exit 2
echo "=== rs-gb10-stress @ $TS (duration=${DUR}s/entry, render=$([[ -z $RENDER_FLAG ]] && echo on || echo off), usbmon=$USBMON) ===" | tee "$OUT/run.log"

# topology + journal cursor before
{ echo "## lsusb -t"; lsusb -t; } > "$OUT/topology.txt" 2>&1
CAM_BUS=2; for d in /sys/bus/usb/devices/*; do [ "$(cat "$d/idVendor" 2>/dev/null)" = "8086" ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = "0b07" ] && CAM_BUS="$(cat "$d/busnum" 2>/dev/null)"; done
CURSOR="$(journalctl -k -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p')"

# --- start low-level USB capture (usbmon + xhci ftrace) for the whole sweep ---
if [[ "$USBMON" -eq 1 ]]; then
  # 11 entries * (DUR + ~6s start/stop/warmup overhead) + margin
  CAP_SECONDS=$(( 11 * (DUR + 6) + 30 ))
  bash "$HERE/rs-gb10-usbmon-capture.sh" start "$CAM_BUS" "$OUT/usbmon" "$CAP_SECONDS" | tee -a "$OUT/run.log"
fi

# run the matrix (stderr = live per-entry PASS/FAIL lines; stdout JSON -> file)
SARG=(); [[ -n "$SERIAL" ]] && SARG=(--serial "$SERIAL")
if [[ -n "$RS_ENV" ]]; then
  "$RS_ENV" python3 "$HERE/rs-gb10-stress-matrix.py" --duration "$DUR" $RENDER_FLAG \
     "${SARG[@]}" --json "$OUT/matrix.json" > "$OUT/matrix-stdout.json" 2> >(tee "$OUT/matrix-progress.log" >&2)
else
  python3 "$HERE/rs-gb10-stress-matrix.py" --duration "$DUR" $RENDER_FLAG \
     "${SARG[@]}" --json "$OUT/matrix.json" > "$OUT/matrix-stdout.json" 2> >(tee "$OUT/matrix-progress.log" >&2)
fi
MATRIX_RC=$?

# --- stop low-level USB capture + summarize protocol-level errors ---
USBMON_ERRORS=0
if [[ "$USBMON" -eq 1 ]]; then
  bash "$HERE/rs-gb10-usbmon-capture.sh" stop "$OUT/usbmon" >> "$OUT/run.log" 2>&1 || true
  USBMON_ERRORS=$(sed -n 's/^error_urbs=//p' "$OUT/usbmon/usbmon-error-summary.txt" 2>/dev/null | head -1)
  USBMON_ERRORS=${USBMON_ERRORS:-0}
fi

# journal delta + fault tally (exclude benign librealsense USBDEVFS_CLEAR_HALT + UVC 981ae2 noise)
journalctl -k --after-cursor "$CURSOR" --no-pager > "$OUT/journal-delta.txt" 2>/dev/null || true
BENIGN='USBDEVFS_CLEAR_HALT for active endpoint|981ae2'
FAULTS=0
{ echo "# kernel fault tally (benign librealsense/UVC noise excluded)"
  for pat in 'USB disconnect' 'clear_halt' 'xhci.*[Ff]ail' 'reset .*device' 'cannot enable' \
             'over-?current' 'Not enough bandwidth' 'device descriptor read' 'babble'; do
    n=$(grep -iE "$pat" "$OUT/journal-delta.txt" 2>/dev/null | grep -vciE "$BENIGN"); n=${n:-0}
    printf '%-28s %s\n' "$pat" "$n"
  done
} > "$OUT/fault-tally.txt"
FAULTS=$(awk 'NR>1{s+=$NF} END{print s+0}' "$OUT/fault-tally.txt")

# parse matrix summary (python for robust JSON)
read -r ENTRIES PASSED FAILED <<<"$(python3 - "$OUT/matrix.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1])); s=d["summary"]; print(s["entries"], s["passed"], s["failed"])
except Exception: print(0,0,1)
PY
)"

# run USB-protocol forensics whenever ANY fault signal fired (journal or usbmon)
if [[ "$USBMON" -eq 1 && ( "$FAULTS" -gt 0 || "${USBMON_ERRORS:-0}" -gt 0 ) ]]; then
  bash "$HERE/rs-gb10-usbmon-capture.sh" forensics "$OUT/usbmon" "$OUT/journal-delta.txt" >> "$OUT/run.log" 2>&1 || true
fi

VERDICT="PASS"
{ [[ "$FAILED" -eq 0 && "$ENTRIES" -gt 0 && "$FAULTS" -eq 0 && "$MATRIX_RC" -eq 0 && "${USBMON_ERRORS:-0}" -eq 0 ]]; } || VERDICT="FAIL"

# report.md
{
  echo "# RealSense GB10 Interface Stress Matrix — ${TS}"
  echo
  echo "**Verdict: ${VERDICT}**  · entries=${ENTRIES} passed=${PASSED} failed=${FAILED} · kernel USB faults=${FAULTS} · usbmon error URBs=${USBMON_ERRORS:-n/a} · ${DUR}s/entry"
  echo
  python3 - "$OUT/matrix.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
dev=d.get("device",{})
print(f"Device: {dev.get('name','?')} serial={dev.get('serial_number','?')} fw={dev.get('firmware_version','?')} usb={dev.get('usb_type_descriptor','?')} port={dev.get('physical_port','?')}\n")
print("| entry | req fps | dur | per-stream fps (gaps) | result |")
print("|---|---|---|---|---|")
for r in d["results"]:
    streams=", ".join(f"{s['stream']} {s['fps']}/{s['requested_fps']} (g{s['gaps']})" for s in r["streams"]) or (r.get("error") or "no frames")
    print(f"| {r['entry']} | {r['requested_fps']} | {r['duration_s']}s | {streams} | {'PASS' if r['ok'] else 'FAIL'} |")
PY
  echo
  echo "Fault tally:"; echo '```'; cat "$OUT/fault-tally.txt"; echo '```'
  if [[ "$USBMON" -eq 1 ]]; then
    echo; echo "## Low-level USB capture (usbmon + xhci ftrace)"
    echo '```'; cat "$OUT/usbmon/usbmon-error-summary.txt" 2>/dev/null; echo '```'
    [[ -s "$OUT/usbmon/usb-forensics.txt" ]] && echo "Forensics (fault correlation): \`usbmon/usb-forensics.txt\`"
    echo "Raw: \`usbmon/usbmon-bus${CAM_BUS}-full.txt\` (capped), \`usbmon/usbmon-bus${CAM_BUS}-errors.txt\`, \`usbmon/xhci-ftrace.txt\`"
  fi
  echo "Artifacts: matrix.json, matrix-progress.log, topology.txt, journal-delta.txt, fault-tally.txt, SHA256SUMS"
} > "$OUT/report.md"

( cd "$OUT" && sha256sum -- *.txt *.json *.log report.md usbmon/*.txt 2>/dev/null | grep -v 'run.log' > SHA256SUMS )

echo "=== VERDICT: $VERDICT (entries=$ENTRIES passed=$PASSED failed=$FAILED faults=$FAULTS) ===" | tee -a "$OUT/run.log"
echo "report: $OUT/report.md"
echo "$OUT" > "${OUT_BASE}/latest-stress-matrix"
[[ "$VERDICT" == "PASS" ]] && exit 0 || exit 1
