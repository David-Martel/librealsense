# ROS2 RealSense Depth Stream-Start Failure on GB10 — Static Root-Cause Analysis

**Date:** 2026-06-05
**Author:** offline static/log analysis (no hardware touched)
**Scope:** `realsense2_camera` (realsense-ros ~4.58) depth-only start on GB10 / DGX-Spark (ARM64),
GB10 SDK `librealsense2 2.58.1`, D435 firmware **5.15.1.55**.

> **The analysis below was offline/static. The minimal config it proposed is HIL-VERIFIED to stream
> cleanly — but the subsequent single-variable A/B REFUTED the H1 mechanism (see the A/B RESULTS box).**
> The *fix* is proven; the *root cause* is a parameter **combination**, NOT the manual-exposure-under-AE
> write H1 blamed. Read the H1 reasoning in the body as the (now-falsified) pre-test hypothesis.

---

## ✅ VERIFIED RESULT (2026-06-05 live HIL, single-process, 25s bounded run)

`scripts/gb10/ros2-launch-depth-minimal.sh` (depth-only 848×480×30, auto-exposure **ON**, **no** manual
depth-XU control pushed) **streams cleanly on GB10**:

| Metric | Result |
|--------|--------|
| Depth frames | **708** (Counter 0→707), Z16 848×480 |
| Frame rate | **30.03 fps**, **zero drops** (707 expected = 707 actual over 23.57 s) |
| Fatal errors | **none** — no "Hardware Error", no `-110`, no failed-start |
| `index 768 / 0x0300` | **2 benign startup warnings**, `EAGAIN` ("Resource temporarily unavailable, number: 11") — the SDK rides through them |
| Controller | **GREEN** before and after (no `HC died`); camera stayed @ 5000M USB3; `/dev/video*` released cleanly |

### Single-variable A/B (2026-06-05, 8 controlled live runs — H1 REFUTED)

The A/B was run (single depth stream = safe envelope; controller GREEN throughout all 8 runs). It **refutes H1**
and shows the fix is a *combination*, not the manual-exposure write:

| Run | Config (Δ from minimal / original) | Frames | fps | idx768 | Result |
|-----|------------------------------------|-------:|----:|-------:|--------|
| ab0 | minimal (baseline)                 | 319 | 30.09 | 2 | **PASS** |
| ab1 | minimal **+ `depth_module.exposure:=8500`** | 319 | 30.09 | **5** | **PASS** → **H1 REFUTED** |
| ab2 | minimal **+ `initial_reset:=true`** | 316 | 30.09 | 2 | **PASS** → co-suspect refuted |
| ab4 | minimal (re-confirm, post-ab3)     | 318 | 30.09 | 2 | **PASS** (device healthy) |
| ab3 | **original** (all node defaults)   | **0** | — | 5 | **FAIL** (0 frames) |
| ab5 | original **+ `enable_sync:=false`** | **0** | — | 2 | **FAIL** → refuted |
| ab6 | original **+ `hdr_enabled:=false`** | **0** | — | 0 | **FAIL** → refuted |

**Findings:**
- **H1 is REFUTED.** Explicitly pushing `depth_module.exposure:=8500` under AE *streams fine* — it only raises the
  benign idx768 `EAGAIN` count (2→5), confirming the write reaches the depth XU but the fw returns `EAGAIN`, **not** a
  fatal error. The manual-exposure-under-AE write is not the killer. `initial_reset:=true` also streams (the
  `hardware_reset` re-enumerates cleanly: `usb 2-1: USB disconnect` then back @ 5000M). Both named suspects: cleared.
- **The original full-default config deterministically gets 0 frames** (ab3), and the device was proven healthy
  *immediately after* (ab4 PASS) — so this is a real config effect, not a post-reset transient. Same SDK/env in both
  scripts (verified offline: only `LRS_LOG_LEVEL` differs), so the cause is the ROS2 **parameter combination**.
