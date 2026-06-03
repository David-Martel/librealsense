# 7. CRITICAL FINDING — Load-Induced xHCI Controller Death (GB10)

> Discovered live during stress testing on 2026-06-02 22:00. This is a **second, distinct
> failure mode** from the USB-2.0 topology issue (§3), and it is more severe.

## 7.1 What happened

Driving **three concurrent streams — depth (Z16) + color (BGR8) + infrared (Y8), all at 848×480@60** — on the *healthy native USB-3 port* (Bus 2) caused the NVIDIA GB10 xHCI host controller to **die**, taking the camera's entire USB controller offline. Escalation (from `controller-death-journal.txt`):

```
22:59:16 usb 2-1: Failed to query (GET_DEF) UVC control 20 ... -110 (ETIMEDOUT)   ← control-transfer timeouts
22:59:22 usb 2-1: Failed to query (SET_CUR) UVC control 5 ...  -110
22:59:32 usb 2-1: Not enough bandwidth for altsetting 0                            ← bandwidth exhaustion
22:59:32 xhci-hcd NVDA8000:00: Host halt failed, -110
22:59:32 xhci-hcd NVDA8000:00: xHCI host controller not responding, assume dead    ← CONTROLLER DEAD
22:59:32 xhci-hcd NVDA8000:00: HC died; cleaning up
22:59:32 usb 2-1: USB disconnect, device number 2                                  ← camera gone
```

librealsense reported `failed to set power state` / `Failed to resolve the request` on that first config, then every subsequent acquisition returned **`No device connected`**.

## 7.2 Severity & blast radius

- Controller **`NVDA8000:00`** (owns USB bus 1 + bus 2) is **dead**. The camera's rear USB-C port is offline.
- **Other USB is unaffected:** buses 3–12 healthy; the dock on bus 5 (keyboard, mouse, 2.5G NIC, audio, storage) keeps working. The machine remains fully usable.
- **Software recovery failed.** Unbind/rebind of the platform xHCI driver returned `Host halt failed, -110` → `can't setup: -110` → `probe ... failed with error -110`. A driver rebind cannot power-cycle the Tegra/GB10 xHCI IP block.
- **Recovery requires a reboot** (or a deeper platform/power-domain reset not exposed to the driver).

## 7.3 Interpretation

This matches the documented **"RealSense camera can cause the USB controller to crash"** reports on NVIDIA Jetson/Orin platforms (same `-110` → host-halt → controller-death signature), now reproduced on **DGX Spark GB10**. It is a **platform xHCI robustness limit under heavy isochronous/bulk RealSense load**, not a cable/topology fault:

- The native USB-3 port is **fine for normal use** — §9 proved single- and dual-stream profiles (incl. 1280×720 depth, depth90-ir at 71 fps, 640×480@30/60) ran with **0 faults** over minutes and 15 start/stop cycles.
- The failure appears only at **high aggregate bandwidth with 3 concurrent streams at 848×480@60**, where the kernel logged **"Not enough bandwidth for altsetting 0"** immediately before the controller halted.

### Confounder notes (honest scoping)
- Two things were true on the failing config: very high aggregate USB bandwidth, and `power/control=auto` (the camera was not pinned awake — initial trigger was `failed to set power state`). The keep-awake band-aid criticized in §2 *might* have prevented the initial `failed to set power state` trigger — but the **controller death under bandwidth exhaustion is a separate, deeper fault** that keep-awake would not prevent. Do not conflate them.
- The on-screen render was throttled to 30 Hz and runs in user space; it is not plausibly the cause of a kernel xHCI death.
- We have **not** yet isolated whether the trigger is the 3-stream count, the 848×480@60 bandwidth specifically, or a transient. The safe envelope below is conservative pending the guarded re-test.

## 7.4 Safe operating envelope (provisional, from evidence so far)

| Config | Result |
|---|---|
| Single stream (depth/IR/color) up to 1280×720@30 / 848×480@90 | ✅ rock-solid, 0 faults |
| Dual stream (e.g. depth 848×480@90 + IR, or depth+color 640×480@60) | ✅ validated 0 faults |
| **Triple stream depth+color+IR @ 848×480@60** | ❌ **killed the controller** |
| Triple stream at lower resolution (≤640×360@60) | ⚠️ untested post-fix — guarded ramp will determine |

**Production guidance:** stay at ≤2 concurrent high-rate streams, or reduce resolution/fps for 3-stream configs. Treat the aligned depth+color (the common case) as the supported path; add IR only after the guarded ramp confirms a safe ceiling.

## 7.5 Tooling hardened in response

`bin/rs-gb10-stress-matrix.py` now:
- **Ramps lightest → heaviest**, so the safe ceiling is found before dangerous configs.
- Runs an **in-loop kernel tripwire** (polls `journalctl -k` every 0.5 s) that **aborts the moment** `-110 (exp`, `Not enough bandwidth`, `Host halt`, `failed to set power`, `cannot enable`, or `over-current` appears — *before* escalation to `HC died`.
- **Halts the whole sweep** on the first danger signature or `No device connected`, with a 2 s inter-entry cooldown.

`bin/rs-gb10-stress.sh --usbmon` captures usbmon + xhci ftrace so that, when the tripwire fires, the **protocol-level URBs and controller trace around the fault are preserved** for root-causing (the usbmon root-check bug that suppressed capture on the first attempt is fixed).

## 7.6 Non-reboot recovery attempts (all failed) → reboot required

Per request, deeper software recovery was attempted before rebooting. **None works on this platform:**

| Lever | Result |
|---|---|
| `xhci-hcd` unbind→settle→**rebind** (×2) | ❌ `Host halt failed, -110` / `can't setup: -110` — the driver reset path itself times out on the wedged HC |
| **Module reload** | ❌ `xhci-hcd` is **built-in**, not a loadable module |
| **reset/remove/rescan** sysfs node | ❌ not exposed on `NVDA8000:00` |
| **Runtime-PM power cycle** | ❌ `power/runtime_status=unsupported` |
| **Suspend/resume** | ❌ only `s2idle` available (`mem_sleep=[s2idle]`, no deep S3); s2idle keeps device power and won't power-cycle the controller IP |

**Conclusion:** the GB10 xHCI reset/power domain is firmware/BPMP-managed and not exposed to userspace. Once `HC died`, only a **full reboot** re-runs controller init. Reboot evidence + attempts saved under `../20260602-2159-xhci-controller-death/`.

## 7.7 Required next step

**Reboot** to restore controller `NVDA8000:00` and the camera's USB-C port. Then re-run the *guarded* stress ramp (`rs-gb10-stress.sh --duration 10 --usbmon`) to map the exact safe ceiling and capture the protocol-level signature if it recurs — this time without risking an unguarded controller death (the tripwire aborts before `HC died`).
