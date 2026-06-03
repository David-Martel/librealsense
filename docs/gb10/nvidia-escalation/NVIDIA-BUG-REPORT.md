# NVIDIA DGX Spark / GB10 xHCI controller (NVDA8000) is permanently wedged by a Stop-Endpoint command issued after a USB control-transfer timeout — no recovery path, reboot required

## Severity / Impact

**Severity: High (platform robustness / availability defect).**

On the DGX Spark / GB10, any USB peripheral that ever times out a control transfer
(`-110` / `ETIMEDOUT`) can permanently disable the xHCI host controller it is attached
to. Once the controller receives a **Stop-Endpoint** command after such a timeout, it
fails to halt, is marked dead (`HC died`), and **does not recover until a full system
reboot**. Driver unbind/rebind of `xhci-hcd` does **not** recover the controller. This is
a single-fault, non-recoverable wedge of a core platform I/O subsystem on an enterprise
product: a transient, recoverable error condition on a downstream device escalates into a
permanent host-controller outage.

This has been reproduced **three times**, across **two independent USB software backends**
(userspace libusb and the in-kernel `uvcvideo` driver), which localizes the defect to the
NVIDIA GB10 controller / BSP rather than to any application or USB library.

## Affected platform (exact versions)

| Component | Value |
|---|---|
| Host | `spark-3066`, NVIDIA DGX Spark / GB10 |
| CPU | ARM64 aarch64 (Cortex-X925 + Cortex-A725) |
| OS | Ubuntu 24.04.4 LTS |
| Kernel | `6.17.0-1021-nvidia` |
| CUDA | 13.0 |
| NVIDIA driver | 580.159.03 |
| USB host controller | xHCI, ACPI id **NVDA8000** (driver `xhci_plat_hcd`, ACPI class `PNP0D15`) |
| Device under test | Intel RealSense **D435** (UVC camera), firmware 5.13.0.55 |
| Link | native **rear** USB-C port, enumerating at USB 3.2 **SuperSpeed (5000 Mbps)** |

Note: GB10 exposes multiple xHCI instances (observed `NVDA8000:00`–`NVDA8000:04`,
`NVDA8001:00`). The defect has been triggered on `NVDA8000:00` and `NVDA8000:02`. The
controller is the generic ACPI **`xhci_plat_hcd`**, **not** the Jetson `xhci-tegra`
driver — and therefore has **no controller re-init / recovery path** (see Technical
Analysis).

## Summary

On the GB10, after any control-path transfer to a device behind the `NVDA8000` xHCI times
out with `-110`, the controller can no longer complete a subsequent **Stop-Endpoint**
command. The kernel signature is identical on every occurrence: `xHCI host not responding
to stop endpoint command` → `Host halt failed, -110` → `xHCI host controller not
responding, assume dead` → `HC died; cleaning up`. The controller is then dead until the
machine is rebooted; an `xhci-hcd` unbind/rebind does not recover it. We have reproduced
this three times under different triggers and across both the libusb (RSUSB) and the
in-kernel `uvcvideo` (V4L2) USB paths, demonstrating the fault is in the controller / BSP
and not in any userspace component.

## Reproduction

The common, backend-independent invariant is: **a control-transfer `-110` timeout on a
device behind the controller, followed by a Stop-Endpoint command, is unsurvivable.** The
specific trigger that produces the `-110` storm varies; the fatal step is always the same.

| # | Date / time | USB backend | Controller | Trigger |
|---|---|---|---|---|
| 1 | 2026-06-02 21:59 | RSUSB (libusb) | `NVDA8000:00` | 3× concurrent `848x480@60` streams (depth+color+IR) → bandwidth / control-path saturation → `-110` storm → Stop-Endpoint → HC died |
| 2 | 2026-06-03 08:04 | RSUSB (libusb) | `NVDA8000:00` | Mid-session pipeline/context teardown: app released the device, kernel `uvcvideo` re-bound and re-probed UVC control endpoints, that re-probe hit a `-110` control-transfer storm (08:03:48–08:03:59), then the Stop-Endpoint at teardown killed the controller (08:04:09) |
| 3 | 2026-06-03 09:02 | **native V4L2 / in-kernel `uvcvideo`** | `NVDA8000:02` | Concurrent dual depth+color `848x480@60` setup; a control-path `-110` during setup (09:01:57, 09:02:02) → Stop-Endpoint → HC died (09:02:07) |

