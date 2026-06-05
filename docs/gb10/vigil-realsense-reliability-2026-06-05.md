# vigil-spark RealSense Reliability / Failure-Mode Analysis on GB10 (2026-06-05)

**Scope:** READ-ONLY analysis of vigil-spark's Intel RealSense usage as it runs on the
DGX Spark GB10. Recommendation document only — `/opt/vigil-spark` is READ-ONLY and was
**never edited**. No camera run, no git, no vigil launch. Each finding is tagged
**[EVIDENCE]** (observed in source, with `file:line`) or **[INFERENCE]** (reasoned from
evidence + the GB10 USB failure model).

**GB10 failure model referenced** (from `realsense-gb10-usb-findings.md`, treated as
established, marked [INFERENCE] where I apply it): on the GB10, a USB transfer error
(`-110` / ETIMEDOUT) followed by a `pipeline.stop()` can trigger the NVIDIA
Stop-Endpoint defect that can wedge or kill the xHCI controller, requiring a host
reboot. "Survival" soaks had zero `-110`; the trigger (firmware vs marginal USB
topology) is not isolated. **The actionable invariant: minimize `stop()`/`start()`
churn and never let two processes open the same device.**

---

## Files in scope (the RealSense surface)

| File | Role | Opens camera? |
|------|------|---------------|
| `vigil_ros_ws/src/sensors/sensors/realsensenode.py` | **The owner.** ROS2 `realsense` executable; one `RealSenseNode` per device, 2 streams each (color 640×480 bgr8@30 + depth 640×480 z16@30), align→color, publishes `/realsense/{color,depth}/image_raw`. | **YES** |
| `vigil_ros_ws/src/vigil_c2/launch/launch_sensors.py` | Launch graph. Launches only `realsense` executable, gated `IfCondition(runRealSense)` default `true`. | n/a |
| `vigil_ros_ws/src/SAM/SAM/{samnode,plotter3dnode,SegmentationThread}.py` | Subscribers. `import pyrealsense2` but **never** open a pipeline (no `.pipeline()`, no `.start()`, no `enable_stream`). | **NO** (safe) |
| `.../UMichProjection/subscriptions/camera_reader/realsense.py` | Standalone `__main__` opener, 2 streams. **Dormant** (not launched). | YES (latent) |
| `.../UMichProjection/ros/local/realsense_reader.py` | Same standalone opener pattern. **Dormant**. | YES (latent) |

---

## Ranked findings

### B1 — `restart_pipeline()` does uncapped `stop()`→`start()` churn on any frame error → GB10 controller-death risk  **[severity: CRITICAL]**

**Evidence chain:**
- `publish_image` wraps `wait_for_frames()` + `align.process()` and catches **any**
  `Exception` (`realsensenode.py:82-95`), sets `__failed=True`, calls
  `restart_pipeline()` and returns.
- `restart_pipeline()` immediately calls `self.__pipeline.stop()` (`:120`), sleeps
  0.5 s, builds a fresh pipeline and `start()`s it (`:123-130`).
- There is **no backoff, no retry cap, no transient-vs-fatal classification.** A single
  transient `wait_for_frames` timeout (the exact symptom a marginal-USB `-110`
  produces) → an unconditional `stop()`.

**GB10 consequence [INFERENCE, from `realsense-gb10-usb-findings.md`]:** the
`-110`-then-`stop()` sequence is precisely the NVIDIA Stop-Endpoint trigger that can
wedge the xHCI controller. If the *restarted* pipeline also faults (likely, if the root
cause is USB topology / a degrading link, not a one-off), every subsequent timer tick
re-enters `publish_image` → re-enters `restart_pipeline` → another `stop()`/`start()`.
The recovery path is itself the most dangerous operation on this platform, and it can
fire repeatedly with no rate limit. **This is the top reliability risk: vigil's
self-heal logic can convert a recoverable frame timeout into a host reboot.**

