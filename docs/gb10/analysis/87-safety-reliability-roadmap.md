# 8d. Safety & Reliability Roadmap — GB10 librealsense Build

> **Static review only.** GB10 xHCI controller is currently DEAD (no USB). No
> streaming/enumeration/hardware access. Pure source + config + installed-artifact
> analysis of `~/dev/repos/librealsense` @ `81f35eb81` and the installed
> `/opt/vigil/opt/` builds, the validation tree `~/realsense-gb10-validation/`, and
> the David-Martel GB10 commits `f7ae4ca43..81f35eb81`.
>
> **Purpose.** Close the gap doc `20-…` §2.4 flagged: the reliability *principles* in
> `realsense.TODO.md` ("Runtime Reliability Work") are **aspirational** — the shipped
> artifacts are the power band-aids + the `rs-gb10-profiler` benchmark tool. The
> capture-path reliability layer (bounded-wait worker, reconnect backoff, drain-to-newest,
> single-owner lock) **does not exist as a reusable production component.** This doc gives
> concrete, integrable items to make it real.
>
> **Scope discipline.** This builds on, and does **not** re-derive: doc `80` (mechanism
> H5–H8), doc `85` (RSUSB patches P1–P6), doc `86` (V4L2 backend assessment), doc `50`
> (staged remediation). Where those docs already specify a patch, this doc *references*
> it and adds the missing reliability/ops/CI layer around it.

---

## 0. Settled constraints that shape every recommendation below

These are **fixed findings**; recommendations that violate them are excluded by design.

1. **The controller death (H7 / doc 85 P6) is NVIDIA silicon/BSP.** Nothing here claims
   to "fix" the crash. Every item below only **reduces the trigger surface** or **fails
   safe** around it. The only crash fix is the NVIDIA escalation (doc 85 P6 / doc 50 §4.5).

2. **Eager full-device uvcvideo detach (doc 85 P1) and the unbind-uvcvideo udev rule
   (doc 85 P5) are CONTRAINDICATED and SUPERSEDED.** Docs 80/85 rated P1 "★★★ highest"
   and P5 "deploy now"; the newer settled finding reverses this — eager detach / unbind
   *wedges the controller* and is **incompatible with the recommended production backend**
   (V4L2 *requires* uvcvideo bound — doc 86 §6.2). **Do not deploy P1 or P5.** The only
   survivor of the RSUSB stop-hardening set is doc 85 **P4** (serialized stop), and only
   as *RSUSB-applicable* hardening — V4L2 gets serialized stop for free from the kernel
   (doc 86 §1, §7).

3. **V4L2 (native uvcvideo) is the recommended production backend** (doc 86 VERDICT).
   It removes H5 + the H6 `-110` storm surface + the multi-threaded clear_halt burst.
   Frame-reliability items below are written to apply to **both** backends because the
   capture-resilience layer sits *above* `rs2::pipeline` and is backend-agnostic.

4. **The global power band-aids are to be removed** (doc 20 §2.2, doc 50 Stage 1):
   `99-dgx-spark-performance.rules`, machine-wide `usbcore.autosuspend=-1`,
   `pcie_aspm=off`, `pcie_port_pm=off`. A serial-scoped keep-awake is permitted **only
   if** a *measured* autosuspend-induced drop appears on the GREEN link.

5. **Serial identifiers — discrepancy to resolve.** The **USB descriptor serial**
   (what udev `ATTR{serial}`, USBGuard, and `rs-fw-update -s` match on) is
   **`404543020690`** (confirmed in every healthcheck `topology.txt` / `result.json` and
   `rs-enumerate.txt` "Firmware Update Id"). The **SDK device serial** is `327122076391`
   (doc 90). The `realsense.TODO.md:161` value **`346522072418`** does **not** match this
   unit and is paired with the also-wrong firmware `5.17.0.10` — treat it as **stale or a
   different physical unit; a human must confirm which camera is in the rack.**

   **Which serial each layer matches (do not cross them):**
   - **udev `ATTR{serial}` (R5) + USBGuard `serial "…"` (R4)** → **descriptor serial
     `404543020690`** (these match the USB descriptor; correct as written).
   - **SDK-facing tools — `rs-gb10-healthcheck.sh`/`rs-gb10-hil.sh --serial`,
     `cfg.enable_device(...)`, profiler `--serial` (R6) and the R7 GREEN pre-check** →
     **SDK serial `327122076391`** (`RS2_CAMERA_INFO_SERIAL_NUMBER`). Passing
     `404543020690` here matches **zero devices**. Single-camera rack → **omit `--serial`**
     (auto-detect) in all R6/R7 SDK invocations below.
   - **`rs-fw-update -s` (R7)** → ambiguous (DFU=`404543020690` / normal=`327122076391`);
     **omit `-s`** since `-l` confirms a single D435 (see R7).

