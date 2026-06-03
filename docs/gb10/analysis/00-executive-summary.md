# Intel RealSense on DGX Spark GB10 — Diagnosis, Critical Review & Remediation

**Host:** `spark-3066` — NVIDIA **DGX Spark / GB10** (aarch64), Ubuntu 24.04.4 LTS, kernel `6.17.0-1021-nvidia`, CUDA 13.0, driver 580.159.03
**Camera:** Intel RealSense **D435** (`8086:0b07`), ASIC/serial `404543020690`, firmware **5.13.0.55**
**Date:** 2026-06-02
**Author:** automated analysis (Claude Code) for david.martel@auricleinc.com

---

## TL;DR

The "persistent disconnect / non‑USB‑3 enumeration" problem is **a USB topology / link‑training fault, not a camera, firmware, or librealsense defect.** It was reproduced, root‑caused, and **resolved by a physical change made mid‑investigation**:

| | **Before (failing)** | **After (working)** |
|---|---|---|
| Connection | USB‑C **dock/hub chain** (Cable Matters + Fresco Logic + Element + ASMedia ASM2364 hubs) | **Rear‑panel native USB‑C port + eMarker cable** |
| Bus / path | Bus 5 → `5-1.4.4` | Bus 2 → `2-1` |
| Negotiated speed | **480M (USB 2.0 high‑speed)**, `bcdUSB 2.10` | **5000M (USB 3.2 SuperSpeed)**, `bcdUSB 3.20` |
| Serial read by kernel | `SerialNumber=0` (**descriptor read failing**) | `SerialNumber=404543020690` ✅ |
| `Usb Type Descriptor` (SDK) | n/a (2.1 fallback) | **3.2** ✅ |
| Power budget | `bMaxPower=0mA` (not granted) | `bMaxPower=720mA` ✅ |
| Profiles available | bandwidth‑starved subset, broken `probe control -32` | **Full set:** 1280×720@30 depth, 1080p color ✅ |
| Kernel log signature | re‑enumeration loop, `UVC probe control -32`, `USB disconnect`, `xhci fail`, `clear_halt` | clean enumeration |

**Root cause (issue #1 — enumeration/disconnect):** the camera was reaching the host only through a chain of **USB‑2.0‑only hubs** inside a USB‑C dock. A USB 3.x device behind a USB‑2.0 hub *can only ever enumerate as USB 2.0*, and the marginal multi‑hop signal path produced descriptor‑read failures, control‑transfer errors, and re‑enumeration/disconnect cycles. This is consistent with a **documented DGX Spark USB‑C quirk** (devices connected before power‑up, or through certain bridge chipsets, fall back to 480M) and with NVIDIA's own guidance to attach RealSense devices to a **direct Type‑C root port**, not a hub.

> ## ⚠️ SECOND, SEPARATE FINDING (discovered during stress testing) — see `70-controller-crash-finding.md`
> On the *healthy* native USB‑3 port, driving **3 concurrent streams (depth+color+IR at 848×480@60)** caused the **GB10 xHCI host controller to die** (`HC died; cleaning up`, `Not enough bandwidth for altsetting 0`). This is the documented "RealSense crashes the USB controller" Jetson/Orin failure mode, now reproduced on DGX Spark GB10. A driver rebind could **not** recover it — **a reboot is required.** Normal single/dual‑stream use is rock‑solid (0 faults, §9); only very high aggregate 3‑stream bandwidth triggers it. The stress tooling has been hardened with a kernel tripwire that aborts *before* controller death. **Current state: controller `NVDA8000:00` is down; the camera's USB‑C port needs a reboot.** Other USB (input devices on bus 5) is unaffected.

## What this analysis delivers

1. **`10-environment-and-sdk-inventory.md`** — exactly what RealSense software/dev effort exists on this machine and how it maps to the David‑Martel GitHub forks.
2. **`20-customization-critical-review.md`** — critical review of the GB10 fork customizations: what is genuinely good, what is a band‑aid, what is inaccurate.
3. **`30-log-analysis-and-root-cause.md`** — the kernel/SDK log evidence, decoded, with the before/after proof.
4. **`40-candidate-solutions.md`** — candidate solutions across all four requested dimensions (platform USB controller, librealsense+local customizations, camera firmware, Ubuntu/Spark compatibility), ranked.
5. **`50-remediation-plan.md`** — a staged, test‑driven remediation plan with explicit pass/fail gates.
6. **`../bin/rs-gb10-healthcheck.sh`** — an **idempotent, re‑runnable, independently‑verifiable** health‑check harness that emits a structured PASS/FAIL report + raw artifacts + topology snapshot + journal fault delta.

## Headline recommendations (ranked)

1. **Operate the D435 only on a direct Spark Type‑C root port with an eMarker USB‑C cable** (done — keep it that way). Treat any USB‑2.0 enumeration as a hard failure, not a degraded mode. *(Confirmed working.)*
2. **Stop relying on the global power‑management band‑aids** (`autosuspend=-1` + `power/control=on` across *all* USB/PCI, `pcie_aspm=off`). They masked symptoms on a bad link and are unnecessary on a good one (camera now runs healthy at `power/control=auto`). Scope any keep‑awake rule to the camera by serial.
3. **Raise `usbcore.usbfs_memory_mb`** from the default 16 MB (RealSense streams need more headroom) — config‑file based, not a global kernel band‑aid.
4. **Plan a controlled firmware update** 5.13.0.55 → 5.16.0.1 (conservative) or 5.17.0.9 (latest), but only *after* the link is proven robust, never over a marginal link.
5. **Adopt the healthcheck harness as the regression gate** before/after any change.

See `50-remediation-plan.md` for the full staged plan and `40-candidate-solutions.md` for rationale.
