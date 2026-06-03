# 6. Low-Level USB Debugging (usbmon / xhci ftrace / packet capture)

To catch RealSense USB errors at the protocol layer — not just the kernel‑log symptom — the toolkit adds in‑kernel **usbmon** capture, **xhci‑hcd ftrace**, and optional **pcap** sniffing, with automatic fault correlation.

## 6.1 Tools

| Tool | Role |
|---|---|
| **`bin/rs-gb10-usbmon-capture.sh`** | start/stop/forensics for usbmon URB trace + error filter + xhci ftrace (+ pcap if `tshark`/`dumpcap` present) |
| **`bin/rs-gb10-stress.sh --usbmon`** | runs the resolution/fps stress matrix with usbmon capture; auto‑runs forensics if any fault fires |
| **`bin/rs-gb10-stress-matrix.py`** | the `pyrealsense2` 60 fps + multi‑resolution stress driver (concurrent depth+color+IR) |

Prerequisites on this host (verified 2026‑06‑02): passwordless `sudo`, `debugfs` mounted, `usbmon` module available (`modprobe usbmon` → nodes `/sys/kernel/debug/usb/usbmon/{0..12}{u,t,s}`), xhci‑hcd ftrace events present. `tshark`/`dumpcap` are **absent** (pcap optional; usbmon text is the primary sniffer).

## 6.2 What "catch errors effectively" means here

usbmon streams every URB on a bus. A **completion** event (`C`) carries a status field; on success it is `0`/`-`, on error it is a **negative errno**. The capture runs a live filter that keeps only error completions, so the high‑signal file stays tiny even under 300 fps load:

```awk
$3=="C" && $5 ~ /^-[0-9]/   # URB callback with negative status = a real protocol error
```

Errno decode (the ones that matter for RealSense):

| errno | meaning | typical RealSense cause |
|---|---|---|
| **‑32** EPIPE | endpoint **STALL** | the `Failed to set UVC probe control : -32` we saw on Bus 5 — bandwidth/negotiation stall |
| **‑71** EPROTO | bus protocol error | bad signal integrity / cable / hub |
| **‑75** EOVERFLOW | babble / oversized packet | marginal SuperSpeed link |
| **‑84** EILSEQ | CRC / framing error | cable / connector |
| **‑110** ETIMEDOUT | no response | link drop / device wedge |
| **‑121** EREMOTEIO | short transfer | endpoint/firmware issue |
| **‑19** ENODEV / **‑108** ESHUTDOWN | device gone | the disconnect itself |

> Note the recalibration from §3: the `index:768 … error: Success` libusb warning and the `USBDEVFS_CLEAR_HALT` line are **not** usbmon error completions — usbmon confirms they carry status 0. That is exactly why a protocol sniffer is worth having: it distinguishes benign control chatter from a real STALL.

The **xhci‑hcd ftrace** snapshot adds the *controller's* view — `xhci_handle_command`, stop‑endpoint, ring halts — which catches faults that never reach a URB completion (e.g. a controller that wedges, as in the Jetson "USB controller crash" reports).

## 6.3 Usage

```bash
# Stress the interface across resolutions at 60 fps WITH protocol capture + auto-forensics:
~/realsense-gb10-validation/bin/rs-gb10-stress.sh --duration 10 --usbmon          # visible
~/realsense-gb10-validation/bin/rs-gb10-stress.sh --duration 30 --usbmon --headless

# Manual capture around a flaky operation:
~/realsense-gb10-validation/bin/rs-gb10-usbmon-capture.sh start 2 /tmp/cap 60
#   ... reproduce the fault (run a profiler/stream) ...
~/realsense-gb10-validation/bin/rs-gb10-usbmon-capture.sh stop /tmp/cap
~/realsense-gb10-validation/bin/rs-gb10-usbmon-capture.sh forensics /tmp/cap <journal-delta.txt>

# Optional richer capture if you install wireshark CLI (asks first — package install):
#   sudo apt-get install -y tshark   # then pcap is captured automatically
```

## 6.4 Outputs (per run, under `usbmon/`)
- `usbmon-bus<N>-full.txt` — size‑capped (~25 MB) full URB trace for forensic context.
- `usbmon-bus<N>-errors.txt` — **only** error‑completion URBs (the high‑signal file).
- `usbmon-error-summary.txt` — error count + errno histogram + xhci error lines.
- `xhci-ftrace.txt` — controller event trace snapshot.
- `usb-forensics.txt` — produced only when a fault fires: correlates journal faults ⇄ usbmon error URBs ⇄ xhci trace.
- `usbmon-bus<N>.pcap` — only if `tshark`/`dumpcap` is installed.

All are added to the run's `SHA256SUMS` manifest for independent verification.

## 6.5 Interpreting a future failure (playbook)
1. `usbmon-error-summary.txt` empty + `fault-tally.txt` all‑zero → link healthy; investigate higher layers (app, frame handling).
2. Errno histogram dominated by **‑32/‑71/‑84** → **physical layer**: reseat/replace the eMarker cable, move to a different rear Type‑C port, remove any hub.
3. **‑110/‑19/‑108** + journal `USB disconnect` → link dropping: power/cable/port; re‑check negotiated speed (`/sys/.../speed`).
4. xhci trace shows stop‑endpoint/halt storms with no URB errors → **controller‑level** wedge: distribute load across separate root ports (NVIDIA's guidance), consider kernel/BSP update, re‑validate.
5. Always re‑run after any change and diff the errno histogram.