---

## 1. The capture-path reliability layer (doc 20 §2.4 — the central gap)

### 1.1 What actually exists vs. what the TODO claims

| `realsense.TODO.md` "Runtime Reliability Work" item | Real code today? | Where |
|---|---|---|
| "worker thread with bounded waits" | **Partial — in the *benchmark* only.** `rs-gb10-profiler` has bounded `try_wait_for_frames` (`rs-gb10-profiler.cpp:555`) and a `process_lock` (`:138-160`), but it is a one-shot stress/benchmark binary, not a reusable capture library. | `tools/gb10-profiler/rs-gb10-profiler.cpp` |
| "reconnect backoff" | **DOES NOT EXIST.** `grep -i 'reconnect\|backoff'` over the profiler and SDK app paths returns nothing. No reconnect state machine anywhere in the fork's own code. | — |
| "drain to the newest frameset, queue depth 1 or 2" | **Stop-path drain only.** `drain_before_stop` (`:638-667`) drains *before stop*; there is no steady-state drain-to-newest with a depth-1/2 queue for a live display consumer. | `:638-667` |
| "single-owner RealSense lock used by VIGIL" | **Two disjoint locks, neither reusable.** Profiler `flock` `process_lock` (`:138`); VIGIL's own lock (TODO:97) lives in VIGIL, not the SDK build. No shared primitive. | profiler `:138`; VIGIL (external) |
| "avoid default `hardware_reset()`" | **Honored by omission, not enforced.** Profiler never calls it (grep clean); SDK still *exposes* `rs2_hardware_reset` (`src/rs.cpp:1745`). Nothing *prevents* an app from calling it. | `src/rs.cpp:1745`, `src/device.cpp:153` |

**Conclusion:** the reliability layer is **prose, not code.** The profiler proves the
*lifecycle* primitives (bounded wait, watchdog stop, drain-before-stop, process lock) but
they are entangled in a benchmark binary and not packaged for a production capture service.

### 1.2 Recommendation R1 — extract a small reusable capture wrapper (do NOT hand-roll a lock)

**The architectural call:** before writing anything new, the dominant option is to
**configure the existing `realsense2_camera` ROS node**, which *already implements* the
bulk of the TODO's reliability list — `reconnect_timeout`, `wait_for_device_timeout`,
`serial_no`, `usb_port_id`, `enable_sync`, `SENSOR_DATA` QoS (TODO:107-109, doc 50 Stage 5).
For a ROS deployment, **prefer `realsense2_camera` over a hand-rolled wrapper** and pin a
single source of truth (align `ros-jazzy-librealsense2` 2.57.7 with the SDK 2.58.1 — doc 50
Stage 5 already flags the version drift).

For the **non-ROS / VIGIL native path**, extract a header-only `rs_capture_guard` from the
profiler's already-proven primitives rather than inventing new ones:

- **Single-owner lock:** reuse the profiler's `flock`-based `process_lock`
  (`rs-gb10-profiler.cpp:138-160`) verbatim — do **not** invent a new lock; reconcile with
  VIGIL's existing single-owner lock (TODO:97) so there is exactly one owner primitive.
- **Bounded-wait worker:** lift the `try_wait_for_frames(frames, timeout_ms)` loop
  (`:555`, `:715-790`) into the wrapper's capture thread; never call the blocking
  `wait_for_frames()`.
- **Drain-to-newest, depth 1–2:** add a steady-state path that keeps only the latest
  frameset for display consumers (the profiler only drains *at stop*; this adds the
  *live* drain the TODO asks for). Implement as an `rs2::frame_queue(1)` on the display
  branch, distinct from the compute branch.
