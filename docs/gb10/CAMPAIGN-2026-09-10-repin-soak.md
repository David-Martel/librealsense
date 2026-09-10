# Campaign plan — fleet re-pin, envelope removal, 720p soak (2026-09-10)

**Authorization.** The account owner explicitly directed, in one message: do the fleet re-pin and
rebuild; do **not** push or file anything to upstream librealsense; ditch the envelope and test both
Sparks against each other; check for newer firmware and update if beneficial; soak 720p at the
highest achievable frame rate; identify modes better suited to vigil-spark (latency, bandwidth,
deployment simplicity, segmentation/clustering/rendering); research ROS 2 / CycloneDDS / networked
topologies / compiler acceleration; produce this plan and then implement it.

This supersedes three self-imposed gates from earlier today: **A7 was gated** (now authorized),
**A10 upstream filing was pending authorization** (now permanently CANCELLED — drafts stay local),
and **"do not touch vigil-spark"** (now scoped-open for the two `ops/` pin scripts only).

---

## Findings that reshape the plan (established before execution)

### F1 — 720p on a D435 is capped at 30 Hz in hardware

`rs-enumerate-devices` on spark-3066, firmware 5.17.3.10:

| Stream | 1280×720 | next tier up in rate |
|---|---|---|
| Depth Z16 | **30**/15/6 Hz | 848×480 @ 90 Hz |
| Color RGB8/BGR8/RGBA8/BGRA8/YUYV | **30**/15/6 Hz | 960×540 @ 60 Hz |
| Infrared 1/2 Y8 | **30**/15/6 Hz | 848×480 @ 90 Hz |

There is no 720p mode above 30 Hz on this SKU, so "720p at the highest framerate possible" resolves
to **1280×720 @ 30 Hz**, and no host-side change can raise it. What host-side work *can* deliver is
**sustaining all four streams at 720p30 concurrently, indefinitely, with zero drops and zero xHCI
faults** — a config heavier on the wire than the June killer (848×480@60 D+C+IR). That is the soak.

**960×540 @ 60 Hz** is the answer if vigil-spark ever needs >30 fps at 16:9; it is recorded as a
mode recommendation, not as a substitute for the 720p soak.

### F2 — the fleet is already on the newest published firmware; there is nothing to flash

Both cameras report **5.17.3.10**. The vendor's D400 firmware release page lists 5.17.3.10 (June
2026, SDK 2.58.1) as the newest release for the D435, with 5.17.0.10 before it. The newer version
constants that appear in the SDK source are **not** D435-USB releases:

| Constant in source | Gate | Applies to our D435 (`8086:0b07`)? |
|---|---|---|
| `5.17.4.13` (`d400-device.cpp:777`, `d400-color.cpp:374`) | `_is_mipi_device && _pid == RS401_GMSL_PID` | no — GMSL/MIPI only |
| `5.17.3.151` (`d400-factory.cpp:100`) | D401 GMSL dual-RGB | no |
| `5.17.3.15` (`d400-color.cpp:217`) | `_is_mipi_device` + d4xx driver ≥ 1.0.4.9 | no |
| `5.17.3.13` (`d400-device.cpp:1049`) | `RS2_OPTION_READOUT_SHAPING`, pid ∈ {D405, D455, D457, **D435i**, D401-GMSL} | no — plain D435 is not in the list |
| `5.17.3.20` (`d400-device.cpp:948`) | depth AE mode, global-shutter, non-D455 SKUs | **would apply**, but no such image is published |

Two of these are worth recording as *wanted* features rather than available ones:
`READOUT_SHAPING` ("higher slows readout to avoid dropped frames") is precisely the knob a 720p
all-stream soak would want, and it is unavailable on this SKU at any firmware; depth AE mode needs
5.17.3.20, which is not downloadable.

**Decision: no flash.** Flashing is a double USB re-enumeration through DFU, and on GB10 a
re-enumeration is the documented trigger surface for controller-death #2. Taking that risk to
install the version already installed is strictly negative. The "power cycle the ports and restart"
step therefore reduces to restarting the affected services, which the re-pin does anyway.

### F3 — the envelope is documentation and script defaults, not a code guard

`grep -rn envelope src/` finds no refusal path; the only runtime guard near it is
`RS2_GB10_REFUSE_REACQUIRE` (`src/usb-tuning.h:199`), which is about re-acquiring a device, not
stream count, and is advisory by default. The envelope lives in `docs/gb10/*`, `scripts/gb10/README.md`,
and the `ros2-launch-depth-{only,minimal}.sh` profiles. Removing it is a documentation and
launch-profile change, gated on the soak below — not a code change.

