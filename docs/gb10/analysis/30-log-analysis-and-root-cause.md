# 3. Log Analysis & Root‑Cause

## 3.1 The failing topology (Bus 5, USB 2.0)

When connected through the USB‑C **dock**, the camera sat at the bottom of a USB‑2.0‑only hub chain:

```
usb5 root (NVDA8000:02, bcdUSB 2.00, 480M)
└─ 5-1   Fresco Logic USB2.0 Hub (1d5c:5801, 480M)      ← dock upstream hub is USB-2.0 ONLY
   ├─ 5-1.1 Cable Matters (2bd5:42a8)  Class=Billboard  ← USB-C alt-mode billboard = SS not negotiated
   └─ 5-1.4 Element USB 2.0 Hub (2188:0034, 480M)
      └─ 5-1.4.4  Intel RealSense D435 (8086:0b07, 480M, bcdUSB 2.10, bMaxPower 0mA, SerialNumber=0)
```

**Physical law of the bug:** a USB 3.x device whose upstream path includes *any* USB‑2.0 hub has **no SuperSpeed lanes to the host** — it can only enumerate at 480M. The D435's SuperSpeed differential pairs terminate at the first 2.0 hub. No software/firmware/driver change can produce USB‑3 through a 2.0 hub. The presence of a **Billboard** device (`2bd5:42a8`) is the tell‑tale of a USB‑C connection where the expected SuperSpeed/alt‑mode was **not** established.

### Kernel signatures (Bus 5), decoded
| Log line | Meaning |
|---|---|
| `usb 5-1.4.4: new high-speed USB device` | enumerated as **USB 2.0**, never SuperSpeed |
| `New USB device strings: … SerialNumber=0` | the kernel **could not read the serial descriptor** — marginal control path |
| `uvcvideo 5-1.4.4:1.2: Failed to set UVC probe control : -32 (exp. 48)` | **‑32 = EPIPE/broken pipe** during stream‑format negotiation: classic bandwidth/link failure |
| repeated `Found UVC 1.50 device …` (dozens, 19:10–19:15) | **re‑enumeration loop** — device kept re‑probing |
| `usb 5-1.4.4: USB disconnect, device number 16` then `…number 17` | hard **disconnect / re‑attach** cycle |
| no‑camera run counters: `xhci_failures=9, clear_halt=6, disconnects=14` | controller‑level instability on the dock path |

### SDK signatures (Bus 5)
- `messenger-libusb control_transfer returned error, index: 768, error: Success, number: 0` — flooded (322 in one run). **See §3.3: this one is *noise*, present on healthy links too.**
- `…-rsusb-unbound` experiment made it *worse* (330 warnings) → unbinding uvcvideo cannot fix a 2.0‑link problem.

## 3.2 The working topology (Bus 2, USB 3.2) — after rear‑port + eMarker cable

```
usb2 root (SuperSpeed, 20000M/x2)
└─ 2-1  Intel RealSense D435 (8086:0b07)
        speed=5000M  bcdUSB=3.20  bMaxPower=720mA  rx_lanes=1 tx_lanes=1
        SerialNumber=404543020690 ✅   Usb Type Descriptor (SDK)=3.2 ✅
```

`dmesg`: `usb 2-1: new SuperSpeed USB device number 2 using xhci-hcd` → `SerialNumber: 404543020690`. Full profile set now offered (1280×720@30 Z16 depth, 1920×1080 RGB color, 848×480@90, etc.).

### Sustained streaming validation (this analysis, Bus 2)
`rs-gb10-profiler`, headless, 60 s sustained per profile:

| Profile | `usb3` | framesets | fps (target) | depth gaps | timeouts | **failures** | stop |
|---|---|---|---|---|---|---|---|
| hd15 (1280×720) | yes | 892 | **14.55** (15) | 0 | 2 | **0** | clean |
| vga30 (640×480) | yes | 1783 | **29.12** (30) | 0 | 4 | **0** | clean |
| stress (all × 3 × 5 s) | — | see `fault-tally.txt` | — | — | — | see run | — |

Kernel journal fault delta across the entire run is recorded in
`…/20260602-214043-bus2-usb3-stability/fault-tally.txt` (see `90-validation-results.md` for the captured numbers). Near‑target frame rates with **zero stream failures** and clean stops is the robustness proof the enumeration check alone could not provide.