Notes on the incidents:

- **Incident #1** is summarized from forensic prose (no per-line kernel timestamps were
  captured for that run); the sequence recorded was: `failed to set power state` →
  `UVC control -110 (ETIMEDOUT)` storm → `Not enough bandwidth for altsetting 0` →
  `Host halt failed, -110` → `HC died` → device gone. Software recovery via `xhci-hcd`
  unbind/rebind **failed (`-110`)**; reboot required.

- **Incident #2 is explicitly NOT a malformed/unsupported video profile.** The requested
  `Color 848x480 BGR8 @60` profile resolved correctly to `Color 848x480 YUYV @60`
  (the format is advertised by the device), and the USB link was clean USB-3.2 throughout.
  The trigger was the **control-transfer `-110` storm during the kernel `uvcvideo` re-probe
  at device handoff** when the application destroyed and recreated its capture
  context/pipeline mid-session, handing the device back to `uvcvideo`. The controller was
  already dead before the color stream's `open()` was ever issued.

- **Incident #3 is the decisive evidence.** It occurred on the **in-kernel `uvcvideo`
  (V4L2) path with no userspace USB library involved at all** — `uvcvideo` is the sole
  driver, there is no libusb claim and no userspace `clear_halt` activity. Single depth
  `848x480@60` and single color `848x480@60` each streamed cleanly first on the same
  backend/controller; only the dual-stream setup tripped the `-110` and killed the
  controller. This proves the defect is in the controller / BSP, not in any userspace USB
  stack.

### Minimal repro

The cleanest backend-independent reproduction is **incident #3**: on the stock in-kernel
`uvcvideo` driver, bring up two concurrent UVC streams (`848x480@60` depth + color) from a
single RealSense D435 on a native USB-3 port. The dual-stream setup issues a UVC control
query that times out (`-110`); the teardown Stop-Endpoint then kills the controller. More
generally, **any** code path that induces a control-transfer `-110` on a device behind the
`NVDA8000` controller and is followed by a Stop-Endpoint command reproduces the wedge.
There is no deterministic single-command trigger; the `-110` precondition is load- and
timing-dependent, but once it occurs the Stop-Endpoint failure is consistent.

## Kernel evidence (verbatim)

The fatal signature is identical on every occurrence. **Incident #2** has the complete
four-line signature (`NVDA8000:00`, 2026-06-03 08:04):

```text
Jun 03 08:03:48 spark-3066 kernel: usb 2-1: Failed to query (SET_CUR) UVC control 5 on unit 7: -110 (exp. 1).
Jun 03 08:03:48 spark-3066 kernel: usb 2-1: Failed to query (GET_DEF) UVC control 20 on unit 1: -110 (exp. 10).
Jun 03 08:03:48 spark-3066 kernel: usb 2-1: UVC non compliance: permanently disabling control 981ae2 (Region of Interest Auto Ctrls), due to error -110
Jun 03 08:03:54 spark-3066 kernel: usb 2-1: Failed to query (GET_CUR) UVC control 4 on unit 2: -110 (exp. 2).
Jun 03 08:03:54 spark-3066 kernel: usb 2-1: Failed to query (GET_INFO) UVC control 11 on unit 7: -110 (exp. 1).
Jun 03 08:03:59 spark-3066 kernel: usb 2-1: Failed to query (GET_DEF) UVC control 20 on unit 1: -110 (exp. 10).
Jun 03 08:03:59 spark-3066 kernel: usb 2-1: Failed to query (GET_INFO) UVC control 2 on unit 6: -110 (exp. 1).
Jun 03 08:04:09 spark-3066 kernel: xhci-hcd NVDA8000:00: xHCI host not responding to stop endpoint command
Jun 03 08:04:09 spark-3066 kernel: xhci-hcd NVDA8000:00: Host halt failed, -110
Jun 03 08:04:09 spark-3066 kernel: xhci-hcd NVDA8000:00: xHCI host controller not responding, assume dead
Jun 03 08:04:09 spark-3066 kernel: xhci-hcd NVDA8000:00: HC died; cleaning up
Jun 03 08:04:09 spark-3066 kernel: usb 2-1: USB disconnect, device number 2
```