**Why the existing guards don't cap it:** `__restarting` (`:111`) only prevents
*re-entrant* restarts within a single restart; once `restart_pipeline` returns
successfully (`__restarting=False`, `:136`) the next bad frame starts a brand-new
restart. Nothing counts restarts or widens the interval.

---

### B2 — `__restarting` is never reset on the restart **failure** path (latent permanent-hang)  **[severity: HIGH (latent) / today MEDIUM]**

**Evidence:** in `restart_pipeline`, `__restarting=True` is set at `:114` and reset to
`False` **only** on the success path (`:136`). The `except` branch (`:140-144`) logs,
calls `self.destroy_node()`, and returns **without** resetting `__restarting`.

**Why it's *latent*, not live, today [EVIDENCE]:** `destroy_node()` sets
`__shutdown=True` (`:249`), and `publish_image`'s guard short-circuits on
`__shutdown or __restarting` (`:77`). So on a failed restart the node **self-destructs**
— it does not sit alive spinning with `__restarting=True`. The stuck flag is masked by
full teardown.

**The latent bug [INFERENCE]:** the moment anyone makes restart-failure *non-fatal*
(e.g. "retry instead of `destroy_node`", a very natural future hardening), `__restarting`
stays `True` forever and the node can **never restart again** — `publish_image` no-ops
permanently and the camera goes dark with no log churn. The correct shape is a
`try/finally` that always resets `__restarting`. Flag it now so the fix for B1 (adding
retries) doesn't silently introduce a permanent hang.

---

### B3 — Cross-thread double `pipeline.stop()` during shutdown / failure  **[severity: HIGH]**  *(this is the real race; the intra-node "race" is not one — see note)*

**Evidence:**
- `run()` spins each node on its own `threading.Thread(target=rclpy.spin,...)`
  (`:287`), then `for thread in threads: thread.join()` (`:293-294`), and in the
  `finally` calls `node.destroy_node()` for every node from the **main thread**
  (`:301-303`). `destroy_node()` calls `self.__pipeline.stop()` (`:253`).
- `restart_pipeline`'s `except` calls `self.destroy_node()` from the **spin thread**
  (`:143`) → also `self.__pipeline.stop()`.

**The race [INFERENCE]:** on a failure during shutdown (e.g. KeyboardInterrupt arrives
while a node is mid-restart-failure), the spin thread and the main thread can both call
`destroy_node()`/`pipeline.stop()` on the **same** pipeline object concurrently. There is
no lock around `__pipeline` / `__shutdown` / `__timer`. `destroy_node` swallows
exceptions (`:255-256`) so a double-stop won't crash Python — **but a double / racing
`stop()` is exactly the operation the GB10 defect punishes.** Concurrent stop-endpoint
teardown is strictly worse than a single one.

**Note — the intra-node timer race the brief asks about does NOT exist in vigil's actual
deployment (N=1) [EVIDENCE].** vigil streams a single D435 ("we expect only 1", comment
`:22`), so there is exactly **one** spin thread driving the process-global executor
(`/opt/ros/jazzy/.../rclpy/__init__.py:101-106,243`). With one thread, the timer callback,
restart, and publish are **serialized** — and because `restart_pipeline` is called
**inline inside** the timer callback (`:94`), not as a separate callback, the callback
cannot re-enter itself. So a timer callback calling `wait_for_frames` on a pipeline that
`restart_pipeline` is mid-reassigning **cannot** interleave. The `time.sleep(0.2)` "wait
for any active timer runs to stop" (`:116`) is therefore largely **vacuous** at N=1.
**For N>1 this serialization does NOT hold** — N spin threads pull concurrently from the
one shared executor; that concurrency hazard is B4. Don't claim an intra-node TOCTOU at
N=1; the genuine cross-thread hazard at any N is the `destroy_node` path above.

---

### B4 — All N camera nodes share ONE process-global executor; multi-camera join blocks only on thread[0]  **[severity: MEDIUM (latent; vigil "expects only 1")]**

