#!/usr/bin/env bash
# Smoke tests for the GB10 rendering / display tools — catches bugs without needing the camera.
# Offline by default: byte-compile sweep, the display tool's offline --self-test (argparse, profile
# clamping, validation math, HUD composition), and the CLI contract (--help exit 0, bad args exit 2).
# Pass --live to also run ONE short, tripwire-guarded on-screen validation (only if the controller is green).
#
# Usage:  scripts/gb10/smoke-test-display.sh [--live]      (or `just smoke-display`)
set -uo pipefail
SG="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SG/../.." && pwd)"
VENV="${LRS_VENV:-$HOME/realsense-gb10-validation/.venv/bin/python}"
OPENCV=/opt/gb10-cuda/install/opencv
FFLIB=/opt/gb10-cuda/install/ffmpeg/lib
FULL="$HOME/realsense-gb10-validation/build-gb10-full/Release"
TOOL="$SG/rs-gb10-nonheadless-verify.py"

# offline env (cv2 needs opencv/lib AND ffmpeg/lib; hil_common needs scripts/gb10 on PYTHONPATH)
export LD_LIBRARY_PATH="$OPENCV/lib:$FFLIB"
export PYTHONPATH="$SG:$OPENCV/lib/python3.12/site-packages"

fails=0
pass() { printf '  [PASS] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; fails=$((fails + 1)); }

echo "== 1. byte-compile all gb10 python tools =="
cfail=0
for f in "$SG"/rs-gb10-*.py "$SG"/_*.py "$SG"/hil_common.py; do
  [ -e "$f" ] || continue
  "$VENV" -m py_compile "$f" 2>/dev/null || { fail "compile $(basename "$f")"; cfail=1; }
done
[ "$cfail" -eq 0 ] && pass "all gb10 python tools compile"

echo "== 2. display tool offline self-test (pure logic) =="
"$VENV" "$TOOL" --self-test >/tmp/smoke-selftest.log 2>&1 && pass "self-test (13 checks)" \
  || { fail "self-test"; tail -3 /tmp/smoke-selftest.log; }

echo "== 3. CLI contract =="
"$VENV" "$TOOL" --help >/dev/null 2>&1 && pass "--help exits 0" || fail "--help"
"$VENV" "$TOOL" --stream bogus >/dev/null 2>&1; [ $? -eq 2 ] && pass "invalid choice exits 2" || fail "bad-arg exit code"
"$VENV" "$TOOL" --interactive --depth --duration 1 --self-test >/dev/null 2>&1 && pass "combined flags parse" || fail "combined flags"

echo "== 4. live smoke (on-screen validation) =="
if [ "${1:-}" = "--live" ]; then
  if journalctl -k --no-pager -n 25 2>/dev/null | grep -qiE "HC died|not responding to stop"; then
    fail "controller shows a prior death — reboot before live smoke"
  else
    LD_LIBRARY_PATH="$OPENCV/lib:$FFLIB:$FULL" \
    PYTHONPATH="$SG:$FULL:$OPENCV/lib/python3.12/site-packages" \
    DISPLAY="${DISPLAY:-:1}" LRS_FFMPEG=/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg LRS_DV_FRAMES=90 \
      "$VENV" "$TOOL" >/tmp/smoke-live.log 2>&1
    if grep -q '"result": "PASS"' /tmp/smoke-live.log; then
      pass "live validation PASS ($(grep -oE '"stutter_count": [0-9]+' /tmp/smoke-live.log | head -1))"
    else
      fail "live validation"; grep -oE '"result": "[A-Z]+"|"checks": \{[^}]*\}' /tmp/smoke-live.log | head -2
    fi
  fi
else
  echo "  (skipped — pass --live for a short tripwire-guarded on-screen run)"
fi

echo
if [ "$fails" -eq 0 ]; then echo "SMOKE: PASS"; else echo "SMOKE: FAIL ($fails failures)"; fi
exit "$fails"