**Incident #3** reproduces the same pattern on a **different controller (`NVDA8000:02`)**
and on the in-kernel `uvcvideo` (V4L2) path (2026-06-03 09:02). The captured kernel lines
(verbatim, as recorded):

```text
Jun 03 09:01:57 spark-3066 kernel: usb 6-1: Failed to query (GET_CUR) UVC control 1 on unit 3: -110 (exp. 1024).
Jun 03 09:02:02 spark-3066 kernel: usb 6-1: Failed to query (GET_CUR) UVC control 1 on unit 3: -110 (exp. 1024).
Jun 03 09:02:07 spark-3066 kernel: xhci-hcd NVDA8000:02: xHCI host not responding to stop endpoint command
Jun 03 09:02:07 spark-3066 kernel: xhci-hcd NVDA8000:02: Host halt failed, -110
Jun 03 09:02:07 spark-3066 kernel: xhci-hcd NVDA8000:02: HC died; cleaning up
Jun 03 09:02:07 spark-3066 kernel: usb 6-1: USB disconnect, device number 10
```

The canonical fatal signature (always in this order) is:

```text
xhci-hcd NVDA8000:0X: xHCI host not responding to stop endpoint command
xhci-hcd NVDA8000:0X: Host halt failed, -110
xhci-hcd NVDA8000:0X: xHCI host controller not responding, assume dead
xhci-hcd NVDA8000:0X: HC died; cleaning up
```

## Technical analysis

1. **Precondition — control-transfer timeout.** A control transfer (EP0) to a device
   behind the `NVDA8000` controller times out with `-110` (`ETIMEDOUT`). This can be
   induced by several conditions (concurrent high-bandwidth bulk streams starving the
   control path; a `uvcvideo` re-probe storm at device handoff; concurrent dual-stream
   setup). The specific cause of the timeout is not the issue — on healthy controllers a
   control timeout is a recoverable, transient error.

2. **Fatal step — Stop-Endpoint after the timeout.** When the driver subsequently issues a
   **Stop-Endpoint** command (e.g. at stream teardown, or while cleaning up the stuck
   endpoint), the `NVDA8000` controller cannot complete it: `xHCI host not responding to
   stop endpoint command`. The host-halt that follows also fails (`Host halt failed,
   -110`), the controller is marked dead, and the HCD tears itself down. This is the
   precise defect: **the controller cannot complete a Stop-Endpoint command after a
   control-path `-110`.**

3. **No recovery path on the ACPI `xhci_plat_hcd`.** On the GB10 the controller is the
   generic ACPI-enumerated `xhci_plat_hcd` (`NVDA8000` / `PNP0D15`). It has **no
   controller re-init / recovery path**. Once `HC died` fires there is no automatic reset,
   and a manual `xhci-hcd` driver unbind/rebind does **not** bring it back — only a full
   system reboot recovers the controller (and the attached device).

4. **Contrast with Jetson — `tegra_xhci_hcd_reinit()`.** On Jetson/Orin platforms the
   xHCI controller is driven by `xhci-tegra`, which provides a controller re-initialization
   path (`tegra_xhci_hcd_reinit()`). When a host error or a stuck endpoint occurs there, the
   driver can reset/re-initialize the controller and recover **without a reboot**. The GB10
   ACPI `xhci_plat_hcd` has no equivalent. This contrast is the crux of the request below:
   the GB10 needs a recovery path analogous to Jetson's, so that a downstream device error
   does not become a permanent host-controller outage.