- **No single override fixes the original:** adding only `enable_sync:=false` (ab5) or only `hdr_enabled:=false` (ab6)
  still yields 0 frames; AE (`true`) and `inter_cam_sync_mode` (`0`) defaults already match the minimal. So the
  working config depends on a **combination** of the minimal's overrides — not isolatable to one param without
  mapping the 2^N subset space (deferred; cumulative single-camera risk).
- **Symptom shifted** from the first session's *"Hardware Error"* to a silent *0-frames* in ab3/ab5/ab6, hinting the
  fatal-vs-silent face has a state component — but the 0-frames failure itself is deterministic and config-driven.

**Bottom line:** the deployable minimal config is proven (4/4 PASS). H1 is wrong; the fix is the override
*combination*. Exact-subset isolation + Kilted rebuild are follow-ons. Logs: `~/realsense-gb10-validation/{hil-runs/*ab*,ros2-ab*}`.

---

## 0. The established fact we are explaining

- `pyrealsense2`, single process, depth `848x480x30`, streams 30/30 fps cleanly, controller GREEN.
- `realsense2_camera` node builds, comes up, advertises depth topics, links the GB10 SDK,
  then **fails on the control path at depth-stream start** with **"Hardware Error"** and a
  failing `control_transfer` at **index 768 (0x0300)**.
- Therefore the delta is *what the node writes to the depth sensor that a bare pyrealsense2
  pipeline does not* — a **node-config** problem, not hardware.

---

## 1. What `index 768 / 0x0300` is (SDK evidence)

The libusb backend logs only the `wIndex` of the failing transfer:

- `src/libusb/messenger-libusb.cpp:42` —
  `LOG_WARNING("control_transfer returned error, index: " << index << ...)`
  (note: it logs **index only**, not `wValue`/control selector — see §5).

`wIndex` for all UVC class get/set on this device is encoded as:

- `src/uvc/uvc-device.cpp:549` (`get_data_usb`) → `unit << 8 | (_info.mi)`
- `src/uvc/uvc-device.cpp:639` (`set_data_usb`) → `unit << 8 | (_info.mi)`
- `src/uvc/uvc-device.cpp:666` (`uvc_get_ctrl`) and `:689` (`uvc_set_ctrl`) → `unit << 8 | _info.mi`

So `index = (unit << 8) | mi`. For `0x0300`: **mi = 0** and **unit = 3**.

The D4xx **depth extension unit** is exactly unit 3 on interface 0:

- `src/ds/ds-private.h:70` —
  `const platform::extension_unit depth_xu = { 0, 3, 2, {GUID...} };`
  (fields: `subdevice=0`/iface, `unit=3`, `node=2`, GUID).

> **wIndex 0x0300 == the D4xx DEPTH EXTENSION UNIT (XU), unit 3, interface 0.**
> This is a *unit-level* identification, not a single control. The specific control is carried
> in `wValue = control << 8` (`uvc-device.cpp:548/638/665/688`), which is **not** in the index
> the log shows. So below we **rank** over the depth-XU control table; we do not assert one control.

### Depth-XU control selectors (the `wValue >> 8` candidates) — `src/ds/ds-private.h:50-64`

