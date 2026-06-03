# 4. Candidate Solutions

Organized by the four requested dimensions. Each carries a confidence and a verdict. **C‑1 is confirmed working**; the rest are hardening/robustness layers.

## Dimension A — Platform USB controller / cabling (the root cause)

### C‑1 ✅ CONFIRMED — Direct Type‑C root port + eMarker cable
- **Action:** Operate the D435 only on a Spark **rear‑panel native USB‑C port**, via an **eMarker (electronically‑marked) USB‑C** cable. Never through the USB‑2.0 dock.
- **Evidence:** moved camera to Bus 2, 5000M, USB 3.2, full profiles, sustained 30 fps / 0 failures.
- **Confidence:** High (empirically proven this session). **This is the fix.**

### C‑2 — If a hub is unavoidable, use a *certified USB‑3 (SuperSpeed) hub* with its own power
- Many docks present USB‑2.0‑only downstream hubs (as this one did). If a hub must be used, it must be a **powered USB‑3.x** hub, and you must **verify** the camera enumerates at 5000M behind it (the harness checks this). Treat the dock's USB‑2.0 hubs as off‑limits for the camera.
- **Confidence:** Medium — works only with genuinely SuperSpeed hubs.

### C‑3 — Account for the DGX Spark "before‑power‑up → 480M" quirk
- Documented Spark behaviour: devices present at boot, or via certain bridge chipsets, may negotiate 480M; **hot‑replug after boot** restores SuperSpeed. Operational rule: **attach the camera after the Spark is booted**, and verify speed.
- **Confidence:** Medium‑High (NVIDIA‑forum corroborated).

### C‑4 — Bandwidth headroom: raise `usbcore.usbfs_memory_mb`
- Default is **16 MB** (verified). RealSense high‑bandwidth multi‑stream benefits from more; Intel's standard guidance is to raise it. Prefer a **modprobe/sysctl config file** over a kernel cmdline band‑aid (per project "config files, not env/global" rule).
- `echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb` (runtime) and a persistent `/etc/modprobe.d/` or initramfs entry.
- **Confidence:** Medium (headroom/insurance; not the root cause).

## Dimension B — librealsense + local customizations

### C‑5 — Implement the "USB2 = hard fail" gate in the *capture path*, not just the profiler
- The profiler already defaults to refusing USB2 (`--allow-usb2` opt‑in). Extend the same gate to the production capture service: on open, read `Usb Type Descriptor`; if `< 3.x`, **refuse and emit a remediation message** ("camera on USB2 — move to native Type‑C port"). Prevents silent degraded operation.
- **Confidence:** High (cheap, high‑value).

### C‑6 — Remove the global power band‑aids; scope keep‑awake by serial only if measured
- Delete/disable `99-dgx-spark-performance.rules` global USB/PCI PM disable + `pcie_aspm=off`. Keep `99-vigil-realsense-power.rules` **disabled** unless §5 proves an autosuspend‑induced drop; if needed, scope by **serial `404543020690`**, not `idProduct`.
- **Confidence:** High (reduces blast radius; camera healthy at `power/control=auto`).

### C‑7 — Keep the good library work; correct stale docs
- Retain pkg‑config libdir fix, isolated prefix, C++20 stop‑lifecycle, ARM64 wrapper fixes. Update `realsense.TODO.md`: firmware `5.13.0.55` (not 5.17.0.10), CUDA path (verify 13.0 vs 13.2), reframe instability as the primary defect (now fixed).
- **Confidence:** High.

### C‑8 — Production capture hardening (from the TODO, currently aspirational)
- Bounded‑wait worker thread, reconnect backoff, drain‑to‑newest with queue depth 1–2, avoid default `hardware_reset()`, USBGuard allow **by serial**. For ROS: evaluate `realsense2_camera` with `serial_no`, `usb_port_id`, `wait_for_device_timeout`, `reconnect_timeout`.
- **Confidence:** Medium‑High (good practice; reduces recoverable‑fault impact).

### C‑9 — Resolve ROS/SDK version skew
- `ros-jazzy-librealsense2` is **2.57.7** while the SDK build is **2.58.1**. Align to avoid ABI/behaviour drift in any ROS pipeline.
- **Confidence:** Medium.

## Dimension C — Camera firmware