5. **Why this is a controller / BSP defect, not a software defect.** The wedge reproduces
   identically (a) across two independent USB software backends (userspace libusb and the
   in-kernel `uvcvideo` driver), (b) across multiple distinct triggers, and (c) on more than
   one physical controller instance (`NVDA8000:00`, `NVDA8000:02`). The only common
   denominator is the `NVDA8000` controller's inability to complete a Stop-Endpoint after a
   control-path `-110`, with no recovery path. No userspace change can fix this — at best it
   can reduce how often the `-110` precondition is hit.

## Requested action

1. **Root-cause** why the `NVDA8000` xHCI cannot complete a **Stop-Endpoint** command after
   a control-path transfer timeout (`-110`), causing `Host halt failed` → `HC died`. Is this
   a controller-firmware, BSP, or `xhci_plat_hcd` driver issue on GB10?

2. **Add a controller re-init / recovery path** for the ACPI `xhci_plat_hcd` on GB10,
   equivalent to Jetson's `tegra_xhci_hcd_reinit()`, so that a host error or stuck endpoint
   triggers a controller reset/re-init **instead of requiring a full system reboot**. A
   working `xhci-hcd` unbind/rebind recovery would also be acceptable; today it returns
   `-110` and does not recover the controller.

3. **Provide any DGX Spark BSP / firmware update or interim workaround** that prevents the
   Stop-Endpoint-after-control-timeout failure, or that restores a stuck controller without
   a reboot.

**Impact statement for prioritization:** on this enterprise platform, any USB peripheral
that ever times out a control transfer can permanently disable the USB host controller it
is attached to until the machine is rebooted. This is a serious robustness and availability
defect for a system intended to run unattended AI/robotics workloads with attached USB
sensors.

## Attachments

The following are available with this report (see the SUBMISSION-CHANNEL note for upload
guidance):

- Kernel log excerpts (the verbatim `-110` → Stop-Endpoint → `HC died` blocks above) and
  full kernel ring-buffer / journal for each incident (`journalctl -k`).
- Forensic SUMMARY / DISCRIMINATOR files for all three incidents:
  - `docs/gb10/forensics/20260602-2159-xhci-controller-death/{SUMMARY.txt,DISCRIMINATOR.txt}`
  - `docs/gb10/forensics/20260603-0804-xhci-controller-death-2/{SUMMARY.txt,DISCRIMINATOR.txt}`
  - `docs/gb10/forensics/20260603-0902-xhci-controller-death-3-V4L2/{SUMMARY.txt,DISCRIMINATOR.txt}`
- Raw capture directories: `~/realsense-gb10-validation/20260603-0804-xhci-controller-death-2/`
  and sibling incident directories.
- Platform/topology evidence: `uname -a`, `lsusb -t`, `lspci`, and the port→controller ACPI
  mapping (`readlink -f /sys/bus/usb/devices/usb*/..`) to show which xHCI instance each
  physical port maps to.
- NVIDIA's standard log-collection bundle if one is provided for DGX Spark
  (e.g. `nvidia-bug-report.sh` / a field-diagnostics bundle); otherwise the above kernel
  journals and platform dumps.

## Reporter mitigations (trigger reduction only — not a fix)

The reporter has shipped opt-in userspace mitigations in a librealsense fork that **reduce
the trigger surface** for the `-110` precondition but **cannot fix the controller defect**
(the Stop-Endpoint failure and the missing recovery path are entirely below userspace):

- A deeper URB pool (raises the default request count) to relieve control-path starvation.
- A `usbfs_memory_mb` advisory at device open.
- A gentler, rate-limited stop sequence (settle/cooldown between endpoint stops).
- A device re-acquire guard that detects/steers a mid-session release-and-reacquire churn
  (the incident-#2 handoff pattern).

These are mentioned only for completeness. They lower the probability of inducing a
control-transfer timeout; they do not, and cannot, prevent the controller from dying once a
`-110` is followed by a Stop-Endpoint. A platform-level fix (items 1–3 above) is required.