| Selector (`DS5_*`) | Value | RS2 option it backs |
|---|---|---|
| `DS5_HWMONITOR` | 1 | HW-monitor / advanced-mode command transport (`command_transfer_over_xu`, `d400-device.cpp:565`) |
| `DS5_DEPTH_EMITTER_ENABLED` | 2 | `RS2_OPTION_EMITTER_ENABLED` (`ds-options.cpp:37`) |
| `DS5_EXPOSURE` | 3 | `RS2_OPTION_EXPOSURE` (depth) (`d400-device.cpp:776`, `uvc_xu_option<uint32_t>`) |
| `DS5_LASER_POWER` | 4 | `RS2_OPTION_LASER_POWER` (`ds-active-common.cpp:39`) |
| `DS5_HARDWARE_PRESET` | 6 | `RS2_OPTION_VISUAL_PRESET` (`d400-device.cpp:691`) |
| `DS5_ERROR_REPORTING` | 7 | `RS2_OPTION_ERROR_POLLING_ENABLED` (`d400-device.cpp:724`) |
| `DS5_EXT_TRIGGER` | 8 | `RS2_OPTION_INTER_CAM_SYNC_MODE` (`d400-device.cpp:716` / `:919`) |
| `DS5_ASIC_AND_PROJECTOR_TEMPERATURES` | 9 | ASIC/PROJECTOR temperature (`ds-options.cpp:64`) |
| `DS5_ENABLE_AUTO_WHITE_BALANCE` | 0xA | (depth AWB) |
| `DS5_ENABLE_AUTO_EXPOSURE` | 0xB | `RS2_OPTION_ENABLE_AUTO_EXPOSURE` (`d400-device.cpp:784`, `uvc_xu_option<uint8_t>`) |
| `DS5_THERMAL_COMPENSATION` | 0xF | `RS2_OPTION_THERMAL_COMPENSATION` (`d400-device.cpp:738`) |
| `DS5_EMITTER_FREQUENCY` | 0x10 | `RS2_OPTION_EMITTER_FREQUENCY` (`emitter-frequency-feature.cpp:19`) |
| `DS5_DEPTH_AUTO_EXPOSURE_MODE` | 0x11 | `RS2_OPTION_DEPTH_AUTO_EXPOSURE_MODE` (RS455 + fw≥5.15 only) |

**Unit-pruning rule:** only options whose backend is a **depth-XU** option can produce `0x0300`.
Depth **GAIN is explicitly a PU option** (`d400-device.cpp:781`, `uvc_pu_option`) → routes through
the **processing unit**, a *different* `wIndex` → **excluded** from the 0x0300 candidate set.

---

## 2. What the ROS node writes to the depth sensor at startup

### 2a. The forced HDR-disable (guaranteed delta, but CAUGHT → likely not fatal)

`ros_sensor.cpp:113` (`RosSensor::setParameters`):

```cpp
SAFE_SET_OPTION(RS2_OPTION_HDR_ENABLED, false);   // forced at node init, every non-rosbag device
```

`RS2_OPTION_HDR_ENABLED` on D400 is an `hdr_option` whose enable/disable ultimately toggles the
depth-XU sequence machinery — i.e. a **depth-XU write (0x0300)**. This is the one write that a bare
`pyrealsense2` pipeline does NOT do, so it is the most *suspicious* delta on paper.

**BUT** `SAFE_SET_OPTION` wraps the call and only **WARNs** on failure
(`ros_sensor.cpp:21-32`: `try { set_option } catch { ROS_WARN_STREAM(...) }`).
A failure here would print `Failed to set option: HDR_ENABLED ...` and **continue** — it would
**not** produce the fatal stream-start "Hardware Error". So despite being the cleanest delta,
HDR-disable is **ranked low as the FATAL cause** (though it may appear as a benign warning).

### 2b. `registerDynamicOptions` — iterates and re-writes every supported option

`sensor_params.cpp:229-294`: loops `i = 0 .. RS2_OPTION_COUNT`, and for each option the depth
sensor `supports()` and is not read-only, calls `set_parameter<T>()`.

`set_parameter` (`sensor_params.cpp:170-227`):
- reads the current device value,
- declares a ROS parameter defaulting to that current value,
- registers a **change callback** (`param_set_option`, line 204-207),
- and **only writes back** if `new_val != option_value` (line 216) — i.e. only when the
  *declared/overridden* ROS value differs from what the device already reports.

The write at `:220` (the `new_val != option_value` branch) **is** wrapped in try/catch and only WARNs.
So writes that happen *purely inside the registration loop* are non-fatal.

### 2c. The FATAL path: the parameter callback (`param_set_option`) is UNWRAPPED

`sensor_params.cpp:71-74`:

```cpp
template<class T>
void param_set_option(rs2::options sensor, rs2_option option, const rclcpp::Parameter& parameter)
{
    sensor.set_option(option, parameter.get_value<T>());   // NO try/catch — exception propagates
}
```