## 3.3 Signal recalibration — which messages actually matter

A crucial finding from running the profiler on the *healthy* USB‑3 link: the
`messenger-libusb … control_transfer returned error, index: 768, error: Success, number: 0`
warnings **still appear**. Therefore:

- ❌ **Not** a link‑health signal. `index 768` (0x0300) is a string/descriptor control transfer that short‑returns 0 bytes while libusb reports success; it is benign RSUSB backend chatter on D400 over libuvc. Earlier runs that "counted control_transfer errors" were counting noise.
- ✅ **The real health signals are kernel‑side:** `USB disconnect`, **host‑stack‑initiated** `xhci … fail` / `reset device` / `cannot enable` / `Not enough bandwidth` / `over-current` / `device descriptor read` error, `Failed to set UVC probe control -32/-71/-110`, `SerialNumber=0`, and the negotiated **speed** (`/sys/.../speed`, `bcdUSB`).
- ⚠️ **`clear_halt` needs care.** The line `usb 2-1: Process NNN (rs-gb10-profile) called USBDEVFS_CLEAR_HALT for active endpoint 0x82` is **librealsense itself** clearing its own endpoint halt via libusb during stream setup — **normal RSUSB behaviour, logged even on a perfectly healthy USB‑3 link.** It is *not* a link fault and must be excluded from fault counts. (The earlier "no‑camera" run's `kernel_clear_halt=6` likely counted these benign ioctls; the unambiguous bad signals in that run were `disconnects=14` and `xhci_failures=9`, which are *not* filtered and remain damning.)
- ⚠️ `UVC non compliance: permanently disabling control 981ae2 … error -5` appears on **both** links — a benign D4xx UVC‑extension quirk (kernel uvcvideo doesn't grok an Intel vendor control). Not the disconnect cause.

The harness (`bin/rs-gb10-healthcheck.sh`) therefore counts only host‑stack faults and **excludes** the benign `USBDEVFS_CLEAR_HALT for active endpoint` and `981ae2 -5` noise, so its `kernel.usb_faults` check is meaningful. This was empirically validated: a visible streaming run initially mis‑scored 1 fault from exactly this benign `USBDEVFS_CLEAR_HALT` line, then scored 0 after the exclusion — with the camera streaming cleanly throughout (fps≈27, usb3, no disconnect).

The healthcheck harness (`bin/rs-gb10-healthcheck.sh`) scores on the kernel‑side signals and the negotiated speed, and explicitly **ignores** the `index:768` noise — so its PASS/FAIL is meaningful.

## 3.4 Root‑cause statement

> **The D435's disconnect / USB‑2.0‑enumeration problem was caused by the camera being attached through a USB‑C dock whose internal hubs are USB‑2.0‑only, placing the camera on a 480M path with no SuperSpeed lanes and a marginal control channel.** This produced descriptor‑read failures (`SerialNumber=0`), stream‑negotiation EPIPE (`-32`), re‑enumeration loops, and hard disconnects, plus controller‑level `xhci`/`clear_halt` faults. Moving the camera to a **direct Spark rear‑panel Type‑C root port with an eMarker‑equipped USB‑C cable** placed it on a SuperSpeed root (Bus 2, 5000M, USB 3.2), restoring full profiles and stable streaming.

### Contributing/secondary factors (not the primary cause)
- **DGX Spark USB‑C negotiation quirk** (documented): some bridge chipsets / "connected‑before‑power‑up" cases fall back to 480M even on a good port; connecting after boot or replug fixes it. Reinforces "always verify the negotiated speed."
- **`usbcore.usbfs_memory_mb=16` (default)** — fine at 480M, but tight for high‑bandwidth USB‑3 multi‑stream; raise it (§4).
- **Old firmware 5.13.0.55** — not the cause, but worth updating on a stable link.
- The global power band‑aids did **not** help (camera now healthy at `power/control=auto`).

## 3.5 Caveat — two variables changed together

The fix changed **port** (dock→rear native) *and* **cable** (→eMarker) simultaneously, so this run does not isolate which mattered. The physics says the **USB‑2.0 hub chain** is sufficient on its own to force 480M, so the port change is almost certainly the dominant factor; the eMarker cable is good practice for clean SuperSpeed signalling. The remediation plan (§5) includes a controlled A/B to attribute cleanly if needed. No `USB disconnect` for the camera has been observed on the Bus‑2 native path so far.
