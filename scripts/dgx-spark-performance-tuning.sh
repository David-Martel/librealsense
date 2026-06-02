#!/usr/bin/env bash
set -euo pipefail

write_if_writable() {
  local path="$1"
  local value="$2"
  [[ -e "$path" ]] || return 0
  printf '%s\n' "$value" 2>/dev/null | tee "$path" >/dev/null 2>&1 || true
}

for governor in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do
  write_if_writable "$governor" performance
done

for preference in /sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference; do
  write_if_writable "$preference" performance
done

write_if_writable /sys/module/usbcore/parameters/autosuspend -1

while IFS= read -r control; do
  write_if_writable "$control" on
done < <(find -L /sys/bus/usb/devices -path '*/power/control' 2>/dev/null)

while IFS= read -r autosuspend; do
  write_if_writable "$autosuspend" -1
done < <(find -L /sys/bus/usb/devices -path '*/power/autosuspend' 2>/dev/null)

while IFS= read -r delay; do
  write_if_writable "$delay" -1
done < <(find -L /sys/bus/usb/devices -path '*/power/autosuspend_delay_ms' 2>/dev/null)

while IFS= read -r control; do
  write_if_writable "$control" on
done < <(find -L /sys/bus/pci/devices -path '*/power/control' 2>/dev/null)

while IFS= read -r policy; do
  write_if_writable "$policy" max_performance
done < <(find -L /sys/class/scsi_host -path '*/link_power_management_policy' 2>/dev/null)

write_if_writable /sys/module/pcie_aspm/parameters/policy performance
write_if_writable /sys/module/nvme_core/parameters/default_ps_max_latency_us 0

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -pm ENABLED >/dev/null 2>&1 || true
fi

if command -v nvpmodel >/dev/null 2>&1; then
  nvpmodel -m 0 >/dev/null 2>&1 || true
fi

if command -v jetson_clocks >/dev/null 2>&1; then
  jetson_clocks >/dev/null 2>&1 || true
fi
