#!/usr/bin/env bash
# rs-gb10-usb2-guard.sh — USB2 fail-fast guard for RealSense D435 on DGX Spark / GB10.
#
# Scans sysfs for the D435 (idVendor=8086 idProduct=0b07).  If the device is present
# but enumerated at USB < 3.0 (SuperSpeed), refuses to continue — returning exit 1 with
# a clear remediation message — unless --allow-usb2 is given (degraded-mode opt-in).
#
# IDEMPOTENT AND READ-ONLY: no sysfs writes, no streaming, no side effects.
# Re-running with the same hardware state always produces the same exit code.
#
# Usage:
#   rs-gb10-usb2-guard.sh [--allow-usb2] [-h|--help]
#
#     --allow-usb2   On USB2 FAIL, emit a WARNING but exit 0 (degraded mode).
#                    Default: exit 1 (hard fail).
#     -h / --help    Print this usage and exit 0.
#
# Exit codes:
#   0  — USB3 pass (or FAIL with --allow-usb2)
#   1  — USB2/degraded link detected (and --allow-usb2 not given)
#   2  — Usage error or no D435 found on the system
#
# Sysfs root override (for testing without real hardware):
#   RS_USB_SYSFS_ROOT=/tmp/fake-usb rs-gb10-usb2-guard.sh
#
# Background:
#   The D435 "persistent disconnect" on the GB10 was root-caused to a USB-2.0 dock hub.
#   On a native USB-C 3.x rear-panel port it is rock-solid.  Production capture MUST
#   refuse to run in degraded link conditions; this script is the single reusable gate.

set -euo pipefail

# ---------- constants ----------
VENDOR="8086"
PRODUCT="0b07"
MIN_SPEED_M=5000          # SuperSpeed threshold (Mbps)
MIN_VERSION="3.0"         # USB version floor

# Allow sysfs root override for fixture-based testing.
SYSFS_ROOT="${RS_USB_SYSFS_ROOT:-/sys/bus/usb/devices}"

# ---------- args ----------
ALLOW_USB2=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --allow-usb2) ALLOW_USB2=1; shift ;;
        -h|--help)
            # Print the leading comment block (lines starting with #), stop at first non-comment.
            sed -n '2,/^[^#]/{ /^[^#]/q; p }' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'rs-gb10-usb2-guard: unknown argument: %s\n' "$1" >&2
            printf 'Usage: rs-gb10-usb2-guard.sh [--allow-usb2] [-h|--help]\n' >&2
            exit 2
            ;;
    esac
done

# ---------- helpers ----------

# read_attr <path> <attr>  — returns the trimmed value or "" on missing/unreadable.
# Never fails under set -e: 2>/dev/null || true absorbs any error.
read_attr() {
    local path="$1" attr="$2"
    local val
    val="$(cat "${path}/${attr}" 2>/dev/null || true)"
    # trim leading/trailing whitespace (version has a leading space on Linux: " 3.20")
    printf '%s' "${val//[[:space:]]/}"
}

# numeric_ge <value_string> <threshold_float>
# Uses awk so it handles "5000", "5000.0", " 3.20", "", etc. correctly.
# Empty/unreadable attr -> awk sees "" -> +0 == 0 -> returns false (natural FAIL).
numeric_ge() {
    local val="$1" thr="$2"
    awk -v v="$val" -v t="$thr" 'BEGIN { exit !(v+0 >= t+0) }'
}

# ---------- scan sysfs ----------
found_path=""
found_speed=""
found_version=""

for devpath in "${SYSFS_ROOT}"/*/; do
    # Skip if idVendor/idProduct don't match; missing attrs handled by read_attr.
    vid="$(read_attr "$devpath" "idVendor")"
    pid="$(read_attr "$devpath" "idProduct")"
    [[ "$vid" == "$VENDOR" && "$pid" == "$PRODUCT" ]] || continue

    found_path="$devpath"
    found_speed="$(read_attr "$devpath" "speed")"
    found_version="$(read_attr "$devpath" "version")"
    break   # first match is sufficient; D435 is a single-device camera
done

# ---------- verdict: no device ----------
if [[ -z "$found_path" ]]; then
    printf 'rs-gb10-usb2-guard: no D435 found (vendor=%s product=%s) under %s\n' \
        "$VENDOR" "$PRODUCT" "$SYSFS_ROOT" >&2
    exit 2
fi

# ---------- verdict: speed + version check ----------
speed_ok=0
version_ok=0
numeric_ge "$found_speed"   "$MIN_SPEED_M"   && speed_ok=1   || true
numeric_ge "$found_version" "$MIN_VERSION"   && version_ok=1 || true

# Display version without leading spaces for messages (read_attr already stripped them).
disp_version="${found_version:-unknown}"
disp_speed="${found_speed:-unknown}"

if [[ $speed_ok -eq 1 && $version_ok -eq 1 ]]; then
    printf 'USB3 OK speed=%sM version=%s path=%s\n' \
        "$disp_speed" "$disp_version" "$found_path"
    exit 0
fi

# ---------- FAIL path ----------
REMEDIATION="D435 is on a USB2 link (speed=${disp_speed}M version=${disp_version}, path ${found_path}). Move it to a rear-panel native USB-C port + eMarker cable; do NOT use the dock. Refusing to start (override with --allow-usb2 for a degraded run)."

if [[ $ALLOW_USB2 -eq 1 ]]; then
    printf 'WARNING: %s\n' "$REMEDIATION" >&2
    printf 'WARNING: proceeding anyway (--allow-usb2 override active)\n' >&2
    exit 0
else
    printf 'ERROR: %s\n' "$REMEDIATION" >&2
    exit 1
fi