This is the lambda installed at `sensor_params.cpp:204-207` and fired by the ROS parameter
service whenever a **launch-supplied parameter** is applied. **This path is not wrapped**, so a
firmware rejection here propagates as an unhandled `rs2::error` → exactly the kind of fatal
"Hardware Error" seen at start.

### 2d. `rs_launch.py` FORCES depth-module parameter values (the injection trap)

`launch/rs_launch.py` `generate_launch_description` →
`OpaqueFunction(..., kwargs={'params': set_configurable_parameters(configurable_parameters)})`.
**All** `configurable_parameters` are declared on the node, not merely offered as CLI defaults.
Relevant depth-module defaults (`rs_launch.py:52-61`):

| Param | Default | Backend unit |
|---|---|---|
| `depth_module.exposure` | **8500** | depth **XU** (`DS5_EXPOSURE`, 0x0300) |
| `depth_module.gain` | 16 | **PU** (excluded) |
| `depth_module.enable_auto_exposure` | **true** | depth **XU** (`DS5_ENABLE_AUTO_EXPOSURE`, 0x0300) |
| `depth_module.hdr_enabled` | false | depth XU (hdr sequence) |
| `depth_module.inter_cam_sync_mode` | 0 | depth XU (`DS5_EXT_TRIGGER`, 0x0300) |

**Key consequence (the trap):** you cannot "avoid" a control write by *setting* its param —
*declaring* the param with a value that differs from the device default is exactly what *triggers*
the (unwrapped) write. A "minimal" launch that simply omits extra CLI args does **NOT** avoid
these writes, because rs_launch declares them anyway.

The tension to resolve per-option:
- `depth_module.exposure = 8500` is a **manual** exposure value written through the depth XU
  while `enable_auto_exposure = true`. Writing a manual depth-XU exposure (`DS5_EXPOSURE`) while
  auto-exposure is enabled is a self-contradictory control sequence that firmware can reject.

---

## 3. Ranked hypothesis (which depth-XU control the D435 fw 5.15.1.55 rejects)

Ranking combines two discriminators: **(a) routes through the depth XU (0x0300)** and
**(b) reaches an UNWRAPPED / fatal write path** (the parameter callback / start sequence), and
**(c) is a delta from the known-good bare pyrealsense2 depth pipeline.**

| # | Hypothesis | Control (`wValue>>8`) | XU? | Fatal path? | Confidence |
|---|---|---|---|---|---|
| **H1** | Manual `depth_module.exposure=8500` written via param-callback while AE=true → fw rejects contradictory depth-XU exposure write | `DS5_EXPOSURE` (3) | yes (0x0300) | **yes** (`param_set_option`, unwrapped) | **Highest** |
| H2 | `depth_module.enable_auto_exposure=true` (or the AE/exposure ordering) toggled via the unwrapped callback in a sequence the fw rejects | `DS5_ENABLE_AUTO_EXPOSURE` (0xB) | yes | yes | High |
| H3 | `RS2_OPTION_HDR_ENABLED=false` forced at init touches the depth-XU sequence machinery | hdr/sequence | yes | **no (caught → WARN)** | Low-as-fatal / may appear as benign warning |
| H4 | `inter_cam_sync_mode=0` (`DS5_EXT_TRIGGER`) write | `DS5_EXT_TRIGGER` (8) | yes | possible | Low (0 == default, `new_val==option_value` → no write) |
| H5 | Error-polling / thermal-compensation toggled at start | `DS5_ERROR_REPORTING`(7) / `DS5_THERMAL_COMPENSATION`(0xF) | yes | possible | Low |
| — | Depth **gain** | — | **no (PU)** | — | **Excluded by unit-pruning** |

**Top hypothesis (H1):** the node, via `rs_launch.py`'s declared `depth_module.exposure=8500`,
fires the **unwrapped** `param_set_option` → `set_option(RS2_OPTION_EXPOSURE, 8500)`, which on
the depth sensor is a `uvc_xu_option<uint32_t>` over `depth_xu`/`DS5_EXPOSURE`
(`d400-device.cpp:776`) → a `set_data_usb`/`set_xu` `control_transfer` at **wIndex 0x0300**.
Writing a manual depth-XU exposure value while depth auto-exposure is enabled is the most
plausible contradictory control the D435 firmware 5.15.1.55 rejects with "Hardware Error".