- **Reconnect backoff:** the genuinely new code. On `wait_for_frames` timeout streak or
  device-removed event, tear down cleanly (drain→settle→bounded stop, exactly the
  profiler's `drain_before_stop` + watchdog pattern at `:638-667,:790-792`), then retry
  `pipeline.start()` with exponential backoff (e.g. 250 ms → 4 s, capped), bounded retry
  count, **never** calling `hardware_reset()` (see R3).

- **(a) what + where:** new `tools/gb10-profiler/rs_capture_guard.h` (header-only, reuses
  profiler primitives) for native consumers; **and** a documented `realsense2_camera`
  parameter set for ROS consumers (doc 50 Stage 5 params).
- **(b) why safer:** converts an unhandled timeout/disconnect from a hard failure into a
  bounded, backed-off recovery; keeps a single device owner (prevents the dual-claim that
  feeds H5); drain-to-newest prevents the frame backlog that thickens the stop storm.
- **(c) effort:** **M** (native wrapper) / **S** (ROS param set — config only).
- **(d) risk:** Low. It is *additive*; it wraps `rs2::pipeline` and adds no new device-level
  command. Backoff must never escalate to `hardware_reset()`.
- **(e) validation:** authoring is **pure-code, do-now**; the *backoff-recovers-within-window*
  gate is **needs-HIL** (induced unplug/replug — doc 50 Gate G5 30-min soak).
- **(f) backend:** **both** (sits above `rs2::pipeline`).

---

## 2. Wire the existing USB2 fail-fast guard into a real entrypoint (R2)

`bin/rs-gb10-usb2-guard.sh` is **built, tested, and idempotent** (read-only sysfs scan,
exit 0/1/2, `--allow-usb2` opt-in, `RS_USB_SYSFS_ROOT` fixture support) — but doc
`USB2-FAILFAST-SOP.md` only shows it as an *example* snippet. **It is not wired into any
running capture entrypoint or systemd unit.** That is the §2.4 gap in concrete form: the
gate exists but nothing calls it.

- **(a) what + where:** add the guard as the **first** line of the production capture
  entrypoint and as a systemd `ExecStartPre=`:
  ```ini
  [Service]
  ExecStartPre=/opt/realsense/bin/rs-gb10-usb2-guard.sh
  ExecStart=/opt/realsense/bin/rs-capture.sh
  Restart=on-failure
  ```
  For ROS, gate the launch: a wrapper that runs the guard before `ros2 launch`.
- **(b) why safer:** USB2 link = guaranteed broken/dropped frames (doc 30, doc 50 Stage 3).
  Refusing to start beats producing bad data; the remediation message tells the operator
  exactly what to do ("rear native Type-C + eMarker; do not use the dock").
- **(c) effort:** **S** (one `ExecStartPre=` line + the prod entrypoint).
- **(d) risk:** Very low. The guard is read-only and already validated against fixtures.
  Only failure mode: it returns 2 (no device) when the camera is unplugged — that is the
  correct "don't start" behavior; ensure the service treats exit 2 as "not ready," not
  "crash-loop" (use `Restart=on-failure` + a restart backoff, or a oneshot pre-check unit).
- **(e) validation:** wiring is **pure-code, do-now**; the negative-path gate (dock=FAIL-fast,
  native=PASS) is **needs-HIL** (doc 50 Gate G3).
- **(f) backend:** **both** (the guard reads sysfs USB speed, backend-independent).

---

## 3. Avoid default `hardware_reset()` — make it enforced, not incidental (R3)

**Findings (grounded):**
- `rs-gb10-profiler` never calls `hardware_reset()` (grep clean) — good, but *incidental*.
- The SDK exposes it: `rs2_hardware_reset` (`src/rs.cpp:1745` → `device::hardware_reset()`
  `src/device.cpp:153`; D400 impl `src/ds/d400/d400-device.cpp:100`).
- I could **not** reach the VIGIL capture application source from this host (the VIGIL repos
  are on a separate Windows path); I therefore **cannot assert "no app call sites."** Scope
  the recommendation to *prevent* and *contain*, not to claim absence.

- **(a) what + where:** (i) audit the VIGIL/capture app for `rs2_hardware_reset` /
  `pipeline … hardware_reset()` call sites (the audit I could not complete here); (ii) gate
  any legitimate reset behind an **explicit recovery-only profile** — a separate code path
  (e.g. `--recovery` flag / `RecoveryProfile`) that first **collects USB + librealsense +
  kernel-journal artifacts** (mirroring the healthcheck's artifact capture) *before* issuing
  the reset, and is never on the steady-state path. The reconnect backoff in R1 must do its
  retries **without** a hardware reset.
- **(b) why safer:** `hardware_reset()` re-enumerates the device → another attach/detach
  cycle and another Stop-Endpoint-adjacent event on a controller that died on exactly that
  class of command. A reflexive reset on every hiccup *increases* H7 exposure.
- **(c) effort:** **S** (audit + a guard flag).
- **(d) risk:** Low. Risk is *not* doing it — an unaudited app reset on a flaky link.
- **(e) validation:** audit + guarded-profile authoring is **pure-code, do-now**; confirming
  the recovery profile actually re-acquires cleanly is **needs-HIL**.
- **(f) backend:** **both** (`hardware_reset` is a device-level command in both backends).

---

## 4. USBGuard allow-by-serial, not by class (R4)

`realsense.TODO.md:95-96` calls for permanent USBGuard rules "by RealSense serial, not by
broad device class." Today the camera is authorized ad-hoc; there is no committed
serial-scoped rule in the validation tree.

- **(a) what + where:** a committed USBGuard rule allowing **only** the D435 by its USB
  descriptor serial:
  ```
  allow id 8086:0b07 serial "404543020690" name "Intel(R) RealSense(TM) Depth Camera 435"
  ```
  (descriptor serial `404543020690` — §0.5; **not** the SDK serial, **not** by class
  `8086:*`, **not** the stale TODO value `346522072418`). Place in
  `/etc/usbguard/rules.conf` (or a drop-in) and document the regeneration step.
- **(b) why safer:** allow-by-class authorizes *any* future Intel UVC device; allow-by-serial
  pins exactly this camera, shrinking the trust surface and preventing an unknown device from
  silently claiming the RealSense path.
- **(c) effort:** **S**.
- **(d) risk:** Low — but if the camera is RMA'd/swapped the serial changes; document the
  rule-update step. Also confirm the operator hasn't disabled USBGuard entirely.
- **(e) validation:** rule authoring is **pure-code, do-now**; "device authorizes and streams"
  is **needs-HIL**.
- **(f) backend:** **both** (authorization is below the backend).

---

## 5. Band-aid removal + serial-scoped keep-awake ONLY if measured (R5)

This is doc 50 **Stage 1** made concrete and gated. **Default position: remove the global
rules.** Do **not** pre-emptively add any keep-awake.

- **(a) what + where:**
  1. `sudo mv /etc/udev/rules.d/99-dgx-spark-performance.rules{,.disabled}`
  2. `sudo mv /etc/udev/rules.d/99-vigil-realsense-power.rules{,.disabled}`
  3. At the next kernel-cmdline maintenance window, drop `usbcore.autosuspend=-1`,
     `pcie_aspm=off`, `pcie_port_pm=off`, `nvme_core.default_ps_max_latency_us=0` from
     `/etc/default/grub.d/99-dgx-spark-performance.cfg` (don't reboot solely for this).
  4. `sudo udevadm control --reload && sudo udevadm trigger`.
  5. **Conditional keep-awake — add ONLY if a measured autosuspend-induced drop appears**
     on the GREEN link:
     ```
     ACTION=="add", SUBSYSTEM=="usb", ATTR{serial}=="404543020690", ATTR{power/control}="on"
     ```
     (serial-scoped — §0.5 — never global, never by `idProduct`).
- **(b) why safer:** the global rules disable runtime PM for *every* USB/PCI device and kill
  PCIe ASPM machine-wide (doc 20 §2.2) — raising idle power/heat and touching signal-integrity
  on unrelated links, a sledgehammer for one camera that already runs healthy at
  `power/control=auto`. Removing them shrinks blast radius to zero and re-baselines honestly.
- **(c) effort:** **S**.
- **(d) risk:** Low; fully reversible (restore the `.disabled` files). The *only* regression
  risk is an autosuspend drop that was previously masked — caught by the Stage-1 re-baseline
  (Gate G1), which is exactly when the conditional serial-scoped rule would be added.
- **(e) validation:** the `mv`/reload is **do-now** (config), but proving "no regression
  without the band-aids" is **needs-HIL** (Gate G1). The keep-awake is added *only* in
  response to a measured HIL result.
- **(f) backend:** **both**.

---

## 6. Regression CI gate after every kernel / BSP / SDK / firmware change (R6)

Doc 50 Stage 6 names `rs-gb10-healthcheck.sh` the **canonical, idempotent gate runner**
(exit `0=PASS / 1=FAIL / 2=setup`, emits `result.json` + `report.md` + `SHA256SUMS`). It is
**not yet wired into any CI / maintenance trigger** — it is run by hand.

- **(a) what + where:** a CI/maintenance job (or a systemd `OnCalendar` + `Restart` unit)
  that runs the gate after any change to: kernel, NVIDIA BSP/firmware, librealsense SDK
  build, or D435 camera firmware. Concrete invocation (interfaces verified):
  ```bash
  # Fast gate (every change): canonical idempotent PASS/FAIL
  rs-gb10-healthcheck.sh || exit 1
  # Backend no-regression (post kernel/SDK change): both backends must PASS
  rs-gb10-hil.sh --backend v4l2  || exit 1
  rs-gb10-hil.sh --backend rsusb || exit 1
  # Periodic soak (weekly / post-firmware): 30-min single-stream
  rs-gb10-healthcheck.sh --soak 1800 || exit 1
  ```
  > **Serial note:** these are **SDK-facing** tools — `rs-gb10-hil.py:369` does
  > `cfg.enable_device(serial)` (the `RS2_CAMERA_INFO_SERIAL_NUMBER` filter =
  > **`327122076391`**), and healthcheck passes `--serial` into the same profiler SDK
  > path. Passing the *descriptor/ASIC* serial `404543020690` here would match **zero
  > devices**. Because this is a single-camera rack and both scripts default `--serial`
  > to auto-detect-first-device, **omit `--serial` entirely** (shown above). If you must
  > pin it, use the **SDK** serial `327122076391`, never `404543020690`. Contrast R4/R5,
  > where the **descriptor** serial `404543020690` is the correct match key.
  Archive each run's timestamped artifact dir; fail the pipeline on exit≠0; compare
  `result.json` against the accepted baseline (Gate G6).
- **(b) why safer:** kernel/BSP/uvcvideo updates can silently change USB/xHCI or metadata-node
  behavior (doc 86 §6.4 — stock-kernel uvcvideo metadata is version-sensitive); a green
  baseline that is *re-asserted automatically* after every such change is the difference
  between catching a regression on a bench vs. in the field.
- **(c) effort:** **S** (the harness exists; this is wiring + a baseline-compare step).
- **(d) risk:** Low. Caveat: the gate **requires live hardware** — a dead controller makes it
  FAIL/setup-error, so it must run on a known-GREEN bench, not a controller-dead host.
- **(e) validation:** the CI yaml / unit is **pure-code, do-now**; every *run* of it is
  **needs-HIL** (it streams). It is the harness that *enforces* the HIL gate.
- **(f) backend:** **both** (the `--backend` flag exercises each; this is precisely the
  no-regression matrix in task #9).

---

## 7. Controlled firmware update path — green-link-gated (R7)

Doc 50 Stage 4. The device reports **`5.13.0.55`** (old). Plan: `5.13.0.55 → 5.16.0.1`
(conservative stable) first; re-validate; **optionally** → `5.17.0.9` (latest). The TODO's
`5.17.0.10` does not exist (doc 20 §2.3) — do not target it.

- **(a) what + where:** a gated `rs-gb10-fw-update.sh` wrapper that **refuses to flash unless
  Stage-0 GREEN is proven first**, then flashes by serial:
  ```bash
  rs-gb10-healthcheck.sh || { echo "link not GREEN — refusing to flash"; exit 1; }   # SDK-facing: auto-detect (single-camera rack)
  # no streaming clients; mains power; rs-fw-update -l lists exactly the D435
  realsense-gb10-env rs-fw-update -f Signed_Image_UVC_5_16_0_1.bin                    # -s OPTIONAL; resolve serial first (see note)
  ```
  > **`rs-fw-update -s` serial ambiguity — resolve before flashing.** Doc 50 uses
  > `-s 404543020690` (the ASIC / "Firmware Update Id," which DFU/recovery mode exposes),
  > but in *normal* (non-DFU) mode `-s` filters by the **SDK** `SERIAL_NUMBER`
  > (`327122076391`). The two differ on this unit. **Safest: omit `-s`** — this is a
  > single-camera rack, and `rs-fw-update -l` already confirms exactly one D435 before
  > flashing. Only add `-s` if a second camera is present, and then pick the key matching
  > the device's current mode (DFU → `404543020690`; normal → `327122076391`). Do **not**
  > inherit doc 50's value blindly.
  Verify the downloaded image checksum against the official D400 firmware release page before
  flashing. Keep the prior `5.13.0.55` image for rollback.
- **(b) why safer:** newer firmware reduces some D400 UVC errata; but flashing is the one
  **irreversible-ish, itself-a-HIL action** in this roadmap. Gating it behind a proven GREEN
  link prevents a half-flash over a flaky link (the worst-case bricking scenario).
- **(c) effort:** **S** (wrapper) + the flash itself.
- **(d) risk:** Medium — flashing is the highest-consequence step. Mitigated by: GREEN-gate
  pre-check, no streaming clients, mains power, checksum verify, kept rollback image.
- **(e) validation:** **needs-HIL — and is itself a HIL action**, not merely HIL-validated.
  Do **not** run until the controller is rebooted GREEN. Post-flash Gate G4: harness GREEN,
  `device.firmware` updated, fewer/zero `-110`/`-32` UVC events than baseline.
- **(f) backend:** **both** (firmware is below the backend; note doc 86 §1 — the DFU path uses
  libusb in *both* builds, so flashing works regardless of streaming backend).

---

## 8. New SDK safety guard — P7

> **CORRECTION 2026-06-03 (re-forensicated death #2; supersedes the original R8 framing below).**
> The original P7 here was an **advertised-profile membership guard**, blamed for controller-death #2.
> Re-reading the death-#2 SDK log + cached `rs-enumerate` (no hardware touched) **disproves that causation**:
> the requested `Color 848×480@60 BGR8` **resolved fine** to the native `YUYV @60`
> (`formats-converter.cpp:234/277`; the D435 advertises `Color 848x480 RGB8 @ 60/30/15/6 Hz`), and the link
> was clean **USB-3.2**. Death #2 was **churn**: the deprecated harness destroyed+recreated the whole
> `rs2::context`/pipeline between depth and color, releasing the device to kernel uvcvideo, which re-probed
> control endpoints → `-110` storm → lethal Stop-Endpoint; the controller was already `NO_DEVICE` before the
> color profile was issued. **The membership guard would never have fired.**
>
> **The implemented P7 is therefore the *device re-acquire guard*, not a membership guard.** It catches the
> verified mechanism — a mid-session re-acquire of the same physical device (the context-recreation/churn
> signature) — *before* the control storm: pure `resolve_reacquire_action`/`reacquire_advice` in
> `src/usb-tuning.h` (TDD-unit-tested), wired at one call site in `src/libusb/device-libusb.cpp`
> (per-process per-device acquire counter keyed by USB bus-port `unique_id`). Opt-in via `RS2_GB10_USB_TUNING`
> (byte-identical upstream); **advisory `WARN` by default**, hard-`REFUSE` only via `RS2_GB10_REFUSE_REACQUIRE=1`;
> it never auto-detaches uvcvideo (the contraindicated P1/P5). The safe pattern it steers callers toward is
> *session-stable ownership*: hold one context per process and reconfigure via `rs2::config`.
>
> The membership guard below is **demoted to a low-priority "never issue an unadvertised config" nicety** —
> correct in principle (and `sensor::open()`/`config::resolve()` already reject truly-unadvertised configs),
> but **not** what killed the controller. Original text retained below for the record.

### 8(orig). Refuse a profile not in the advertised set (R8 — demoted)

This is the one genuinely **new SDK-level guard** (additive to doc 85's P1–P6; call it
**P7**). Per the task framing, requesting a configuration the D435 does **not advertise**
(848×480@60 BGR8 color) drove a `-110` control storm during config resolution that
contributed to controller-death #2. (**Attribution note:** the "caused controller-death #2"
causation is the task author's / `rs-gb10-hil.sh` header's framing — see the hil rewrite
rationale (a) — I have **not** read the raw `20260602-2159-xhci-controller-death/` artifacts
to verify that specific causal chain myself. The guard is sound on first principles
regardless: never issue a config the device didn't advertise.) **[See the correction above —
this causal claim was later disproven; 848×480@60 BGR8 resolved fine to native YUYV.]**

- **(a) what + where:** in the config-resolution path, **before** committing a stream config,
  validate every requested (stream, format, resolution, fps) tuple against the device's
  **advertised** profile set (`sensor.get_stream_profiles()`); if any requested profile is
  not advertised, **fail fast with a clear error** naming the unsupported tuple and listing
  the nearest advertised alternatives — *before* any control transfer is issued. Touch point:
  the resolver / multistream setup the docs already cite — `src/pipeline/resolver.h:137-159`
  (serial open loop) and `:139-145` (depth/color coupling). Upstreamable as opt-in; default-on
  in the GB10 profile.
- **(b) why safer:** the failure mode that hurt was a *negotiation*-time `-110` storm from an
  unadvertised request. A pre-flight membership check turns "storm the control endpoint trying
  to honor an impossible config" into "reject before touching EP0" — removing a control-path
  stressor (H6) entirely for the bad-config case.
- **(c) effort:** **M** (SDK code + an opt-in flag + a mockable unit test that feeds a fake
  profile list and asserts the reject path).
- **(d) risk:** Low-Med. Must not reject *valid* configs (off-by-one in fps/format matching);
  the unit test must cover advertised-pass and unadvertised-fail. Keep opt-in upstream
  (default-on GB10 only) per doc 85's compatibility rule.
- **(e) validation:** **pure-code, do-now** for authoring + unit test (mockable profile list,
  no hardware). The "does it actually prevent the storm on live hardware" confirmation is
  needs-HIL, but the *guard logic itself* is unit-testable now.
- **(f) backend:** **both** (config resolution is above the backend; the advertised-set check
  is identical for V4L2 and RSUSB).

---

## 9. Items explicitly EXCLUDED (so a reader of docs 80/85 isn't confused)

| Excluded item | Source that rated it high | Why excluded here |
|---|---|---|
| **P1 eager full-device uvcvideo detach** | doc 85 P1 "★★★", doc 80 §8.6#1 | Contraindicated (wedges controller); incompatible with V4L2 production backend (§0.2). |
| **P5 unbind-uvcvideo udev rule** | doc 85 P5 "deploy now" | Same — removes `/dev/video*`, breaks V4L2 (doc 86 §6.2), wedges controller. |
| **Anything claiming to "fix" the crash** | — | H7/P6 is NVIDIA silicon/BSP (§0.1). Only the NVIDIA escalation can fix it. |
| **Machine-wide keep-awake / `pcie_aspm=off`** | the original band-aids | Removed in R5; blast radius = whole machine (doc 20 §2.2). |

**RSUSB-only tunings (defer to doc 85, not re-specified here):** `usbfs_memory_mb` (doc 85 P3),
`usb_request_count` (doc 85 P2), serialized stop (doc 85 P4). These help the **RSUSB** path
only and have **no effect on V4L2** (doc 86 §6.5). Since V4L2 is the production backend, they
are secondary; apply only if RSUSB is retained for a specific consumer.

---

## 10. Prioritized roadmap

Backend column: **both** unless a backend is the *only* one a row affects.

| ID | Item | Pri | Effort | Risk | Backend | Pure-code do-now? |
|----|------|-----|--------|------|---------|-------------------|
| **R2** | Wire `rs-gb10-usb2-guard.sh` into prod entrypoint + systemd `ExecStartPre=` | **P0** | S | Low | both | **DO-NOW** (wiring) |
| **R5** | Remove global power band-aids (Stage 1); no keep-awake unless measured | **P0** | S | Low | both | **DO-NOW** (config `mv`) |
| **R3** | Audit app for `hardware_reset()`; gate behind recovery-only profile | **P0** | S | Low | both | **DO-NOW** (audit + guard) |
| **R4** | USBGuard allow-by-serial `404543020690` (not class, not stale serial) | **P0** | S | Low | both | **DO-NOW** (rule) |
| **R8/P7** | SDK guard: refuse profile not in advertised set | **P0** | M | Low-Med | both | **DO-NOW** (code + unit test) |
| **R1** | Reusable capture wrapper: bounded-wait worker, drain-to-newest depth 1–2, reconnect backoff, single-owner lock — prefer `realsense2_camera` for ROS | **P1** | M (S for ROS) | Low | both | **DO-NOW** (author) / HIL (verify backoff) |
| **R6** | Wire `rs-gb10-healthcheck.sh` + `rs-gb10-hil.sh` no-regression matrix into CI/maintenance | **P1** | S | Low | both | **DO-NOW** (CI yaml) / each run = HIL |
| **R7** | Gated firmware `5.13.0.55 → 5.16.0.1` (→ opt. 5.17.0.9), GREEN-link-gated wrapper | **P2** | S+flash | Med | both | **NEEDS-HIL** (flash *is* a HIL action) |
| **P6** | NVIDIA xHCI controller-death escalation (the only crash fix) | **P0\*** | — | — | platform | external (file the bug) |

\* **P6 is P0 in importance but external** — it is the NVIDIA BSP/firmware escalation (doc 85
P6 / doc 50 §4.5), not a code change in this build. Re-run R6 after every BSP update.

### Split: authoring now vs. validation later

**Pure-code / config — author & commit NOW (controller dead, no HIL needed):**
- R2 (guard wiring + systemd unit), R5 (band-aid `mv` + reload), R3 (call-site audit +
  recovery-profile flag), R4 (USBGuard rule), R8/P7 (SDK advertised-set guard + mockable
  unit test), R1 (capture wrapper code / ROS param set), R6 (CI yaml + baseline-compare step),
  R7's *wrapper* (the GREEN-gate script — but **not** the flash).

**Needs post-reboot HIL to validate (PASS/FAIL gate requires the live GREEN controller):**
- R1 reconnect-backoff recovery (induced unplug/replug, doc 50 G5 soak).
- R2 negative path (dock=FAIL-fast, native=PASS, doc 50 G3).
- R5 no-regression-without-band-aids re-baseline (doc 50 G1); conditional keep-awake added
  *only* if a measured drop appears.
- R6 every run (it streams) — this is the harness that *enforces* the gate.
- R8/P7 on-hardware confirmation (logic is unit-testable now; storm-prevention is HIL).
- **R7 firmware flash — itself a HIL action**, the single irreversible-ish step; do not run
  until the controller is rebooted and Stage-0 GREEN is re-proven.

---

## 11. Sources (file:line)

- **Reliability-gap evidence:** `tools/gb10-profiler/rs-gb10-profiler.cpp:138-160`
  (`process_lock`), `:555` (`try_wait_for_frames`), `:638-667` (`drain_before_stop`),
  `:715-792` (bounded run loop + watchdog stop); `grep reconnect|backoff` = none.
- **`hardware_reset`:** `src/rs.cpp:1745` (`rs2_hardware_reset`), `src/device.cpp:153`,
  `src/ds/d400/d400-device.cpp:100`; profiler grep clean.
- **Advertised-set guard touch points:** `src/pipeline/resolver.h:137-159`, `:139-145`.
- **Existing tooling:** `~/realsense-gb10-validation/bin/rs-gb10-usb2-guard.sh` (read-only
  guard, exit 0/1/2), `bin/rs-gb10-healthcheck.sh` (canonical gate, exit 0/1/2, `--soak`,
  `--serial`), `bin/rs-gb10-hil.sh` (`--backend rsusb|v4l2`, `--serial`), `docs/USB2-FAILFAST-SOP.md`.
- **Serial identifiers:** healthcheck `topology.txt` / `result.json` / `rs-enumerate.txt`
  (descriptor serial `404543020690`); doc 90 (SDK serial `327122076391`);
  `realsense.TODO.md:161` (stale `346522072418`, stale FW `5.17.0.10`).
- **Band-aids + Stage matrix:** `realsense.TODO.md:204-217`, doc 20 §2.2, doc 50 Stage 1/3/4/6.
- **Backend applicability:** doc 86 §6.2 (V4L2 needs uvcvideo bound), §6.5 (usbfs/request_count
  RSUSB-only), §1/§7 (V4L2 serialized stop for free); doc 85 P1–P6 (RSUSB patch set), P4 survivor.
- **Mechanism:** doc 80 §8.2 (failure chain), §8.3 (H5–H8), §8.5 (no GB10 recovery).
