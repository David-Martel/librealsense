> ## ⚠️ CORRECTION (2026-06-03, incident #3) — this assessment's hopeful verdict was REFUTED on hardware.
> This doc's static prediction was "V4L2 likely REDUCES (not eliminates) the death." **HIL testing refuted the
> reduction:** on the V4L2 backend, `single_depth` and `single_color` 848×480@60 streamed cleanly, but
> **`dual_D+C 848×480@60` SETUP killed controller `NVDA8000:02`** — a kernel `uvcvideo` control query timed out
> (`usb 6-1: Failed to query … UVC control … -110`) → Stop-Endpoint → `HC died` (09:02:07). Forensics:
> `../20260603-0902-xhci-controller-death-3-V4L2/`. **Conclusion: V4L2 does NOT prevent the death.** The crash is
> reproduced on BOTH backends and on a control failure from ANY cause → it is purely the NVIDIA GB10 xHCI P6/H7
> defect (Stop-Endpoint cannot complete, no recovery). The code-level stop-path *differences* below remain accurate
> (V4L2 IS gentler — one serialized STREAMOFF vs a libusb clear_halt storm), but "gentler" did not translate to
> "survives dual." Revised empirical safe envelope: **single high-rate stream only**; dual 848×480@60 is unsafe.
> The only fix for the crash is NVIDIA BSP/firmware (file all three death dirs with NVIDIA).

# 8c. V4L2 (native uvcvideo) Backend vs RSUSB — GB10 xHCI Controller-Death Assessment

> **Static code review only.** GB10 xHCI controller is currently DEAD (no USB); no streaming/enumeration/hardware access was performed. Pure source + config + installed-binary symbol analysis. Sources: librealsense `~/dev/repos/librealsense` @ `81f35eb81`; installed builds under `/opt/vigil/opt/`. Companion to `80-multistream-root-cause-deepdive.md` (mechanism) and `85-librealsense-patch-specs.md` (RSUSB patches).
>
> **Question:** Would the **RSUSB-disabled (native V4L2/uvcvideo) backend** avoid or reduce the GB10 `xhci_plat_hcd` controller death reproduced twice on `spark-3066`?

---

## VERDICT

**V4L2 likely REDUCES (does not eliminate) the controller-death risk.**

It removes BOTH triggers that preceded both deaths — the dual-driver contention (H5) and the userspace `clear_halt`/`cancel_transfer` storm during stop/teardown — by routing all streaming and UVC control through kernel uvcvideo, which serializes per-device operations. It does **not** fix the proximate fatal cause (H7/P6): the GB10 xHCI Stop-Endpoint-command timeout → `HC died` lives in NVIDIA silicon/driver, and the kernel uvcvideo `VIDIOC_STREAMOFF` path **still issues a Stop-Endpoint command**. So V4L2 lowers the *probability of reaching* H7, but cannot *guarantee* H7 never fires.

**Net:** V4L2 is the correct production backend for this platform (matches Intel's official multistream guidance and the fork's `realsense-rsusb-metal` recommendation), and is strictly safer than RSUSB on the GB10 — but "safer," not "immune." The only crash-proof fix remains P6 (NVIDIA BSP/firmware).

---

## 1. The stop/teardown path — the death's proximate cause

The death is a **Stop-Endpoint command timeout** (`xHCI host not responding to stop endpoint command` → `Host halt failed, -110` → `HC died`). The two backends reach the kernel Stop-Endpoint command very differently.

### RSUSB stop (userspace storm, multi-threaded)

`src/uvc/uvc-streamer.cpp::stop()` (`:203-236`), invoked **per stream** from each stream's own libusb event thread:

| Step | Call | Maps to kernel |
|---|---|---|
| `:210` | `_request_callback->cancel()` | — |
| `:218-219` | `for r in _requests: messenger->cancel_request(r)` → `libusb_cancel_transfer` (`messenger-libusb.cpp:98-109`) | URB cancel → **Stop-Endpoint** |
| `:225` | `messenger->reset_endpoint(...)` → `libusb_clear_halt` (`messenger-libusb.cpp:23-34`) | CLEAR_FEATURE(HALT) control transfer + **Stop-Endpoint** |