This is consistent with the known-good case: bare `pyrealsense2` `pipeline.start()` with a
plain depth config does **not** push a manual exposure nor force an AE/exposure re-write, so it
never issues the rejected 0x0300 transfer.

**Confidence:** moderate-to-high on the *unit* (0x0300 == depth XU: high, two-fact corroborated).
Moderate on the *specific control* (exposure vs AE), because the libusb log does not print `wValue`
(§5). H1 vs H2 is decided by the HIL run, not by static analysis.

---

## 4. Proposed minimal-param overrides (PENDING-HIL)

Goal: stop the node from issuing manual/contradictory depth-XU writes at start, while keeping a
clean depth-only `848x480x30` pipeline (the proven-safe envelope).

Override set (rationale per line):

| Param | Value | Why |
|---|---|---|
| `enable_color` | `false` | depth-only safe envelope (matches known-good) |
| `enable_gyro` / `enable_accel` | `false` | no motion module |
| `enable_infra1` / `enable_infra2` | `false` | depth-only |
| `depth_module.depth_profile` | `848x480x30` | proven-safe envelope |
| `depth_module.enable_auto_exposure` | `true` | leave AE ON; do **not** push a manual exposure underneath it |
| `depth_module.exposure` | **omit / leave at device default** | **H1 mitigation:** avoid the manual depth-XU `DS5_EXPOSURE` write while AE=true |
| `depth_module.gain` | leave default | PU path, not 0x0300; harmless |
| `depth_module.hdr_enabled` | `false` | matches device; HDR-disable is the forced init write (caught) |
| `depth_module.emitter_enabled` | leave default | do not toggle emitter XU unnecessarily |
| `depth_module.inter_cam_sync_mode` | `0` | default; `new_val==default` → no write |
| `enable_sync` | `false` | single stream; avoids extra alignment monitor churn |
| `initial_reset` | `false` | do NOT hardware_reset the fragile controller at start |

**Important caveat on the trap (§2d):** because `rs_launch.py` *declares* `depth_module.exposure`
with default 8500 regardless, the only robust way to stop the manual-exposure write under H1 is to
**force consistency**: keep `enable_auto_exposure:=true` AND set `depth_module.exposure` to a value
equal to the device's current/default so the `new_val != option_value` test is false (no write),
**or** better, drive the node from a YAML `config_file` that omits `depth_module.exposure` entirely
if the node version honors omission. The launch wrapper below takes the pragmatic path: AE on,
no extra controls, profile pinned — and documents that the residual rs_launch default may still
attempt the write. The HIL run will show (via `LRS_LOG_LEVEL=DEBUG`) whether 0x0300 still fires.

> **None of the above is verified.** It is the smallest static-evidence-based change set that
> *should* avoid the rejected 0x0300 transfer under H1/H2. **PENDING-HIL.**

---

## 5. How the serial HIL run will confirm/refute (instrumentation)

- The launch wrapper sets `export LRS_LOG_LEVEL=DEBUG` and tees all output to a log.
- **Caveat (verified in SDK):** `messenger-libusb.cpp:42` logs **only `index`**, not `wValue`/control.
  So the debug log will confirm the failing transfer is at `index: 768` but will **not** directly
  print the control selector. The discriminator is therefore the **pass/fail of the stripped
  config**:
  - If the minimal config **starts cleanly** → H1/H2 (a depth-XU exposure/AE write) confirmed as
    the cause class.
  - If it **still fails at 0x0300** → the cause is a depth-XU write that happens regardless of our
    params (e.g. the forced HDR-disable §2a, or `registerDynamicOptions` re-writing a supported
    option) → next step is to patch `realsense2_camera` to wrap/skip that write, or load a known-good
    advanced-mode JSON.