### F4 — the two Sparks are not topologically equivalent, and the difference is the right control

| | spark-3066 | spark-0060 |
|---|---|---|
| sysfs path | `6-1` | `2-1.1` |
| upstream of the camera | root port directly | `2109:0211` VIA Labs "USB3.0 Hub", 1 downstream port |
| xHCI controller | `NVDA8000:02` (bus 6) | `NVDA8000:00` (bus 2) |
| root port rate | 20 Gbps (`20000M/x2`) | 20 Gbps (`20000M/x2`) |
| negotiated device rate | 5 Gbps SuperSpeed | 5 Gbps SuperSpeed |

The account owner states 0060's camera is "connected to the spark-bus", not behind a hub. The sysfs
chain does show a VIA Labs VL2109 between the root port and the camera. Both readings are recorded
here without adjudicating whether that hub is on the mainboard, in a captive cable, or in an
adapter — it was not opened or traced physically. **The practically important facts are that both
cameras negotiate the same 5 Gbps SuperSpeed link, so the bandwidth ceiling is identical, and that
they hang off different xHCI controller instances** — which is the variable that matters for a
controller-death defect and is what the two-host comparison actually controls for.

Earlier notes calling 0060 "behind a hub, therefore unvalidated" over-weighted the hub and
under-weighted the controller instance. Corrected here.

### F5 — the consumer surface is one matched pair, so the ABI gate was over-stated

B7 was run as an `ldd` sweep rather than a source grep, over `~/dev/releases/**`, `/opt/vigil/**`,
`/usr/local/lib` and the vigil-spark checkout on both Sparks. Results:

- **No ROS 2 `realsense2_camera` node exists on either Spark.** vigil-spark drives the camera through
  `pyrealsense2` directly (`realsensenode.py`), not through the official ROS wrapper. So there is no
  separately-compiled C++ consumer to re-link.
- **The only thing linking `librealsense2` is `pyrealsense2` — and the copy that actually loads lives
  *inside the prefix itself*.** Confirmed against the live process on spark-0060 (`/proc/<pid>/maps`):
  it has `…/librealsense-v2.58.1-dgx-spark-gb10/lib/librealsense2.so.2.58.1` and that same prefix's
  `pyrealsense2…so.2.58.1` mapped, selected by `PYTHONPATH` + `LD_LIBRARY_PATH`.

That last point is what shrinks A7. The mid-enum removal in 2.58.4 can only bite when two
*separately compiled* modules disagree about an enum's integer value. Here the binding and the
library are built together, shipped together in one prefix, and selected together by one pair of
environment variables — they cannot disagree. Re-pinning swaps a **matched pair** for a matched pair.

Two corollaries:

- **The real pin is `/etc/profile.d/vigil-realsense-gb10.sh`**, written by vigil-spark's
  `ops/deploy_gb10_realsense.sh` (`PREFIX_DEFAULT` at line 25), which sets `PYTHONPATH` and
  `LD_LIBRARY_PATH`. `/usr/local/lib/librealsense2.so` is a secondary alias, not the mechanism the
  running node uses. Earlier notes in this repo that framed the re-pin as "flip
  `/usr/local/lib/librealsense2.so`" named the wrong lever; flipping only that would leave the node
  on 2.58.1.
- **`vigil-spark/qobi/pyrealsense2/` is dead weight and cannot load at all.** Its active symlinks
  select 2.57.4 bindings whose `DT_NEEDED` is `librealsense2.so.2.57`, and no such library exists
  anywhere on either Spark (`ldconfig -p` count 0, filesystem search empty) — `ldd` reports
  `librealsense2.so.2.57 => not found`. Its own `PROVENANCE.md` already calls it "a temporary
  compatibility bridge" and directs new work at a reproducible build from this fork. It is not on
  the live worker's `PYTHONPATH`, so nothing is broken today; it is a trap for whoever adds it.
  Flagged to the vigil-spark owner, not deleted from here.

---

## Plan

Ordered so that firmware/hardware state is settled before measurement, and the re-pin validates a
**soaked** artifact rather than preceding the soak.