**Evidence:**
- `get_global_executor()` returns a **single module-global** `SingleThreadedExecutor`
  (`rclpy/__init__.py:101-106`). Every `rclpy.spin(node)` thread (`realsensenode.py:287`)
  adds its node to the **same** executor. With N>1 cameras, **N threads call `spin_once`
  concurrently on one executor** → callbacks across nodes run **concurrently** (the
  "SingleThreaded" name refers to the executor's own internals, not to N external driver
  threads), plus unguarded multi-thread `add_node` on shared executor state. This is a real
  cross-node concurrency hazard, not mere contention.
- `for thread in threads: thread.join()` (`:293-294`) blocks on `thread[0]` (the
  **primary** camera). If a **secondary** camera node self-destructs (B2/B3 path), the
  main loop never notices — it's still joined on the healthy primary → **silent loss of a
  camera, no re-enumeration.** Conversely, primary failure tears down and `main()`
  (`:317-322`) re-runs `run()` for the **whole** set, churning every camera.

**Consequence [INFERENCE]:** the multi-camera model is not robust to per-camera failure;
recovery granularity is "all or the primary only." Mitigated in practice because vigil
streams a single D435 (comment `:22` "we expect only 1"), but it's a real N-camera gap and
interacts badly with B1's churn (one bad camera can restart-churn or tear down siblings).

---

### B5 — Partial-frameset / malformed-frame edge cases  **[severity: MEDIUM]**

- **Partial frameset (color XOR depth):** `publish_image` checks `if not color_frame or
  not depth_frame` (`:87`) and returns — **good**, no publish of a half message. But this
  return path does **not** set `__failed`, so a camera delivering only one stream for a
  long time produces zero published frames and **no escalation to restart** — it silently
  starves downstream SAM. [EVIDENCE]
- **`align.process` on a frameset missing a stream:** `align(color)` is constructed once
  (`:63`). If depth is momentarily absent, `align.process` may raise → caught by the
  broad `except` (`:90`) → **B1 churn**. So a transient missing-depth frame is escalated
  straight to a `stop()`/`start()`. [INFERENCE]
- **`cv2.imencode` failure in `build_color_message`:** `success` is **ignored** (`:152`);
  on encode failure `data` is `None` and `data.tobytes()` (`:172`) raises
  `AttributeError` — caught nowhere in `build_color_message` (it's outside the
  `publish_image` try, which only wraps frame acquisition `:82-95`), so it propagates up
  through the timer callback. Under default executor that **kills the spin loop** for that
  node. Low probability but unguarded. [EVIDENCE]
- **Depth-scale re-read after restart:** `__depthScale` **is** re-read on restart
  (`:131-132`) — **good**. [EVIDENCE]
- **0 devices found:** `run()` loops `time.sleep(30)` re-enumerating (`:263-269`) — fine,
  but blocks forever with no upper bound / no degraded-mode signal. [EVIDENCE]
- **Device disappears mid-run:** surfaces as a `wait_for_frames` exception → B1 churn → on
  GB10 a `stop()` against a vanished device is itself a defect trigger. [INFERENCE]

---

### B6 — Dead code: `encode_depth_jpeg_with_meta`  **[severity: MINOR / cleanup]**

**Evidence:** `encode_depth_jpeg_with_meta` (`:178-210`) has **no caller** anywhere in
the tree (grep across `/opt/vigil-spark`, sole hit is the `def`). The published depth path
is `build_depth_message` (`:215-244`), which sends the **raw** `array('H', …)` (`:224,
:232`), not the JPEG. Also contains an unreachable `return None` after `raise` (`:202-203`).
No reliability impact; noted so the depth-encoding story is unambiguous (vigil ships
**raw uint16 depth**, no lossy depth JPEG in the live path).

---

## The latent 2-process footgun (Brief Q4)

