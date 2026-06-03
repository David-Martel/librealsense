# RealSense on DGX Spark GB10 — Analysis Package (2026-06-02)

Host `spark-3066` · Intel RealSense **D435** · NVIDIA **DGX Spark / GB10** · Ubuntu 24.04.4 · kernel 6.17.0-1021-nvidia

## Bottom line
The "persistent disconnect / non-USB-3 enumeration" problem was a **USB topology fault** — the camera was tunneled through a USB-C dock's **USB-2.0-only hub chain** (Bus 5, 480M). Moving it to a **rear-panel native USB-C port + eMarker cable** put it on a SuperSpeed root (Bus 2, USB 3.2, 5000M) with full profiles and **validated stable streaming (0 failures, 0 kernel faults over ~4 min + 15 start/stop cycles)**. Not a camera, firmware, or librealsense defect.

## Read in this order
| Doc | Contents |
|---|---|
| [`00-executive-summary.md`](00-executive-summary.md) | One-page before/after, root cause, ranked recommendations |
| [`10-environment-and-sdk-inventory.md`](10-environment-and-sdk-inventory.md) | SDK footprints (3 + ROS), David-Martel fork mapping, udev rules |
| [`20-customization-critical-review.md`](20-customization-critical-review.md) | Critical review of the GB10 fork: good vs band-aid vs stale-claim, scorecard |
| [`30-log-analysis-and-root-cause.md`](30-log-analysis-and-root-cause.md) | Kernel/SDK log evidence decoded; signal recalibration; root-cause statement |
| [`40-candidate-solutions.md`](40-candidate-solutions.md) | 13 candidate solutions across controller/library/firmware/OS, ranked |
| [`50-remediation-plan.md`](50-remediation-plan.md) | Staged, gated, test-driven plan (Stages 0–6) |
| [`60-low-level-usb-debugging.md`](60-low-level-usb-debugging.md) | usbmon / xhci ftrace / pcap capture + errno decode + failure playbook |
| [`70-controller-crash-finding.md`](70-controller-crash-finding.md) | **CRITICAL:** load-induced GB10 xHCI controller death (3-stream 848×480@60); safe envelope; reboot recovery |
| [`80-multistream-root-cause-deepdive.md`](80-multistream-root-cause-deepdive.md) | **Deep root cause:** exact mechanism (dual-driver contention + control-path starvation → Stop-Endpoint-timeout → HC died), 8 hypotheses confirmed/refuted w/ code citations, librealsense-fixable vs NVIDIA-only, post-reboot test plan |
| [`85-librealsense-patch-specs.md`](85-librealsense-patch-specs.md) | **Patch specs P1–P6** (eager uvcvideo detach, deeper URB pool, usbfs preflight, gentler stop, udev rule, NVIDIA escalation) with code citations, acceptance criteria, risk, upstreamability |
| [`90-validation-results.md`](90-validation-results.md) | Bus-2 stability run: fault tally, sustained + stress results |

## Tooling
- **[`../bin/rs-gb10-healthcheck.sh`](../bin/rs-gb10-healthcheck.sh)** — idempotent, independently-verifiable health gate.
  - Default = **visible** (renders captured frames on screen + saves evidence images). `--headless` for CI/soak.
  - Emits `result.json` (structured PASS/FAIL vs explicit thresholds), `report.md`, raw artifacts, and a `SHA256SUMS` manifest.
  - Scores only genuine host-stack USB faults; **excludes** benign librealsense `USBDEVFS_CLEAR_HALT` and `981ae2 -5` UVC noise.
  ```bash
  ~/realsense-gb10-validation/bin/rs-gb10-healthcheck.sh            # visible, hd15+vga30 60s each
  ~/realsense-gb10-validation/bin/rs-gb10-healthcheck.sh --quick    # 15s smoke
  ~/realsense-gb10-validation/bin/rs-gb10-healthcheck.sh --soak 1800 --headless   # 30-min soak
  ```

## Evidence directories (raw)
- Working (Bus 2, USB 3.2): `../20260602-214043-bus2-usb3-stability/` and `../healthcheck-*/`
- Failing baseline (Bus 5, USB 2.0): `../20260602-143904/`, `../20260602-144941-rsusb-unbound/`, `../20260602-1456*-no-camera-diagnostic/`

## Headline recommendations
1. **Native Type-C port + eMarker cable only** — never the dock. *(done/confirmed)*
2. Remove global power band-aids (`autosuspend=-1`/`power/control=on` on all USB+PCI, `pcie_aspm=off`); scope keep-awake by serial only if measured.
3. Raise `usbcore.usbfs_memory_mb` from 16 via a config file.
4. Enforce "USB2 = hard fail" in the capture path.
5. Controlled firmware update 5.13.0.55 → 5.16.0.1/5.17.0.9 **after** the link is GREEN.
6. Use the healthcheck harness as the regression gate after every kernel/BSP/SDK/firmware change.