### C‑10 — Controlled firmware update 5.13.0.55 → 5.16.0.1 (conservative) or 5.17.0.9 (latest)
- Device is on **5.13.0.55** (~2021‑era). Latest D435 is **5.17.0.9**; **5.16.0.1** is the well‑proven prior stable. Newer FW improves USB enumeration robustness and UVC compliance.
- **Pre‑conditions (mandatory):** stable **USB‑3** link, mains power, no streaming clients, `rs-fw-update -l` confirms the device, take a backup of current behaviour first. **Never flash over a marginal/USB‑2 link.**
- **Confidence:** Medium — beneficial but carries flashing risk; do it only after C‑1 is locked in.

## Dimension D — Ubuntu / Spark compatibility

### C‑11 — RSUSB backend is the correct choice on this kernel; don't patch the kernel
- The GB10 build uses `FORCE_RSUSB_BACKEND=ON` (libuvc/libusb), which avoids needing RealSense kernel patches on the `6.17.0-1021-nvidia` kernel. Correct for a non‑standard NVIDIA kernel. Do **not** attempt the DKMS kernel‑patch path here.
- **Confidence:** High.

### C‑12 — Accept the benign UVC quirks as noise; don't "fix" them
- `UVC non compliance … 981ae2 … error -5` and `Unknown video format 00000050-…` are expected D4xx‑over‑uvcvideo artifacts (Intel vendor controls / Z16‑Y8 formats). Filter them out of dashboards; they are not actionable.
- **Confidence:** High.

### C‑13 — Keep kernel/JetPack/BSP updated; re‑validate USB after each
- Spark USB‑C interoperability has been an area of active NVIDIA fixes. After any BSP/kernel update, **re‑run the healthcheck harness** as a regression gate.
- **Confidence:** Medium.

## Dimension E — xHCI controller robustness under load (NEW, from §7)

### C‑14 ✅ Stay within the safe streaming envelope
- Single or **dual** high-rate streams are rock-solid. **Avoid 3 concurrent streams at 848×480@60** — it killed the GB10 xHCI (`HC died`). For 3-stream needs, drop resolution/fps (≤640×360@60) and validate with the guarded ramp first.
- **Confidence:** High (empirically the failing config; safe configs proven in §9).

### C‑15 — Cap aggregate USB bandwidth; reduce IR/streams in production
- The common case (aligned depth+color) is supported. Add IR only after the guarded ramp confirms a ceiling. Consider per-stream rate limits and avoid simultaneous max-res on all three sensors.
- **Confidence:** Medium-High.

### C‑16 — Tripwire-guarded testing only
- Never run unguarded high-bandwidth stress again: `rs-gb10-stress.sh` now aborts within 0.5 s of the first `-110`/`Host halt`/`Not enough bandwidth` signature, before controller death. Use it (with `--usbmon`) to map the ceiling and capture the protocol signature.
- **Confidence:** High.

### C‑17 — Escalate the controller-death to NVIDIA; track BSP/kernel fixes
- This reproduces the documented Jetson/Orin "RealSense crashes USB controller" bug on GB10. File with NVIDIA (DGX Spark forum) with the `controller-death-journal.txt` evidence; re-validate after each BSP/kernel update. Possible mitigations to test post-update: confine camera to one root port, avoid load spikes, kernel xHCI quirks.
- **Confidence:** Medium (depends on NVIDIA).

### C‑18 — Recovery: reboot (software rebind insufficient)
- A dead `NVDA8000:00` needs a reboot; `xhci-hcd` unbind/rebind returns `-110`. Document this in the operator SOP so a wedged controller isn't mistaken for a dead camera.
- **Confidence:** High (verified).

## Ranked shortlist

| Rank | ID | Solution | Status |
|---|---|---|---|
| 1 | C‑1 | Direct Type‑C + eMarker cable | ✅ done/confirmed |
| 2 | C‑6 | Remove global PM band‑aids; serial‑scope keep‑awake | recommended |
| 3 | C‑5 | USB2 hard‑fail gate in capture path | recommended |
| 4 | C‑4 | Raise `usbfs_memory_mb` (config file) | recommended |
| 5 | C‑3 | Attach‑after‑boot + verify speed SOP | recommended |
| 6 | C‑10 | Controlled firmware update (after link locked) | planned |
| 7 | C‑7/C‑8/C‑9/C‑13 | Doc fixes, capture hardening, version align, regression gate | backlog |