**Concrete reachability [EVIDENCE]:** the two UMich files
(`.../camera_reader/realsense.py:88-110`, `.../ros/local/realsense_reader.py`) are
`if __name__ == '__main__':` scripts that build their own `rs.pipeline()` and `start()` a
color+depth config (`realsense.py:13-35`). They are **not** in `launch_sensors.py` and
`projection_node.py`'s realsense subscriptions are commented out — so under normal vigil
operation they are dormant. **But nothing prevents** an operator/dev from running
`python .../camera_reader/realsense.py` (the preview tool with `cv2.imshow`, `:101-104`)
**while `realsensenode` is already streaming.** That is a second `enable_device` on the
same serial.

**GB10 consequence [INFERENCE]:** a second opener forces a USB re-claim / config conflict
on a device already held by `realsensenode`; the contention manifests as `-110` on one of
them → B1 churn → controller-death risk. This is the single most dangerous *operator*
mistake on this platform and there is currently **no guard** against it.

**Why `gb10-doctor.sh` alone is insufficient [EVIDENCE+INFERENCE]:** the fork already has
`scripts/gb10/gb10-doctor.sh` (a one-shot preflight; checks pyrealsense2 import etc.). A
device-busy check *there* is **TOCTOU** — it only catches an opener that is already
running at the instant doctor fires; the UMich script launched 30 s later sails through.
**Preflight is advisory; it is not the guard.**

---

## >2-stream / multi-stream verdict (Brief Q5)

**Verdict: NO — vigil does not justify enabling riskier >2-stream functionality.
Confirmed negative across the whole tree.** [EVIDENCE]

Proof (grep across `/opt/vigil-spark`, excluding venv/site-packages/qobi):
- **Every** `enable_stream` call (4 total, all in `realsensenode.py` + 2 dormant UMich
  files) enables **exactly two** streams: `color 640×480 bgr8@30` + `depth 640×480 z16@30`.
- **Zero** hits for: `stream.infrared` / `stream.fisheye` / `stream.accel` /
  `stream.gyro` / `stream.pose` (no IR, no IMU, no T265 pose).
- **Zero** hits for `advanced_mode`, `enable_record`, `hdr_enabled`, `emitter`,
  `laser_power` (no advanced-mode, no recording, no HDR, no emitter control).
- **Zero** higher-res dims (`1280×720`, `848×480`) tied to RealSense — the only `1280,720`
  hit is an unrelated CornellTracker GUI canvas size.
- **`align(depth)`: zero hits.** Only `align(color)` is used (`realsensenode.py:63`,
  UMich `:42`) — confirms the conventional 2-stream align-to-color topology.
- **No RealSense post-processing filters** (`decimation_filter` / `spatial_filter` (rs) /
  `temporal_filter` / `hole_filling` / `disparity_transform` / `colorizer`): zero hits.
  (`spatial_filter` matches are unrelated `cv2.filter2D` vein-detection kernels.)

The downstream consumers (SAM aligner, plotter3d) operate purely on the published
color+depth ROS topics — they need no additional camera streams. **Enabling >2-stream
features would *add* USB bandwidth/endpoint pressure on the exact controller the GB10
mitigations are trying to protect, for zero functional benefit to vigil.** Keep the camera
at 2 streams.

---

## Recommended hardening (RECOMMENDATION ONLY — vigil is read-only; the user decides)

Ordered by risk reduction. **(V)** = change in vigil; **(F)** = the GB10 librealsense fork
could absorb it as a reusable, vigil-agnostic safety primitive.

1. **(V) Cap and back off the restart path (fixes B1 — top priority).** Add a retry
   counter + exponential backoff + a max-restart ceiling to `restart_pipeline`. Classify
   the exception first: a single `wait_for_frames` timeout should *retry the read* a few
   times **before** ever calling `stop()`. On GB10 the cheapest safe action is "wait and
   re-read," not "stop/start." After N failed restarts, stop churning and surface a hard
   error rather than looping `stop()` forever.