Plus a **stall watchdog** (`uvc-streamer.cpp:102-...`) that fires its own `reset_endpoint`/Stop-Endpoint asynchronously under load. With 3 concurrent streams on 2+ event threads, this is a **burst of independent Stop-Endpoint commands** at a controller that is already control-path-starved — exactly the condition that detonated H7 in event #1. (This is *why* RSUSB needs patch P4 / task #6 to serialize the storm.)

### V4L2 stop (single kernel-serialized ioctl per node)

`src/linux/backend-v4l2.cpp`:

- `v4l_uvc_device::stop_data_capture()` (`:1677-1690`): `signal_stop()` (writes the stop pipe, `:1730-1738`) → `_thread->join()` → **`streamoff()`**.
- `streamoff()` (`:2520-2523`) → `stream_off(_fd, ...)` (`:588-592`) = **one** `xioctl(fd, VIDIOC_STREAMOFF, &type)`.

`VIDIOC_STREAMOFF` is a **single ioctl that the kernel uvcvideo driver executes under its per-device lock**, internally tearing down URBs and issuing the Stop-Endpoint command **once, serialized**, with the driver's own ordering — not a userspace race of `clear_halt`/`cancel_transfer` from multiple threads. There is **no** `libusb_clear_halt`, **no** `libusb_cancel_transfer`, and **no** userspace stall-watchdog reset in the V4L2 path:

```
grep -c 'libusb_claim_interface|libusb_clear_halt|libusb_cancel_transfer|libusb_detach' src/linux/backend-v4l2.cpp  →  0
```

**Difference, stated precisely:** RSUSB issues a *multi-threaded userspace clear_halt/cancel storm* (`uvc-streamer.cpp:218-225` + watchdog) that maps onto a burst of Stop-Endpoint commands; V4L2 issues **one coordinated, kernel-serialized `VIDIOC_STREAMOFF` per node** (`backend-v4l2.cpp:2520-2523` → `:588-592`). V4L2 thus does **not** issue the racy multi-threaded clear_halt that detonates H7. It still issues *a* Stop-Endpoint (so H7 can still fire), but it presents the controller with the gentlest, most-serialized form of that command rather than the storm.

---

## 2. Dual-driver contention (H5) — removed under V4L2

Under RSUSB, kernel uvcvideo stays bound to the interfaces librealsense does **not** claim (lazy/scoped auto-detach, `handle-libusb.h:57`, `device-libusb.cpp:35-43`), so uvcvideo keeps issuing competing UVC control queries → the `-110` storm logged in both deaths. Two drivers fight over one device.

Under V4L2, **uvcvideo is the sole driver of the streaming interfaces**. The V4L2 backend opens an ordinary `/dev/video*` character device:

- `map_device_descriptor()` (`:2551-2555`): `_fd = open(_name.c_str(), O_RDWR | O_NONBLOCK, 0)` — a plain V4L2 node open, **no** `libusb_claim_interface`, **no** auto-detach dance.
- Device discovery is via uvcvideo's sysfs nodes: the enumerator walks `/sys/class/video4linux/.../videoX` → `/dev/videoY` (`:612, :672, :859-943`), i.e. it *consumes* the nodes uvcvideo created. It cannot work unless uvcvideo is bound.

Code-level proof there is no competing libusb claim under V4L2:

```
grep -rn 'libusb_claim_interface|libusb_detach_kernel|set_auto_detach' src/linux/  →  NONE FOUND
grep -n  '#include .*libusb'  src/linux/backend-v4l2.cpp                            →  NONE
```