| # | Item | Host | Gate to proceed |
|---|---|---|---|
| **B1** | Bus claims + stop-notice for the unit holding 0060's camera | — | posted, no objection |
| **B2** | Firmware inventory and decision | both | **done — F2, no flash** |
| **B3** | Add 720p entries to `rs-gb10-stress-matrix.py`; commit | repo | matrix runs headless |
| **B4** | 720p30 D+C+IR1+IR2 short baseline | 3066 | streams start, fps ≥ 29 |
| **B5** | **≥60 min soak** at 720p30 all-streams, kernel tripwire armed, in tmux | 3066 | 0 faults, 0 drops |
| **B6** | Same ladder on 0060 after stopping the fleet unit | 0060 | 0 faults; compare to B5 |
| **B7** | Consumer inventory by `ldd`, not grep — every binary linking `librealsense2.so.2.58` | both | list is complete |
| **B8** | Rebuild consumers against 2.58.4 (`realsense2_camera` + `pyrealsense2`) | both | build clean |
| **B9** | vigil-spark PR: bump `ops/build_gb10_realsense.sh` (2.58.3→2.58.4) and `ops/deploy_gb10_realsense.sh` (2.58.1→2.58.4) | vigil-spark | claimed, no objection |
| **B10** | Flip `/usr/local/lib/librealsense2.so` to the 2.58.4 prefix | both | B5+B6+B8 pass |
| **B11** | Restart the 0060 fleet unit with its recorded `ExecStart`; verify topics stream | 0060 | `ros2 topic hz` healthy |
| **B12** | ROS 2-level soak on the rebuilt node | 3066 | 0 faults |
| **B13** | Retire the envelope in docs + launch profiles | repo | B5+B6+B12 pass |
| **B14** | Modes / ROS 2 / Cyclone / compiler research → measured recommendations | — | runs during B5/B6 |

### Rollback

Every prefix is side-by-side under `/opt/vigil/opt/`; the 2.58.1 prefix is untouched throughout, so
B10 is reverted by pointing the symlink back. Firmware is not modified, so there is no firmware
rollback to plan. The 0060 fleet unit's full `ExecStart` is recorded in the bus claim and in B11.

### Explicitly out of scope

- Any push, issue, or PR to upstream librealsense — permanently cancelled by direction.
- Host reboots. If an xHCI controller wedges and a driver-level rebind does not recover it, that is
  a stop-and-ask, not a reboot.
- Any vigil-spark file other than the two `ops/` pin scripts.

---

### F6 — the shipped SDK is compiled for **baseline armv8-a**, not Armv9.2

Probed on spark-3066 (read-only) while the soaks ran:

```
$ gcc --version              → gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
$ lscpu | grep 'Model name'  → Cortex-X925
$ gcc -mcpu=native -Q --help=target | grep -E 'march=|mcpu=|mtune='
  -march=            (empty)
  -mcpu=             (empty)
  -mtune=            (empty)
$ gcc -mcpu=cortex-x925 -c t.c
  cc1: error: unknown value 'cortex-x925' for '-mcpu'
```

**GCC 13.3 does not know Cortex-X925**, and `-mcpu=native` on an unrecognised part resolves to
nothing at all rather than erroring — so it silently compiles to the aarch64 default baseline.
`scripts/build-dgx-spark-gb10.sh:27-30` already warns about exactly this; what had not been checked
is which flag the *shipped* artifacts actually carry:

| Prefix | `CMAKE_CXX_FLAGS_RELEASE` | Effective ISA |
|---|---|---|
| `…-v2.58.1-…` (**currently pinned on both Sparks**) | `-O3 -DNDEBUG -mcpu=native …` | **baseline** |
| `…-v2.58.4-…` (**the prefix this campaign pins to**) | `-O3 -DNDEBUG -mcpu=native …` | **baseline** |
| `…-v2.58.1-…-py313-rpath-v4l2` | `-mcpu=neoverse-v2` | Armv9 |
| `…-v2.58.3-…-py312` | `-mcpu=cortex-x925` | (flag this GCC rejects) |

So both the incumbent and the replacement are baseline builds: no SVE2, no BF16, no I8MM, no Armv9.2
baseline. The script's own `LRS_GB10_REPRODUCIBLE=1` path sets
`-march=armv9.2-a+sve2+bf16+i8mm -mtune=neoverse-v2`, and this GCC **accepts** that combination
(verified by compiling with it). GCC 13.3 also accepts `-mcpu=grace` and `-mcpu=neoverse-v2`.

This is the largest untaken acceleration opportunity found in this campaign, and it is orthogonal to
everything else here — it changes codegen for every hot path (align, pointcloud, colorize, the YUY2
converter) without touching a line of source.

**Sequenced after the re-pin, deliberately**: the re-pin swaps a baseline 2.58.1 for a baseline
2.58.4, so it changes one variable. Rebuilding with Armv9.2 flags is then a second, separately
measurable change, A/B'd against the baseline prefix with `scripts/gb10/bench-filters.sh` and the
profiler, and byte-identity gated before it is pinned. Doing both at once would make a regression
unattributable.

---

## Mode recommendations for vigil-spark

