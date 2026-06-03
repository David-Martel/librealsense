# 8. Multistream Controller-Death — Deep Root-Cause Analysis

> Investigation per systematic-debugging discipline. **Live reproduction is blocked** (controller dead, reboot pending), so this is an evidence + code + kernel/firmware analysis producing a *confirmed mechanism* and a *discriminating post-reboot test plan*. Inputs: captured kernel journals (`../20260602-2159-xhci-controller-death/`), librealsense source (`~/dev/repos/librealsense` @ `81f35eb81`), GB10 kernel state, and sourced research (NVIDIA forums, LKML, librealsense issues).

## 8.1 The question

Is the multistream controller crash a **race condition, memory leak, or bad allocation** — and can it be fixed **permanently in librealsense**?

**Answer up front:** It is **none of those three in the classic sense.** It is a **dual-driver contention + control-path starvation** condition that drives a **host-controller error-recovery failure** (Stop Endpoint command timeout) on a GB10 xHCI that has **no recovery path**. librealsense can make it *much less likely to trigger* (and degrade gracefully instead of fatally), but **cannot guarantee a fix** — the controller death itself is NVIDIA xHCI driver/firmware territory.

## 8.2 The exact failure mechanism (now fully grounded)

```
D435 over RSUSB (libusb) backend, 3 concurrent streams:
  • depth sensor  = Media Interface 0  → Z16 depth + Y8 infrared (ONE streaming iface,
                    its OWN usb_context + OWN libusb event thread)
  • color sensor  = Media Interface 3  → BGR8 (separate usb_context + event thread)
  Opened SERIALLY by the pipeline (depth claimed first, color second).            [resolver.h:137-159]

(1) DETACH IS LAZY + SCOPED. The backend uses libusb_set_auto_detach_kernel_driver(true)  [handle-libusb.h:57]
    and detaches uvcvideo ONLY from the VC+VS interfaces it claims                 [device-libusb.cpp:35-43]
    → kernel uvcvideo STAYS BOUND to the device's other UVC interfaces and keeps
      issuing UVC control queries.  (Log: uvcvideo probing 2-1:1.3.)

(2) CONTROL-PATH SATURATION under load. With 3 saturating 848×480@60 BULK streams,    [request-libusb.cpp:37]
    each only 2 URBs deep (~814 KB buffers)                  [uvc-device.h:42, uvc-streamer.cpp:26,141]
    on 2 event threads, PLUS uvcvideo's competing control queries, PLUS librealsense's
    own XU/power control transfers during setup, the device control endpoint (EP0)
    and the xHCI command path are overwhelmed.

(3) CONTROL TIMEOUTS. Control transfers start failing with -110 (ETIMEDOUT):
      21:59:16  uvcvideo: Failed to query UVC control 20 ... -110     ← kernel side
      librealsense: "failed to set power state"                       ← userspace side

(4) ENDPOINT WEDGE → STOP ENDPOINT. A bulk endpoint stalls; the stop/cancel path
    (libusb_cancel_transfer → reset_endpoint/libusb_clear_halt)        [uvc-streamer.cpp:176-209,
    maps onto the kernel issuing a Stop Endpoint command.               messenger-libusb.cpp:23-34]

(5) CONTROLLER DEATH. The GB10 xHCI fails to complete the Stop Endpoint command:
      21:59:32  xHCI host not responding to stop endpoint command
      21:59:32  Host halt failed, -110  →  HC died; cleaning up
    No recovery (see 8.5) → reboot required.
```

