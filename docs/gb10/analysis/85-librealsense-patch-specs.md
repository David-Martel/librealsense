# 8b. librealsense Patch Specifications & Requirements

> Concrete, citeable specs for the librealsense-side mitigations identified in `80-multistream-root-cause-deepdive.md`. These reduce/remove the **trigger** of the GB10 xHCI controller death; they do **not** (cannot) fix the controller halt itself (NVIDIA BSP/firmware — see PATCH‑6 escalation). Targets verified in `~/dev/repos/librealsense` @ `81f35eb81`. **Status: SPEC ONLY — not yet implemented.** Validation of every patch is gated on a reboot (controller currently dead) and the §8.7 guarded ramp.

## Priorities

| ID | Patch | Layer | Value | Risk | Upstreamable |
|---|---|---|---|---|---|
| **P1** | Eager full-device uvcvideo detach | backend C++ | ★★★ highest | med | yes (opt-in) |
| **P2** | Configurable / deeper URB pool for D400 multistream | backend C++ | ★★ | low | yes |
| **P3** | `usbfs_memory_mb` preflight check + warning | backend C++ | ★ | none | yes |
| **P4** | Gentler, serialized stop/cancel (clear_halt storm) | backend C++ | ★★ | med | yes |
| **P5** | Operational: udev rule to keep uvcvideo off RealSense ifaces | config/ops | ★★★ (deploy now) | low | n/a (deploy) |
| **P6** | NVIDIA xHCI controller-death escalation | platform | ★★★ (the real fix) | n/a | n/a |

---

## P1 — Eager, full-device kernel-driver (uvcvideo) detach before claim

**Problem (root cause H5, §8.3).** The RSUSB backend relies on **lazy, per-interface** auto-detach scoped only to the VideoControl + associated VideoStreaming interfaces it claims:
- `src/libusb/handle-libusb.h:57` — `libusb_set_auto_detach_kernel_driver(_handle, true)` then `claim_interface` of own + `get_associated_interfaces()`.
- `src/libusb/device-libusb.cpp:35-43` — association groups only VC→following-VS within one group.

Result: `uvcvideo` stays bound to every other UVC interface (observed probing `2-1:1.3` with `-110` while librealsense started streams). This dual-driver contention is the confirmed trigger.

**Spec.**
1. Add a device-open path that, **before any `libusb_claim_interface`**, iterates **all** interfaces of the active config and, for each where `libusb_kernel_driver_active(handle, i) == 1`, calls `libusb_detach_kernel_driver(handle, i)` (explicit, eager) — not just the claimed group.
2. Record which interfaces were force-detached so they can be **re-attached on close** (parity with current auto-detach re-attach-on-release behavior; `libusb_attach_kernel_driver`).
3. Make it **opt-in and default-off upstream** (flag `RS2_USB_DETACH_ALL_INTERFACES` / device-open option) to avoid surprising single-stream/V4L2 users; **default-on in the GB10 build profile**.
4. Detach must be **idempotent** and tolerate `LIBUSB_ERROR_NOT_FOUND` (already detached).

**Touch points:** `src/libusb/handle-libusb.h` (around :57), `src/libusb/device-libusb.cpp` (config/interface enumeration ~:30-60), the `usb_device_libusb::open`/`get_subdevices` path, and a re-attach hook on handle destruction.

**Acceptance criteria.** Post-reboot, with P1 on, the §8.7 **T3** 3‑stream@848×480@60 run shows the `uvcvideo … -110` control-timeout count drop to **0** in the journal delta (vs 6 in the unpatched crash), and the tripwire does not fire from a uvcvideo-attributed control timeout. (Does **not** by itself prove the controller won't die under pure bulk load — that's P2/P4 + NVIDIA.)

**Risk.** Detaching uvcvideo from interfaces librealsense doesn't stream removes `/dev/video*` for those while open; must re-attach cleanly on close. Verify no leak of detached state across open/close cycles.

---

## P2 — Configurable, deeper URB pool for D400 high-bandwidth multistream

**Problem (H8, §8.3).** Only **2 URBs per stream** with ~814 KB buffers (848×480 Z16) — a thin pipeline for 3 saturating bulk streams.
- `src/uvc/uvc-device.h:42` — `rs_uvc_device(..., uint8_t usb_request_count = 2)`.
- `src/uvc/uvc-device.cpp:82-86,465` — stored as `_usb_request_count`, passed to `uvc_streamer_context`.
- `src/uvc/uvc-streamer.cpp:26,141` — `_read_buff_length = header + dwMaxVideoFrameSize`; exactly `request_count` `usb_request_libusb` objects created.

**Spec.**
1. Expose `usb_request_count` as a **runtime-configurable** value (device option / env `RS2_USB_REQUEST_COUNT`, or per-product-line default) instead of a hard-coded 2.
2. Raise the **default for D400 over RSUSB when ≥2 high-bandwidth streams** are active (e.g. 4–8), bounded by available `usbfs_memory_mb` (coordinate with P3).
3. Document the memory cost: `request_count × _read_buff_length × active_streams` must fit under `usbfs_memory_mb`.

**Touch points:** `src/uvc/uvc-device.h:42`, `src/uvc/uvc-device.cpp:76` (construction site), option plumbing in `uvc-sensor`.

**Acceptance criteria.** Post-reboot §8.7 **T4**: with deeper pool + raised `usbfs_memory_mb`, the 3‑stream safe ceiling rises and/or `-110`/drop counts fall vs P0 baseline, measured by the guarded ramp. No regression in single/dual-stream fps.

**Risk.** Low. Over-large pools waste pinned memory; bound by P3 check.

---

## P3 — `usbfs_memory_mb` preflight check + warning