- Optional deeper signal (no code change to SDK): run with the node and capture `usbmon` /
  `LRS_LOG_LEVEL=DEBUG` together; the SDK option-layer logs which `rs2_option` it is setting just
  before the failing transfer, which pins exposure-vs-AE.

---

## 6. Kilted rebuild plan (PENDING-HIL)

Currently only **Jazzy** is at `/opt/ros/jazzy` and the workspace overlay builds
`realsense2_camera` (~4.58) against the GB10 SDK 2.58.1. **Kilted** (ROS 2 Kilted Kaiju) ships a
newer `ros-kilted-realsense2-camera` apt package on Ubuntu 24.04. Two routes:

### Route A — apt-install Kilted realsense2_camera (fast)
1. Add ROS 2 apt repo if not present; `sudo apt update`.
2. `sudo apt install ros-kilted-realsense2-camera ros-kilted-realsense2-camera-msgs`
   (this pulls `ros-kilted-librealsense2` — **the apt SDK, not our GB10 2.58.1 build**).
3. Overlay/env (mirror the existing depth-only script):
   - `export LD_LIBRARY_PATH=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib:$LD_LIBRARY_PATH`
     **before** `source /opt/ros/kilted/setup.bash`, so our GB10 `librealsense2.so.2.58.1`
     wins over the apt `ros-kilted-librealsense2`.
   - Verify with `ldd $(ros2 pkg prefix realsense2_camera)/lib/.../realsense2_camera ...` that the
     GB10 .so is the one resolved (the existing script's `GB10_SO` existence check pattern).
4. **Risk:** an apt realsense2_camera built against a *different* SDK ABI may not be binary-compatible
   with our 2.58.1 .so. If symbols mismatch, fall back to Route B.

### Route B — source-rebuild realsense2_camera against the GB10 SDK (robust, recommended)
1. Source Kilted: `source /opt/ros/kilted/setup.bash`.
2. In a fresh colcon ws, clone realsense-ros at its Kilted-compatible tag.
3. Point the build at the GB10 SDK so it links 2.58.1, not apt:
   - `colcon build --cmake-args -Drealsense2_DIR=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib/cmake/realsense2`
     (adjust to the actual cmake config dir under the GB10 prefix).
4. Overlay env identical to Route A step 3 (GB10 `LD_LIBRARY_PATH` ahead of ROS).

### How Kilted might change the option set (and thus the 0x0300 outcome)
- A newer realsense-ros may **wrap `param_set_option` in try/catch** (the current ~4.58 does not —
  `sensor_params.cpp:71-74`), which alone could downgrade the fatal start failure to a warning.
- It may change defaults (e.g. drop the forced `HDR_ENABLED=false`, or omit a manual
  `depth_module.exposure` default), changing which depth-XU writes fire at start.
- **Therefore the Kilted upgrade is itself a hypothesis-test**: re-diff `sensor_params.cpp` /
  `ros_sensor.cpp` / `rs_launch.py` of the Kilted tag against the findings here before declaring it
  a fix. **PENDING-HIL.**

---

## 7. Summary

- **0x0300 == D4xx depth extension unit (unit 3, iface 0)** — high confidence, two-fact corroborated
  (`ds-private.h:70` + `uvc-device.cpp:549/639`).
- The node's **fatal** option-write path is the **unwrapped** `param_set_option`
  (`sensor_params.cpp:71-74`), fired by `rs_launch.py`-declared depth-module parameters
  (the HDR-disable and the registration-loop writes are *caught*/non-fatal).
- **Top hypothesis (H1):** manual `depth_module.exposure=8500` written via the depth XU
  (`DS5_EXPOSURE`, `d400-device.cpp:776`) while `enable_auto_exposure=true` → firmware 5.15.1.55
  rejects the contradictory depth-XU control → "Hardware Error" at index 0x0300.
- **Minimal config:** depth-only `848x480x30`, AE left ON, **no manual exposure pushed**, no emitter/
  sync/preset/json toggles, `initial_reset:=false`. Run serially with `LRS_LOG_LEVEL=DEBUG`.
- **All of the above is PENDING-HIL.** No hardware was touched; no claim here is verified.