**Scope caveat (important — do not overclaim "no libusb").** The V4L2 build *does still link* libusb (`src/CMakeLists.txt:16-18` unconditionally includes `src/libusb/`; `src/linux/CMakeLists.txt:11-12` `target_link_libraries(... usb)`). But libusb in the V4L2 build is used **only** for the firmware-update/DFU path (`src/fw-update/…`), **never** for streaming or UVC/XU control. The streaming and control path in `backend-v4l2.cpp` issues zero libusb calls. So the accurate statement is: *under V4L2, libusb is linked but not used for streaming or control; uvcvideo solely drives the streaming interfaces, so there is no second driver and no `-110` storm from a competing libusb claim.* This removes the trigger (H5) that preceded **both** deaths.

---

## 3. Control transfers — kernel-serialized ioctls, less `-110`-prone

Under RSUSB, control queries (including the `set_power_state(D0)` that failed in event #2) go through userspace `libusb_control_transfer` (`messenger-libusb.cpp:36-...`) racing the kernel uvcvideo's own queries on the same EP0. Event #2's chain is visible in code: `rs_uvc_device::set_power_state(D0)` (`uvc-device.cpp:210-244`) calls `_usb_device->open()` (libusb claim + `listen_to_interrupts`); on failure it **throws** `"failed to set power state"` — and the failed-resolve teardown produced the `-110` control storm that preceded the halt.

Under V4L2, every control goes through **`UVCIOC_CTRL_QUERY` ioctls** that the kernel serializes per-device:

- `set_xu` / `get_xu` (`backend-v4l2.cpp:2170-2200`): `xioctl(_fd, UVCIOC_CTRL_QUERY, &q)`.
- `get_xu_range` and the PU/CT control accessors (`:2220, :2236, :2247, :2258, :2269`): all `ioctl(_fd, UVCIOC_CTRL_QUERY, …)`.
- Critically, **`set_power_state` under V4L2 is just fd open/close** (`:2154-2168`): `D0` = `map_device_descriptor()` (open the node), `D3` = `close()` + `unmap_device_descriptor()`. **No control transfer, no libusb claim, no `listen_to_interrupts`** — so the exact "failed to set power state" → control-storm chain from event #2 *cannot occur the same way* on V4L2. A failed open returns an error from a single ioctl/open, not a userspace control-transfer storm racing the kernel.

**Conclusion:** the V4L2 control path is materially **less likely** to produce the `-110` starvation storm, because (a) there is no second driver competing on EP0, and (b) control is one serialized ioctl rather than userspace transfers racing the kernel.

---

## 4. The honest limit (H7 / P6 residual risk)

V4L2 **cannot guarantee** the controller will never die:

- The Stop-Endpoint-command timeout → `Host halt failed -110` → `HC died` is inside the GB10 `xhci_plat_hcd` (ACPI `NVDA8000:PNP0D15`) — NVIDIA silicon/driver, with **no recovery path** on this platform (no Jetson-style `tegra_xhci_hcd_reinit`; see `80-…:8.5`). Userspace backend choice cannot touch the xHCI command ring.
- **Kernel uvcvideo itself issues a Stop-Endpoint command on `VIDIOC_STREAMOFF`.** If the GB10 controller is fragile enough to die on *any* Stop-Endpoint under load (not only on a multi-threaded storm), V4L2 streamoff can still trip it. V4L2 makes that command **single, serialized, and kernel-ordered** instead of a userspace burst — strictly gentler, but still a Stop-Endpoint.

**Precise claim:** V4L2 **reduces the trigger likelihood** (removes H5 + the H6/`-110` storm surface + the multi-threaded clear_halt burst) and is the safest available backend on GB10, but it **only reduces, never eliminates** the H7/P6 residual risk. The crash-proof fix is still P6 (NVIDIA BSP/firmware hardening of Stop-Endpoint/halt recovery).

---

## 5. Empirical backend separation (installed binaries)

Symbol inspection of the two installed builds confirms they are genuinely different backends (not a config toggle):

| Build | `v4l_uvc_device` symbols | libusb/`rs_uvc_device` symbols |
|---|---|---|
| `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10-v4l2/lib/librealsense2.so.2.58` | **98** (incl. `v4l_uvc_device::streamoff`, `::stream_on`, `::set_power_state`) | (libusb present for DFU only) |
| `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib/librealsense2.so.2.58` (RSUSB) | **0** | **75** (`rs_uvc_device`, `messenger_libusb`) |

The V4L2 build's streaming/control path is `v4l_uvc_device`; the RSUSB build's is `rs_uvc_device`/libusb. Confirmed.

---

## 6. V4L2 build & setup requirements (test-harness checklist)

### 6.1 Build flag & compiled backend

- **CMake flag:** `FORCE_RSUSB_BACKEND=OFF` (default OFF on Linux — `CMake/lrs_options.cmake:35`). The selection logic:
  - `CMake/unix_config.cmake:107-111`: `if(FORCE_RSUSB_BACKEND) BACKEND=RS2_USE_LIBUVC_BACKEND else() BACKEND=RS2_USE_V4L2_BACKEND`.
- **Backend macro:** `RS2_USE_V4L2_BACKEND` (NOT `RS2_USE_LIBUVC_BACKEND`).
- **Compiled source:** `src/CMakeLists.txt:30-32` → `include(.../linux/CMakeLists.txt)` compiles `src/linux/backend-v4l2.cpp` (+ `backend-hid.cpp`). The RSUSB-only sources (`src/uvc/`, `src/libuvc/`) are **not** compiled (`src/CMakeLists.txt:42-50` gate them behind `RS2_USE_LIBUVC_BACKEND`).
- **Note:** `src/libusb/` is still linked under V4L2 (`src/CMakeLists.txt:16-18`) but used only by `src/fw-update/` (DFU). It does **not** drive streaming or control.

### 6.2 uvcvideo MUST be bound (opposite of RSUSB)

- V4L2 **requires** kernel uvcvideo bound to the RealSense streaming interfaces — it opens the `/dev/video*` nodes uvcvideo creates (`backend-v4l2.cpp:2553` open; enumerator walks `/sys/class/video4linux/` `:612,:672`).
- **Do NOT** run `realsense-rsusb-metal unbind-uvcvideo` (P5) for a V4L2 deployment — that removes `/dev/video*` and breaks V4L2 entirely (the P5 caveat in `85-…` warns of exactly this). Any `modprobe.d` blacklist of `uvcvideo` must be **absent**.

### 6.3 udev rules — the V4L2-specific gap

- **What the shipped rule provides:** `config/99-realsense-libusb.rules` is entirely `SUBSYSTEMS=="usb"` (MODE 0666, GROUP plugdev on the *usb* node) + `KERNEL=="iio*"`/`hid_sensor*` (motion module). It grants the **usbfs** node access that RSUSB needs.
- **What it does NOT provide (V4L2 gap):** there is **no** `KERNEL=="video*"` / `SUBSYSTEM=="video4linux"` rule for USB D400 in that file (the only `video4linux` rule, `99-realsense-d4xx-mipi-dfu.rules:6`, is MIPI/IPU6 D457 `DS5 mux` only — not USB D400). **Therefore V4L2 requires the streaming user to have `/dev/video*` access by another means** — membership in the `video` group, an added `SUBSYSTEM=="video4linux", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0b07", GROUP="video", MODE="0660"` rule, or running as root. **This is a genuine V4L2-only requirement the shipped RealSense udev rule does not satisfy.** Add it to the harness setup.
- **Permissions vs node creation — correction to the task framing:** udev does **not** create the metadata or streaming nodes. **uvcvideo creates both the streaming and the metadata `/dev/video*` nodes on bind**; udev only sets ownership/permissions on them. Metadata-node *existence* depends on kernel uvcvideo metadata support, not on a udev rule.
- HW-timestamp/metadata: the V4L2 backend pairs each streaming node with a **separate metadata `/dev/video*` node** for HW timestamps (`match_video_with_metadata_nodes` `:1351-1393`; `V4L2_CAP_META_CAPTURE`; `set_md_from_video_node` `:471`). That metadata node must exist and be readable (same `/dev/video*` permission requirement as the streaming node).

### 6.4 Kernel uvcvideo — requirement to VERIFY (cannot test; HW dead)

- The fork chose RSUSB explicitly **to avoid RealSense kernel patches** (`realsense.TODO.md:54`: *"RSUSB backend for newer kernel compatibility without RealSense kernel patches"*). V4L2 re-introduces a dependency on kernel uvcvideo behavior that RSUSB sidesteps.
- **Streaming** on stock uvcvideo is expected to work. **HW timestamps / D400 metadata** historically required the **realsense uvcvideo patch** (the kernel-4.16 metadata-layout handling at `backend-v4l2.cpp:337` is evidence the metadata path is kernel-version-sensitive).
- **Action (do NOT assert; verify post-reboot):** confirm stock kernel 6.17 uvcvideo exposes the D400 **metadata capture node** (`V4L2_CAP_META_CAPTURE`) and correct metadata. If it does not, expect **degraded/SW timestamps**, not a streaming failure. This is the one requirement RSUSB avoids and V4L2 must satisfy.

### 6.5 usbfs_memory_mb — IRRELEVANT to V4L2

- `usbfs_memory_mb` bounds **usbfs** (libusb/RSUSB) pinned URB allocations. V4L2 uses **kernel V4L2 buffers** (`VIDIOC_REQBUFS` MMAP/USERPTR — `req_io_buff` `:594-608`, `allocate_io_buffers` `:2532-2549`), **not** usbfs. The P2/P3 RSUSB tunings (`usb_request_count`, `usbfs_memory_mb`) **do not apply** to V4L2. Do not set them for a V4L2 run; they have no effect on the V4L2 buffer pipeline.

---

## 7. Why V4L2 reduces the trigger — one-line summary per hypothesis

| Hypothesis (from `80-…`) | RSUSB | V4L2 | Effect |
|---|---|---|---|
| **H5** dual-driver contention | uvcvideo + libusb both on device (`handle-libusb.h:57`) | uvcvideo sole driver; no libusb claim (`backend-v4l2.cpp` grep = 0) | **Removed** |
| **H6** `-110` control storm | userspace `control_transfer` racing kernel on EP0 | serialized `UVCIOC_CTRL_QUERY` ioctls; `set_power_state` = open/close | **Reduced** |
| **H7** Stop-Endpoint storm | multi-threaded `clear_halt`+`cancel` burst + watchdog (`uvc-streamer.cpp:218-225`) | one kernel-serialized `VIDIOC_STREAMOFF` per node (`:2520-2523`) | **Reduced (gentler), not removed** |
| **H7/P6** controller silicon fault | NVIDIA `xhci_plat_hcd`, no recovery | same controller; uvcvideo still issues Stop-Endpoint | **NOT fixable in either backend** |
| **H8** usbfs under-provisioning | shallow usbfs URBs, `usbfs_memory_mb` tight | kernel V4L2 buffers; usbfs irrelevant | **N/A (sidestepped)** |

P4 (serialized stop, task #6) is the RSUSB patch that tries to *approximate* what V4L2 gets **for free** from the kernel — which is the structural reason V4L2 reduces the trigger.

---

## 8. Setup checklist (copy for the harness)

- [ ] Build with `FORCE_RSUSB_BACKEND=OFF` → backend `RS2_USE_V4L2_BACKEND`, compiles `src/linux/backend-v4l2.cpp`. (Use the existing `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10-v4l2/` build — verified 98 `v4l_uvc_device` symbols.)
- [ ] uvcvideo **bound** to the D400 interfaces (do NOT unbind/blacklist; no `realsense-rsusb-metal unbind-uvcvideo`).
- [ ] Install `99-realsense-libusb.rules` (usb-node perms) **AND** add a `/dev/video*` access rule (`SUBSYSTEM=="video4linux"`, GROUP `video`/`plugdev`) **or** add the user to the `video` group — the shipped rule does not grant `/dev/video*`.
- [ ] Confirm streaming **and** metadata `/dev/video*` nodes exist after uvcvideo bind (`v4l2-ctl --list-devices`); pairing logic needs the metadata node for HW timestamps.
- [ ] Verify (post-reboot, HW alive) stock kernel-6.17 uvcvideo exposes D400 `V4L2_CAP_META_CAPTURE`; if absent, expect degraded timestamps (not a streaming failure).
- [ ] Do **NOT** set `usbfs_memory_mb` / `usb_request_count` for V4L2 — kernel V4L2 buffers, not usbfs; no effect.
- [ ] Re-run the §8.7 guarded ramp (`rs-gb10-stress.sh --usbmon`) on V4L2 and compare the `-110`/Stop-Endpoint/HC-died deltas against the RSUSB baseline. **Predicted:** `-110` storm absent or much reduced; 3-stream ceiling higher; H7 still possible at the extreme (residual NVIDIA risk).

---

## 9. Sources (file:line)

- **Stop path:** RSUSB `src/uvc/uvc-streamer.cpp:203-236` (stop), `:102-...` (watchdog); `src/libusb/messenger-libusb.cpp:23-34` (`reset_endpoint`=`libusb_clear_halt`), `:98-109` (`cancel_request`=`libusb_cancel_transfer`). V4L2 `src/linux/backend-v4l2.cpp:1677-1690` (`stop_data_capture`), `:2520-2523` (`streamoff`), `:588-592` (`stream_off`=`VIDIOC_STREAMOFF`).
- **Dual-driver / claim:** RSUSB `src/libusb/handle-libusb.h:57`, `src/libusb/device-libusb.cpp:35-43`. V4L2 `src/linux/backend-v4l2.cpp:2551-2555` (plain `open`), `:612,:672,:859-943` (sysfs `/dev/video` enumeration); `grep libusb_claim/clear_halt/cancel src/linux/ = 0`.
- **Control / power:** RSUSB `src/uvc/uvc-device.cpp:210-244` (`set_power_state`→`open`/claim→throw "failed to set power state"); `src/libusb/messenger-libusb.cpp:36` (`control_transfer`). V4L2 `src/linux/backend-v4l2.cpp:2154-2168` (`set_power_state`=open/close), `:2170-2200,:2220-2269` (`UVCIOC_CTRL_QUERY` ioctls).
- **Build flags:** `CMake/lrs_options.cmake:35`, `CMake/unix_config.cmake:107-111`, `src/CMakeLists.txt:16-18,30-32,42-50`, `src/linux/CMakeLists.txt:11-12`.
- **udev / metadata:** `config/99-realsense-libusb.rules` (usb+iio only, no `/dev/video*`), `config/99-realsense-d4xx-mipi-dfu.rules:6` (MIPI-only video4linux rule), `CMake/embedd_udev_rules.cmake`; metadata pairing `src/linux/backend-v4l2.cpp:1351-1393,:471,:337`; buffers `:594-608,:2532-2549`.
- **Kernel-patch rationale:** `realsense.TODO.md:54` (RSUSB chosen to avoid RealSense kernel patches), `:222-223` (`unbind-uvcvideo`/`rebind-uvcvideo`).
- **Binaries:** `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10-v4l2/lib/librealsense2.so.2.58` (98 `v4l_uvc_device` syms) vs `…-gb10/lib/librealsense2.so.2.58` (0 v4l, 75 libusb syms).
- **Prior analysis:** `80-multistream-root-cause-deepdive.md` (H5–H8, §8.5 GB10 no-recovery), `85-librealsense-patch-specs.md` (P1–P6).