**Problem.** librealsense never reads or sets `usbfs_memory_mb` (`grep` of `src/` returns nothing); default is 16 MB, tight for high-bandwidth multistream bulk allocations.

**Spec.**
1. On Linux device open, read `/sys/module/usbcore/parameters/usbfs_memory_mb`.
2. If `< required` (computed from active profiles × `request_count` × buffer size, or a conservative 256 MB threshold), emit a single `LOG_WARNING` with the exact remediation (`echo 1000 | sudo tee …` and the persistent `/etc/modprobe.d` form).
3. Read-only; never write the sysfs value from the SDK (respects the "config files, inspectable" principle).

**Touch points:** new small helper in `src/libusb/` or `src/linux/`, called from the RSUSB device-open path.

**Acceptance criteria.** With `usbfs_memory_mb=16`, opening a D400 logs the warning exactly once; with `≥256`, silent. Unit-testable by mocking the sysfs read.

**Risk.** None (advisory only).

---

## P4 — Gentler, serialized stop / cancel (reduce the clear_halt storm)

**Problem (H7 trigger surface).** The stop path fires `libusb_cancel_transfer` + `libusb_clear_halt` per stream, which map onto the kernel **Stop Endpoint** command that the GB10 xHCI died on.
- `src/uvc/uvc-streamer.cpp:176-209` — `stop()` cancels each request then `reset_endpoint`.
- `src/libusb/messenger-libusb.cpp:23-34` — `reset_endpoint` = `libusb_clear_halt`; `:98-109` — `cancel_request`.
- `src/uvc/uvc-streamer.cpp:98-109` — per-stream stall watchdog also issues `reset_endpoint` once streaming.

**Spec.**
1. **Serialize** stop across streams of a device (one stream fully cancelled/halted before the next) rather than issuing concurrent clear_halt/cancel from multiple event threads — reduces the burst of control commands hitting a struggling controller.
2. Add a small, configurable **settle delay** between consecutive stream stops and between MI0 (depth) and MI3 (color) **starts** (the resolver already documents depth/color coupling at `src/pipeline/resolver.h:139-145`).
3. Make the stall-watchdog `reset_endpoint` **rate-limited / backoff** instead of immediate, so a transient stall under load doesn't add to the control-command storm.

**Touch points:** `src/uvc/uvc-streamer.cpp` (stop/start ordering, watchdog), `src/uvc/uvc-device.cpp:122-125` (`stream_on` loop).

**Acceptance criteria.** Post-reboot §8.7 **T5**: staggered start + serialized stop reduces setup/teardown `-110` counts vs baseline; no fps regression; clean stop within the profiler's `--hard-stop-ms`.

**Risk.** Medium — adds latency to start/stop; must stay within the profiler's stop watchdog budget. Validate stop timing unchanged for single-stream.

---

## P5 — Operational udev rule (deployable NOW, P1's effect without a rebuild)

**Spec.** Ship a rule that prevents kernel `uvcvideo` from binding RealSense (8086 D400) interfaces so only the RSUSB/libusb backend drives them — eliminating the dual-driver contention without a code change. Approaches (pick one, test post-reboot):
- `ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0b07", DRIVERS=="uvcvideo", RUN+="/bin/sh -c 'echo $kernel > /sys/bus/usb/drivers/uvcvideo/unbind'"` (unbind on add), **or**
- the fork's existing `realsense-rsusb-metal unbind-uvcvideo` invoked at session start, **or**
- USBGuard authorization by serial (per `realsense.TODO.md:24-25`).

**Acceptance criteria.** After applying, `lsusb -t` / driver column shows the D400 streaming interfaces **not** bound to uvcvideo; the §8.7 T3 `-110` storm disappears. **Caveat:** unbinding uvcvideo removes `/dev/video*` for the camera — fine for RSUSB-only use, **breaks** any V4L2 consumer; document the trade-off.

**Risk.** Low-med; reversible (`rebind-uvcvideo`).

---

## P6 — NVIDIA escalation (the only *permanent* fix for the crash)

**Requirement.** File a DGX Spark / GB10 bug with NVIDIA: GB10 `xhci_plat_hcd` (ACPI `NVDA8000:PNP0D15`) suffers an **unrecoverable** Stop-Endpoint-command timeout → `HC died` under multi-stream USB3 bulk load, with **no auto-recovery** (unlike Jetson `xhci-tegra` `en_hcd_reinit`). Attach `../20260602-2159-xhci-controller-death/{controller-death-journal.txt,DISCRIMINATOR.txt,SUMMARY.txt}` and request: (a) xHCI Stop-Endpoint/halt recovery hardening, (b) a controller re-init path equivalent to `tegra_xhci_hcd_reinit()` for the ACPI/plat driver, (c) any BSP/firmware with the fix. Re-validate with the guarded ramp after each BSP update.

---

## Cross-cutting requirements

- **Validation gate:** every code patch (P1–P4) is unverified until the controller is rebooted and the §8.7 guarded ramp (`rs-gb10-stress.sh --usbmon`) shows the predicted change in the `-110`/drop/fault counts with usbmon protocol evidence. No "fixed" claim without that delta (per `verification-before-completion`).
- **Backward compatibility:** P1/P2/P4 must be **opt-in upstream** (flags/options), default-on only in the GB10 build profile; must not change single-stream or V4L2-backend behavior.
- **Build:** GB10 build via `scripts/build-dgx-spark-gb10.sh`; keep `-D warnings`-clean (CLAUDE.md rule #1) and C++20 (fork standard).
- **Tests:** add a mockable unit test for P3 (sysfs read) and P2 (request_count plumbing); P1/P4 need the hardware guarded ramp (no pure unit test for USB detach/halt timing).
- **Scope discipline:** these are the *trigger-reduction* set; do not conflate with claiming the controller crash is "fixed" — P6 is the only crash fix.
