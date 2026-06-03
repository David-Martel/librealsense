# 5. Remediation Plan — staged, test‑driven, verifiable

Each stage has an **action**, a **gate** (objective pass/fail), and an **evidence artifact**. The harness `bin/rs-gb10-healthcheck.sh` is the canonical gate runner; it is idempotent (re‑runnable, fresh timestamped report each run) and independently verifiable (raw artifacts + a machine‑readable `result.json` + SHA‑256 manifest).

> **Golden rule:** never advance a stage whose gate is RED. Never flash firmware or change config over a link that isn't already GREEN at Stage 0.

---

## Stage 0 — Lock in the physical fix *(DONE / confirmed)*
- **Action:** D435 on rear‑panel native USB‑C + eMarker cable (C‑1). Label the cable+port; document "do not use the dock for the camera."
- **Gate G0:** `rs-gb10-healthcheck.sh` → `usb_speed >= 5000M`, `usb_descriptor == 3.2`, serial read non‑zero, sustained 60 s hd15 **and** vga30 with `failures=0`, and **0** kernel USB faults in the journal delta.
- **Evidence:** `20260602-214043-bus2-usb3-stability/` (already PASS — see §9).
- **Status:** ✅ GREEN.

## Stage 1 — Remove the band‑aids, re‑baseline
- **Action (C‑6):**
  1. Disable the global PM rule: `sudo mv /etc/udev/rules.d/99-dgx-spark-performance.rules{,.disabled}`.
  2. Disable the broad keep‑awake: `sudo mv /etc/udev/rules.d/99-vigil-realsense-power.rules{,.disabled}`.
  3. Leave `pcie_aspm=off`/`usbcore.autosuspend=-1` out of GRUB on the next kernel‑cmdline edit (don't reboot just for this; remove at next maintenance window).
  4. `sudo udevadm control --reload && sudo udevadm trigger`.
- **Gate G1:** re‑run the harness. Must remain GREEN (camera already runs at `power/control=auto`). If — and only if — a measured autosuspend‑induced drop appears, re‑introduce a **serial‑scoped** keep‑awake:
  `ACTION=="add", SUBSYSTEM=="usb", ATTR{serial}=="404543020690", ATTR{power/control}="on"`.
- **Evidence:** new harness report dir; diff fault counts vs Stage 0.
- **Rollback:** restore the `.disabled` files.

## Stage 2 — Bandwidth headroom (config‑file, not band‑aid)
- **Action (C‑4):** raise usbfs memory via a versioned config file:
  `echo 'options usbcore usbfs_memory_mb=1000' | sudo tee /etc/modprobe.d/99-realsense-usbfs.conf`
  Runtime (no reboot): `echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb`.
- **Gate G2:** harness records `usbfs_memory_mb >= 256`; sustained hd15 60 s still `failures=0`. (Headroom insurance; expect no regression.)
- **Evidence:** harness report shows the new value; streaming unchanged/better.

## Stage 3 — Enforce "USB2 = hard fail" in the capture path (C‑5)
- **Action:** in the production capture entrypoint (and any ROS launch), read `Usb Type Descriptor`; if `< 3.0`, refuse to start and print: *"D435 on USB2 (path X). Move to a rear native Type‑C port + eMarker cable; do not use the dock."* The profiler already models this (`--allow-usb2` opt‑in only).
- **Gate G3:** test the negative path — connect via the dock (USB2) and confirm the capture service **refuses** with the remediation message; connect native and confirm it starts.
- **Evidence:** two harness runs (dock=FAIL‑fast, native=PASS) captured side by side.

## Stage 4 — Controlled firmware update (C‑10) — *only after Stages 0–2 GREEN*
- **Pre‑conditions:** link GREEN at USB‑3, mains power, **no** streaming clients, `rs-fw-update -l` lists exactly the D435.
- **Action:** update `5.13.0.55` → **5.16.0.1** (conservative) first; re‑validate; optionally → **5.17.0.9** (latest). Download from the official RealSense D400 firmware releases page; verify checksum.
  `realsense-gb10-env rs-fw-update -s 404543020690 -f Signed_Image_UVC_5_16_0_1.bin`
- **Gate G4:** post‑flash harness GREEN; `device.firmware` reflects the new version; sustained streaming `failures=0`; **fewer/zero** `981ae2 -5` / `-32` UVC events than baseline.
- **Rollback:** RealSense FW is re‑flashable; keep the prior image; if regression, reflash 5.13.0.55.
- **Risk:** flashing is the only irreversible‑ish step → gated behind a proven link.

## Stage 5 — Capture‑path resilience & version alignment (C‑8/C‑9)
- **Action:** bounded‑wait worker thread, reconnect backoff, drain‑to‑newest queue depth 1–2, avoid default `hardware_reset()`, USBGuard allow **by serial**. Align `ros-jazzy-librealsense2` (2.57.7) with the SDK (2.58.1) or pin a single source of truth. For ROS, set `serial_no`, `usb_port_id`, `wait_for_device_timeout`, `reconnect_timeout`.
- **Gate G5:** a 30‑minute soak (`rs-gb10-healthcheck.sh --soak 1800`) with `failures=0`, `0` kernel faults, and any induced unplug/replug auto‑recovering within the backoff window.
- **Evidence:** soak report + induced‑fault recovery log.

## Stage 4.5 — Map the safe streaming envelope (NEW, C‑14/C‑16) — *requires reboot first*
- **Pre-condition:** reboot to restore controller `NVDA8000:00` (a dead controller cannot be tested).
- **Action:** run the **guarded** ramp `rs-gb10-stress.sh --duration 10 --usbmon` (lightest→heaviest, kernel tripwire armed). Let it find the ceiling; it aborts before any controller death and preserves usbmon/xhci forensics if a fault fires.
- **Gate G4.5:** a documented max-safe concurrent-stream/resolution/fps set with `0` controller faults; the failing `HEAVY_60fps_848x480_D+C+IR` either passes (transient) or is confirmed off-limits with protocol-level evidence.
- **Action (prod):** pin the production capture config at or below the safe ceiling (C‑15); default to aligned depth+color, IR only if within budget.
- **Escalation:** file the controller-death with NVIDIA (C‑17) using `20260602-2159-xhci-controller-death/`.

## Stage 6 — Docs, SOP & regression gate (C‑7/C‑13)
- **Action:** correct `realsense.TODO.md` (firmware 5.13.0.55, CUDA path, instability framing). Write a 1‑page operator SOP: *native Type‑C only, eMarker cable, attach after boot, run the harness, USB2 = stop.* Wire `rs-gb10-healthcheck.sh` into CI/maintenance to run after every kernel/BSP/SDK/firmware change.
- **Gate G6:** harness GREEN committed as the accepted baseline; SOP reviewed.

---

## Stage → Candidate → Gate matrix

| Stage | Candidates | Gate | Reversible? |
|---|---|---|---|
| 0 | C‑1 | G0 (speed+stream+0 faults) | yes (replug) |
| 1 | C‑6 | G1 (no regression w/o band‑aids) | yes |
| 2 | C‑4 | G2 (usbfs headroom) | yes |
| 3 | C‑5 | G3 (USB2 fail‑fast) | yes |
| 4 | C‑10 | G4 (FW + clean stream) | re‑flashable |
| 5 | C‑8/C‑9 | G5 (30‑min soak + recovery) | yes |
| 6 | C‑7/C‑13 | G6 (baseline + SOP) | n/a |

## Independently‑verifiable reporting

Every gate run emits (under a fresh timestamped dir):
- `result.json` — structured PASS/FAIL per check with thresholds and measured values.
- `report.md` — human summary.
- raw artifacts: `topology.txt`, `rs-enumerate.txt`, `profiler-*.log`, `journal-delta.txt`, `fault-tally.txt`.
- `SHA256SUMS` — manifest so a third party can confirm artifacts are unaltered.

A reviewer re‑runs `rs-gb10-healthcheck.sh` and compares `result.json`; identical PASS on the same hardware = reproducible. The check is **idempotent** (safe to run repeatedly, no persistent side effects) and **deterministic in its verdict** against fixed thresholds, even though streaming metrics vary run‑to‑run.