## 8.3 Hypotheses — confirmed / refuted (with evidence)

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| H1 | **Memory leak** (RSS growth exhausts buffers) | **REFUTED** | Failure on the *first* heavy config within seconds of start; no growth period. |
| H2 | **librealsense-internal race** in multi-stream start | **REFUTED** | Start is **serialized**: serial `for` loops `resolver.h:137-159`, per-sensor `_configure_lock` `uvc-sensor.cpp:99,381`, per-device `_action_dispatcher` `uvc-device.cpp:173`, URB submit in `invoke_and_wait` `uvc-streamer.cpp:152-173`. No internal claim-time race. |
| H3 | **xHCI endpoint/slot exhaustion** (ran out of endpoints) | **REFUTED** | Controller is xHCI 1.2 (`hcc params 0x01844f91`), hundreds of slots; 3 bulk endpoints is trivial. `Not enough bandwidth` line appears *after* `HC died` (uvcvideo re-probing a dead device) — a consequence, not the trigger. |
| H4 | **USB autosuspend** caused power-state churn | **REFUTED** | `usbcore.autosuspend=-1` already on kernel cmdline → autosuspend globally disabled. |
| H5 | **Dual-driver contention** (kernel uvcvideo + userspace libusb both driving the device) | **CONFIRMED (contributing trigger)** | Detach is lazy+scoped `handle-libusb.h:57`,`device-libusb.cpp:35-43`; uvcvideo logged probing `2-1:1.3` with `-110` *during* the librealsense start. Present (benignly) in all runs; becomes fatal only under (H6) load. |
| H6 | **Control-path starvation under 3-stream concurrent load** | **CONFIRMED (primary trigger)** | **Discriminator:** `uvcvideo -110` control timeouts appear in the 3-stream crash (6×) and are **absent (0×) from every 1–2 stream run** that streamed faultlessly for minutes. Shallow 2-URB buffering `uvc-device.h:42` makes the pipeline thin under three 814 KB/URB streams. |
| H7 | **GB10 xHCI cannot recover a stuck endpoint** (Stop Endpoint timeout → HC died) | **CONFIRMED (proximate fatal cause)** | `xHCI host not responding to stop endpoint command` → `Host halt failed -110` → `HC died`. This is `usb_hc_died()` in `xhci-hcd`; a host-controller fault, not a device fault. Matches documented Jetson/Orin RealSense signature. |
| H8 | **Under-provisioned transfer pool / usbfs** amplifies fragility | **CONFIRMED (contributing)** | `usb_request_count=2` (shallow) and librealsense never sets/checks `usbfs_memory_mb` (default 16 MB). Not a malloc bug — an under-provisioned allocation that thins the pipeline. |

**Classification vs the user's question:** closest to a **timing/resource-starvation condition** (H5+H6+H8) that detonates an **unrecoverable host-controller error-handling path** (H7). Not a leak (H1), not an internal race (H2), not a slot-allocation overflow (H3).

## 8.4 Why it's load-gated (the discriminator, restated)

| Run | Streams | `uvcvideo -110` | stop-endpoint / HC died | Outcome |
|---|---|---|---|---|
| healthcheck ×4 | 1 | **0** | 0 | ✅ |
| sustained + stress | 1–2 | **0** | 0 | ✅ 0 faults, 2341 framesets |
| **HEAVY matrix entry** | **3 @848×480@60** | **6** | **yes** | ❌ HC died |

uvcvideo is bound to non-streamed interfaces in *all* runs, so contention (H5) is constant — but control transfers only start *timing out* at 3 concurrent streams (H6). The combination is what tips H7.

## 8.5 Why the controller can't recover on GB10 (worse than Jetson)

