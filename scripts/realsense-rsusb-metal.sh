#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-status}"
REALSense_IDS=("8086:0b07" "8086:0ad1" "8086:0ad2" "8086:0ad3" "8086:0b3a" "8086:0b3d" "8086:0b5c")
ALLOW_UVC_UNBIND="${LRS_GB10_ALLOW_UVC_UNBIND:-${REALSENSE_RSUSB_ALLOW_UVC_UNBIND:-0}}"

is_realsense_device() {
  local dev="$1"
  local vendor product id
  [[ -r "$dev/idVendor" && -r "$dev/idProduct" ]] || return 1
  vendor="$(cat "$dev/idVendor")"
  product="$(cat "$dev/idProduct")"
  id="${vendor}:${product}"
  for known in "${REALSense_IDS[@]}"; do
    [[ "$id" == "$known" ]] && return 0
  done
  [[ -r "$dev/product" ]] && grep -qi 'RealSense' "$dev/product"
}

write_if_writable() {
  local path="$1"
  local value="$2"
  [[ -e "$path" ]] || return 0
  printf '%s\n' "$value" 2>/dev/null | tee "$path" >/dev/null 2>&1 || true
}

apply_power_tuning() {
  write_if_writable /sys/module/usbcore/parameters/autosuspend -1
  for control in /sys/bus/usb/devices/*/power/control; do
    [[ -e "$control" ]] || continue
    write_if_writable "$control" on
  done
  for delay in /sys/bus/usb/devices/*/power/autosuspend_delay_ms; do
    [[ -e "$delay" ]] || continue
    write_if_writable "$delay" -1
  done
}

status() {
  for controller in /sys/bus/platform/devices/NVDA8000:* /sys/bus/platform/devices/NVDA8001:*; do
    [[ -e "$controller" ]] || continue
    local driver="unbound"
    [[ -e "$controller/driver" ]] && driver="$(basename "$(readlink -f "$controller/driver")")"
    printf 'xhci=%s driver=%s runtime=%s power=%s\n' \
      "$(basename "$controller")" \
      "$driver" \
      "$(cat "$controller/power/runtime_status" 2>/dev/null || true)" \
      "$(cat "$controller/power/control" 2>/dev/null || true)"
  done

  for dev in /sys/bus/usb/devices/*; do
    [[ -d "$dev" ]] || continue
    is_realsense_device "$dev" || continue
    local name vendor product speed authorized power
    name="$(cat "$dev/product" 2>/dev/null || true)"
    vendor="$(cat "$dev/idVendor" 2>/dev/null || true)"
    product="$(cat "$dev/idProduct" 2>/dev/null || true)"
    speed="$(cat "$dev/speed" 2>/dev/null || true)"
    authorized="$(cat "$dev/authorized" 2>/dev/null || true)"
    power="$(cat "$dev/power/control" 2>/dev/null || true)"
    printf 'device=%s id=%s:%s speed=%s authorized=%s power=%s name=%s\n' \
      "$(basename "$dev")" "$vendor" "$product" "$speed" "$authorized" "$power" "$name"
    for iface in "$dev":*; do
      [[ -e "$iface" ]] || continue
      local driver=""
      [[ -e "$iface/driver" ]] && driver="$(basename "$(readlink -f "$iface/driver")")"
      printf '  interface=%s driver=%s\n' "$(basename "$iface")" "$driver"
    done
  done
}

unbind_uvcvideo() {
  if [[ "$ALLOW_UVC_UNBIND" != "1" ]]; then
    cat >&2 <<'WARN'
Refusing to unbind RealSense interfaces from uvcvideo by default.

On DGX Spark / GB10, RSUSB direct ownership can wedge the NVDA8000 xHCI
controller during endpoint stop/reset sequences. Keep uvcvideo bound for normal
operation and use a native V4L2 librealsense build for production validation.

To run an explicit fault-isolation experiment, set:
  LRS_GB10_ALLOW_UVC_UNBIND=1 realsense-rsusb-metal unbind-uvcvideo
WARN
    exit 1
  fi

  local dead_controller=0
  for controller in /sys/bus/platform/devices/NVDA8000:* /sys/bus/platform/devices/NVDA8001:*; do
    [[ -e "$controller" ]] || continue
    if [[ ! -e "$controller/driver" ]]; then
      printf 'refusing unbind: xHCI controller %s is not bound to a driver\n' "$(basename "$controller")" >&2
      dead_controller=1
    fi
  done
  [[ "$dead_controller" -eq 0 ]] || exit 1

  apply_power_tuning
  for dev in /sys/bus/usb/devices/*; do
    [[ -d "$dev" ]] || continue
    is_realsense_device "$dev" || continue
    for iface in "$dev":*; do
      [[ -e "$iface/driver" ]] || continue
      if [[ "$(basename "$(readlink -f "$iface/driver")")" == "uvcvideo" ]]; then
        printf '%s\n' "$(basename "$iface")" > /sys/bus/usb/drivers/uvcvideo/unbind
        printf 'unbound %s from uvcvideo\n' "$(basename "$iface")"
      fi
    done
  done
}

rebind_uvcvideo() {
  modprobe uvcvideo >/dev/null 2>&1 || true
  for dev in /sys/bus/usb/devices/*; do
    [[ -d "$dev" ]] || continue
    is_realsense_device "$dev" || continue
    for iface in "$dev":*; do
      [[ -e "$iface" ]] || continue
      [[ -e "$iface/driver" ]] && continue
      printf '%s\n' "$(basename "$iface")" > /sys/bus/usb/drivers/uvcvideo/bind 2>/dev/null || true
    done
  done
}

case "$MODE" in
  status) status ;;
  tune) apply_power_tuning; status ;;
  unbind-uvcvideo) unbind_uvcvideo; status ;;
  rebind-uvcvideo) rebind_uvcvideo; status ;;
  *)
    cat >&2 <<'USAGE'
Usage: realsense-rsusb-metal [status|tune|unbind-uvcvideo|rebind-uvcvideo]

This helper keeps RealSense USB runtime power disabled and reports GB10 xHCI
controller health. RealSense-only uvcvideo unbind is disabled by default because
it can wedge the host xHCI controller on this platform; set
LRS_GB10_ALLOW_UVC_UNBIND=1 only for controlled fault-isolation experiments.
USAGE
    exit 2
    ;;
esac
