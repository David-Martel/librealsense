# Where to submit the GB10 xHCI bug report

**This is a DGX Spark / GB10 enterprise platform + BSP/firmware defect. It is NOT a Jetson
issue — do not file it on the Jetson forum, and do not lead with the generic NVIDIA DevZone
bug portal, which is not the channel for entitled enterprise hardware.**

## Recommended: file two places, cross-referenced

### 1. PRIMARY — NVIDIA Enterprise Support Portal ticket (system of record)

This is the escalatable channel that can reach DGX Spark BSP/firmware engineering and track
a hardware/platform defect to resolution.

- **Open a ticket (login):** https://nvid.nvidia.com/login
- **Enterprise Support Portal / live chat:** https://enterprise-support.nvidia.com/s/
- **View existing tickets:** https://nvid.nvidia.com/login

**Account / entitlement needed:** an NVIDIA Application Hub account whose **Entitlement
Certificate lists `NVIDIA AI Enterprise — DGX Spark`** specifically. Other NVIDIA AI
Enterprise entitlements do **not** automatically include DGX Spark support — confirm the
certificate names this exact product before opening the case. (Register via the link in the
NVIDIA Entitlement Certificate instructions → NVIDIA Application Hub.)

- **Fallback if you do not hold the AI Enterprise — DGX Spark entitlement:** the DGX Spark
  **hardware** support path: open a ticket at http://nvidia.custhelp.com/app/ask
  (live chat http://nvidia.custhelp.com/app/chat/chat_launch/ ,
  tickets http://nvidia.custhelp.com/app/account/overview/ ). A controller that dies until
  reboot is a legitimate hardware/platform robustness case for this path.

### 2. PARALLEL — DGX Spark / GB10 developer forum (reach BSP/firmware engineers directly)

- **Forum:** https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/719

Post the report here and **reference the enterprise case number** for cross-linking. This
forum is monitored by NVIDIA DGX Spark/GB10 engineers, and **there is precedent for USB
firmware fixes on this exact platform** — NVIDIA already ships DGX Spark firmware capsules
addressing USB Power Delivery controller stability — so a USB-controller BSP/firmware fix is
an established outcome path here.

## What to attach

- Kernel evidence: the verbatim `-110` → Stop-Endpoint → `HC died` blocks from
  `NVIDIA-BUG-REPORT.md`, plus full `journalctl -k` for each of the three incidents.
- The three forensic summaries: the `SUMMARY.txt` / `DISCRIMINATOR.txt` pairs under
  `docs/gb10/forensics/20260602-2159-…`, `…/20260603-0804-…-2`, `…/20260603-0902-…-3-V4L2`.
- Platform/topology dumps: `uname -a`, `lsusb -t`, `lspci`, and the port→controller ACPI
  mapping `readlink -f /sys/bus/usb/devices/usb*/..` (shows which `NVDA8000:0X` each port is).
- If NVIDIA provides a DGX Spark log-collection bundle / field-diagnostics tool
  (e.g. `nvidia-bug-report.sh`), attach its output as well; otherwise the above kernel
  journals and platform dumps are sufficient to start.

---

### Channels deliberately NOT used

- **Jetson / Jetson Orin developer forum** — wrong product. DGX Spark / GB10 is an
  enterprise platform, not a Jetson module; its USB stack is the ACPI `xhci_plat_hcd`, not
  Jetson's `xhci-tegra`.
- **Generic NVIDIA DevZone / driver bug portal** — not the right system of record for
  entitled enterprise hardware; use the Enterprise Support Portal ticket instead.

Sources:
- https://docs.nvidia.com/dgx/dgx-spark/support.html
- https://forums.developer.nvidia.com/c/accelerated-computing/dgx-spark-gb10/719
- https://www.nvidia.com/en-us/support/dgx-spark/
- https://enterprise-support.nvidia.com/s/
