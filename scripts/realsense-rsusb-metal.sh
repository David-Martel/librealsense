#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-status}"
REALSense_IDS=("8086:0b07" "8086:0ad1" "8086:0ad2" "8086:0ad3" "8086:0b3a" "8086:0b3d" "8086:0b5c")

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
  while IFS= read -r control; do
    write_if_writable "$control" on
  done < <(find -L /sys/bus/usb/devices -path '*/power/control' 2>/dev/null)
  while IFS= read -r delay; do
    write_if_writable "$delay" -1
  done < <(find -L /sys/bus/usb/devices -path '*/power/autosuspend_delay_ms' 2>/dev/null)
}

status() {
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

The GB10 SDK is built with FORCE_RSUSB_BACKEND=ON. This helper keeps RealSense
USB runtime power disabled and can unbind only RealSense UVC interfaces from
uvcvideo so librealsense/libusb can own the interfaces directly.
USAGE
    exit 2
    ;;
esac