vigil-spark's ROS node hardcodes **640×480@30 depth + colour**
(`src/sensors/sensors/realsensenode.py:797-798`) and already negotiates `bgr8` with a `yuyv`
fallback. Everything below is measured on this hardware unless marked otherwise.

### Wire arithmetic (corrected)

The D435 colour sensor transmits **YUY2 at 2 bytes/pixel** and the SDK converts host-side to
RGB8/BGR8/RGBA8/BGRA8 (`d400-color.cpp:342` → `device.cpp:255`). Depth is Z16 at 2 B/px, IR is Y8 at
1 B/px. So:

| Config | Wire bytes/s | % of a 5 Gbps SuperSpeed link (≈500 MB/s practical) |
|---|---:|---:|
| 640×480@30 D+C **(deployed today)** | 36.9 MB/s | ~7% |
| 848×480@30 D+C | 48.9 MB/s | ~10% |
| **1280×720@30 D+C** | 110.6 MB/s | ~22% |
| 1280×720@30 D+C+IR1+IR2 **(soaked)** | 165.9 MB/s | ~33% |
| 960×540@60 D(848@60)+C | 118.1 MB/s | ~24% |

The deployed configuration uses about **7%** of the link. The heaviest thing measured here uses
about a third and dropped no frames on either host.

### Recommendations, in the order they are worth doing

| # | Change | Why | Cost |
|---|---|---|---|
| **M1** | **640×480 → 1280×720 @30, depth + colour** | 3× the pixels for identification/segmentation/clustering. Measured 29.98/30 fps, 0 drops, 0 USB faults on **both** hosts. Still only ~22% of the link. | Two literals in `realsensenode.py`. Downstream models must accept the larger frame. |
| **M2** | **Request `yuyv` rather than `bgr8`** *when the consumer can take it* | Skips the `yuy2_converter` pass per frame. The node already has the fallback wired, so this is a preference flip. **Not a bandwidth saving** — the wire is YUY2 either way. | Frees CPU only; unmeasured at 30 Hz (see caveat). |
| **M3** | **Add IR1 (+IR2) only if a consumer uses them** | Proven safe at 720p30 alongside D+C. Stereo IR is the honest input for depth-quality work. | +55 MB/s. No benefit unless consumed. |
| **M4** | **960×540@60 for >30 fps colour** | The **only** 16:9 colour mode above 30 Hz on this SKU. Use this, not 720p, if motion is the constraint. | Lower resolution than 720p. |
| **M5** | **848×100@300 or 256×144@300 depth for a latency-first path** | 300 Hz depth exists. If a controller needs fast depth rather than detailed depth, this is a different operating point entirely. | Tiny frames; a separate pipeline. |
| **M6** | **Rebuild with Armv9.2 flags** (F6) | Both the incumbent and replacement SDK are baseline armv8-a. Affects every hot path. | Rebuild + A/B + byte-identity gate. |

**Caveat on M2**, stated because it has not been measured: at 720p30 the conversion does not show up
in delivered frame rate (bgr8 29.98 vs yuyv 29.95 on 3066; 29.98 vs 29.98 on 0060), so the argument
for it is CPU headroom, which was *not* instrumented here. Do not adopt M2 on a throughput claim.

### ROS 2 / CycloneDDS — what is already right, and what is missing

Checked on the fleet rather than assumed:

- **`net.core.rmem_max` = 16 MB on the Sparks** (`rmem_default` and `wmem_max` likewise). The ROS 2
  DDS-tuning guide asks for ≥8–10 MB against a Linux default of 208 KB. **This is already done** —
  do not "fix" it.
- The fleet's Cyclone config (`ops/gb10-validation/qsfp_cyclone.xml`) pins the QSFP p2p interface,
  disables multicast and lists the two peers explicitly. That is the right shape for a two-host
  200 GbE fabric.
- **Missing, and the documented levers for large image topics**: `<Internal><SocketReceiveBufferSize>`,
  `<MaxMessageSize>` and `<FragmentSize>`. Also absent is any `<SharedMemory>`/iceoryx section, which
  is the on-host zero-copy path for `sensor_msgs/Image`.
- **These only matter if image topics actually cross the DDS boundary.** The RealSense node publishes
  on the host that owns the camera; whether frames traverse the fabric depends on where the
  subscribers run, which was not established here. **Measure that before tuning it** — otherwise this
  is optimisation of a path that may carry nothing.

Sources for the DDS levers: [ROS 2 Jazzy DDS tuning](https://docs.ros.org/en/jazzy/How-To-Guides/DDS-tuning.html),
[rmw_cyclonedds shared-memory support](https://github.com/ros2/rmw_cyclonedds/blob/rolling/shared_memory_support.md).