- GB10's xHCI is driven by **generic `xhci_plat_hcd` via ACPI** (`acpi:NVDA8000:PNP0D15`), **not** the Jetson `xhci-tegra.c` driver.
- The Jetson auto-recovery (`en_hcd_reinit` → `tegra_xhci_hcd_reinit()` on XUSB mailbox interrupt) lives in `xhci-tegra.c` — **not in use here**. Confirmed: no `en_hcd_reinit` param exposed; `xhci_hcd` exposes only `link_quirk`/`quirks` (no Stop-Endpoint-timeout tunable).
- Therefore once `HC died`, there is **no firmware re-init path** → driver unbind/rebind returns `-110` → **reboot only.** (And NVIDIA *removed* even the Jetson auto-recovery in JetPack 6.2 — forum #369419.)

## 8.6 Can it be fixed permanently in librealsense? — layered answer

### Addressable IN librealsense / configuration (reduces or removes the *trigger*)

1. **Eager, full-device uvcvideo detach before any claim** — *the single highest-value change.* Today detach is lazy + scoped (`handle-libusb.h:57`, `device-libusb.cpp:35-43`), leaving uvcvideo on `…:1.3` to issue the competing control queries the logs show. Detaching uvcvideo from **all** of the device's UVC interfaces up front removes H5 entirely. Achievable via:
   - backend change (explicit `libusb_detach_kernel_driver` on every interface, or unbind at open), **or**
   - operationally now: a udev rule / `modprobe.d` that prevents `uvcvideo` from binding RealSense interfaces, or `realsense-rsusb-metal unbind-uvcvideo`, or USBGuard authorization (the fork's `realsense.TODO.md:24-25` already flags this).
2. **Deeper URB buffering** — raise `usb_request_count` above 2 (`uvc-device.h:42`) for D400 high-bandwidth multistream, **and** raise `usbcore.usbfs_memory_mb` (16→1000) — librealsense sets neither. Thickens the pipeline (H8).
3. **Gentler stop/cancel** — serialize the cancel→`clear_halt` storm across streams (`uvc-streamer.cpp:176-209`) and add a settle delay between MI0 (depth) and MI3 (color) starts; reduces the control-command burst that detonates H7.
4. **Cap concurrent high-bandwidth streams / lower resolution for 3-stream** — the proven-safe envelope (§7.4) until the ceiling is mapped.

### NOT fixable in librealsense (the *crash* itself)

- The Stop Endpoint timeout → `Host halt failed -110` → `HC died` is inside the GB10 `xhci_plat_hcd`/silicon/firmware. Userspace cannot drive the xHCI command ring or reset the host. A robust controller resets the wedged endpoint and continues; this one dies. **Permanent fix for the crash needs an NVIDIA BSP/firmware update** (the Jetson lineage of this exact bug was only ever fixed via JetPack firmware, never userspace).

**Net:** librealsense changes (esp. #1 eager detach + #2 deeper buffers) can very plausibly convert this from a **fatal controller death** into, at worst, a **graceful per-stream failure** — i.e. make multistream *not trigger* H7. But "permanently fixed" in the strict sense (controller never dies under any load) is **not achievable in librealsense alone**; it requires NVIDIA to harden the GB10 xHCI Stop-Endpoint/halt recovery.

## 8.7 Post-reboot discriminating test plan (to confirm the mechanism & validate fixes)

Run each via the **tripwire-guarded** `rs-gb10-stress.sh --usbmon` (aborts before `HC died`; captures usbmon URBs + xhci ftrace at the moment control transfers start failing — the protocol-level proof we lack). Order:

| Test | Question it answers | Predicted result if mechanism is correct |
|---|---|---|
| T1: guarded ramp, **as-is** | Find the exact ceiling; capture usbmon/xhci at first `-110` | Light/dual pass; 3-stream@848×480@60 trips the tripwire with `-110` URBs visible before any halt |
| T2: 3 streams at **lower res** (≤640×360@60) | Is it stream-*count* or aggregate *bandwidth*? | If bandwidth: lower-res 3-stream passes. If count/contention: still trips. |
| T3: **blacklist uvcvideo** for RealSense (eager detach proxy), then 3-stream@848×480@60 | Does removing dual-driver contention (H5) prevent the `-110` storm? | If H5 material: the `-110` storm shrinks/vanishes; ceiling rises |
| T4: raise `usbfs_memory_mb`=1000 (+ if patched, `usb_request_count`>2), then 3-stream | Does deeper buffering (H8) help? | Fewer timeouts / higher safe ceiling |
| T5: stagger start (settle delay MI0→MI3) | Does spacing the control burst help? | Fewer setup-time `-110`s |

usbmon error-completion URBs (errno histogram) from T1–T3 will show whether the first failure is a STALL (-32/EPIPE), proto error (-71), or timeout (-110) at the protocol layer — distinguishing device-firmware vs host-controller origin definitively.

## 8.8 Concrete recommendations

- **librealsense (upstream-able):** implement eager full-device uvcvideo detach for the RSUSB backend; expose/raise `usb_request_count` for D400 multistream; emit a warning if `usbfs_memory_mb` is low. File these + the controller-death repro with Intel and NVIDIA.
- **Operational now (no code):** native Type-C + eMarker; **≤2 concurrent high-rate streams**; prefer aligned depth+color; if 3 needed, lower resolution and validate with the guarded ramp; consider the **native V4L2/uvcvideo backend** for production multistream (Intel's official guidance vs RSUSB).
- **Platform:** track NVIDIA DGX Spark BSP/firmware updates for xHCI Stop-Endpoint robustness; re-run the guarded ramp after each; escalate `../20260602-2159-xhci-controller-death/` to NVIDIA.

## 8.9 Sources
- Captured: `../20260602-2159-xhci-controller-death/{controller-death-journal.txt,DISCRIMINATOR.txt}`.
- Code: librealsense `@81f35eb81` — `handle-libusb.h:57`, `device-libusb.cpp:35-43`, `resolver.h:137-159`, `uvc-sensor.cpp:99,381`, `uvc-device.{h:42,cpp:173}`, `uvc-streamer.cpp:{26,141,150-209}`, `request-libusb.cpp:37`, `messenger-libusb.cpp:23-34`, `context-libusb.cpp:81-98`.
- Research: NVIDIA forums #371594 / #342718 / #293436 / #369419 (Tegra+RealSense xHCI death; JetPack 6.2 removed auto-recovery); librealsense #6395 ("Failed to set power state" multicam, sequential-startup workaround), #9157 / Jetson docs (RSUSB not recommended for multi-cam); LKML xhci stop-endpoint/`usb_hc_died`.