2. **(F) A safe-stop wrapper in the fork.** Provide a single helper (C++/pybind or a Python
   shim shipped with the GB10 build) that performs `pipeline.stop()` with the GB10
   Stop-Endpoint mitigation sequence (quiesce → guarded stop → settle), is **idempotent**
   and **internally locked** so a double/racing stop (B3) collapses to one. vigil (and any
   consumer) calls *that* instead of raw `pipeline.stop()`. This is the highest-leverage
   fork contribution: it neutralizes B1's and B3's GB10 consequence library-side.

3. **(F) + (V) Single-opener guard — runtime lock, not just preflight (fixes Q4 footgun).**
   - **(F) runtime fix:** a `flock`-based (or `O_EXCL` lockfile) guard keyed on the device
     **serial / USB path**, taken around `pipeline.start()` inside the GB10 build (or a thin
     `pyrealsense2` wrapper the fork ships). A second opener of the same serial fails fast
     with a clear "device already in use by PID …" instead of inducing `-110`. This is the
     *real* guard — it holds regardless of launch order.
   - **(V/F) advisory preflight:** add a device-busy check to `gb10-doctor.sh` (lsof/fuser
     on the USB node, or "is `realsense` executable already running") so operators get an
     early warning. Useful, but explicitly secondary to the runtime lock (TOCTOU).

4. **(V) `try/finally` reset of `__restarting` (fixes B2).** Wrap the restart body so
   `__restarting` is **always** reset on every exit path. Do this *with* item 1 so adding
   retries can't introduce a permanent hang.

5. **(V) Guard the cross-thread teardown (fixes B3).** Make `destroy_node` idempotent (a
   `__destroyed` flag + a lock around `__pipeline.stop()`), so the main-thread `finally`
   and the spin-thread failure path can't double-stop. (Item 2's idempotent safe-stop also
   covers this if adopted.)

6. **(V) Per-camera failure isolation (fixes B4).** Join on **all** threads / detect any
   node death, and restart only the failed camera rather than the whole set or silently
   ignoring a dead secondary. Low priority while vigil runs a single camera.

7. **(V) Escalate partial-frameset starvation (fixes B5).** Treat a sustained
   single-stream / no-frame condition (`:87` path) as a *counted* health signal (not silent
   return), but route it through item 1's capped/backoff logic — **not** straight to
   `stop()`.

8. **(V) Guard `cv2.imencode` success (B5).** Check `success` at `:152` and drop the frame
   on failure instead of `None.tobytes()` crashing the timer callback.

9. **(V) Remove dead `encode_depth_jpeg_with_meta` (B6).** Cleanup only.

**Fork-vs-vigil split summary:** items **2** (safe-stop wrapper) and **3-runtime**
(single-opener flock) are the two reusable safety primitives the GB10 librealsense fork
should own — they protect *any* consumer on this controller, not just vigil. Everything
else is vigil application logic the maintainers own.

---

## Evidence index (file:line)

| ID | Claim | Citation |
|----|-------|----------|
| B1 | broad except → unconditional `stop()`/`start()`, no cap | `realsensenode.py:82-95, 120-130` |
| B2 | `__restarting` reset only on success; except → `destroy_node` | `realsensenode.py:114, 136, 140-144, 249, 77` |
| B3 | double `pipeline.stop()` across threads; no lock | `realsensenode.py:143, 253, 287, 301-303`; `rclpy/__init__.py:101-106, 243` |
| B4 | shared global SingleThreadedExecutor; join on thread[0] | `rclpy/__init__.py:101-106`; `realsensenode.py:287, 293-294, 22` |
| B5 | partial frameset no-escalate; imencode success ignored; depth-scale re-read ok | `realsensenode.py:87, 152, 172, 131-132, 263-269` |
| B6 | dead JPEG-depth helper; raw uint16 published | `realsensenode.py:178-210` (no caller), `224, 232` |
| Q4 | UMich standalone openers reachable; not launched | `.../camera_reader/realsense.py:13-35, 88-110`; `launch_sensors.py:111-119` |
| Q5 | only color+depth; no IR/IMU/advanced/record/filters; align→color only | 4× `enable_stream` (all color+depth); zero hits for IR/IMU/advanced/record/filter grep |
