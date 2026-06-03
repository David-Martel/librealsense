# GB10 Multi-stream HIL — 2026-06-03 (eyes-open, current full config)

Host `spark-3066`, D435 on `2-1` / `NVDA8000:00` @ USB-3.2 5000 Mbps (clean native bus, no
dock). Build `build-gb10-full`: **RSUSB + CUDA + `RS2_GB10_USB_TUNING=1`** (P2/P3/P4/P7
active), pyrealsense2 against uv venv 3.12.3. These runs were authorized eyes-open (each
could have killed the controller; it did not).

## Confound notice (read before drawing conclusions)
Four things differ from the runs that killed the controller (incidents #1–#3), so **no
single cause is isolated** by these results:
1. **Topology**: clean USB-3 dedicated bus now vs dock-contended / USB-2 chain then.
2. **Mitigations**: P2/P3/P4/P7 active now; not then.
3. **Backend**: RSUSB now; incident #3 was native V4L2.
4. **Build/thermal/reboot state**: fresh build, freshly rebooted.
The outcome moved (survival) and four variables moved — do **not** credit any one (e.g.
"topology") as *the* cause. The honest statement is "**under the current full configuration,
these configs survived short runs**."

## Results

| Test | Config | Frames | Result | Controller |
|------|--------|--------|--------|-----------|
| dual + align | depth+color 848×480@60, `rs.align(depth→color)` per frame | 300 | 59.53 fps, 0 gaps, align 0.4 ms mean | **GREEN** |
| churn ×4 (WARN) | destroy+recreate context/pipeline, dual stream, ×4 cycles | 4 cycles | all 4 streamed | **GREEN** |
| 4-stream | depth+color+IR1+IR2 848×480@60 (≥ incident-#1 load) | 300 | survived (fps not captured — see caveat) | **GREEN** |

## The mechanistic finding (the part that is grounded)
**All surviving runs produced ZERO `-110` control-transfer timeouts** (kernel journal grep
over the full 14:41–14:45 window = 0). The only kernel noise was a benign `-5`/EIO permanently
disabling one optional ROI control — which also appears in *healthy* single-stream runs — plus
UVC "unknown format" warnings. By contrast, **every controller death (#1/#2/#3) was preceded by
a `-110` storm at stream setup** (incident #1: `-110` storm → "Not enough bandwidth for
altsetting 0"; incident #3: two `-110`s during dual setup → Stop-Endpoint → HC died).

This is consistent with — and only *consistent with*, not proof of — the model: **the GB10
death requires a control-path `-110`; the Stop-Endpoint is only fatal when issued after such an
error. Absent any `-110`, these multi-stream configs are survivable.** Why no `-110` occurred
here (clean USB-3 bandwidth headroom? mitigations? backend? thermal?) is the unresolved,
confounded question above.

## P7 re-acquire guard — proven in-situ (this result IS clean / confound-free)
The death-#2 churn pattern (destroy+recreate context between streams), run both ways:
- **WARN (default):** all 4 context-recreate cycles streamed; controller GREEN.
- **`RS2_GB10_REFUSE_REACQUIRE=1`:** cycle 0 streamed; cycles 1–3 were **refused** (the device
  re-acquire after a full release is blocked). P7 correctly fires on the real churn pattern and
  halts the loop.
- **Usability caveat:** under REFUSE the P7 `throw` is swallowed by `create_usb_device`'s
  `catch` (`enumerator-libusb.cpp`), so the app sees **"No device connected"**, not the P7
  remediation text. REFUSE is a blunt hard-stop (camera effectively vanishes on re-acquire);
  **WARN remains the recommended default** (the advisory text is visible in the log and the app
  keeps working).

## Envelope guidance (UNCHANGED — deliberately conservative)
The shipped/recommended safe envelope remains **single high-rate stream** for
production-critical use. These results are a **strong preliminary signal** that the current
clean-USB-3 + mitigated configuration tolerates multi-stream, but they do **not** earn an
envelope relaxation:
- Runs were ~5 s each; the deaths happened at *setup* (which these passed) but a multi-minute
  soak / thermal drift was not tested.
- The failure is asymmetric and field-visible (a wrong "multi-stream is fine" → a dead
  controller in VIGIL production needing a reboot on an enterprise box).
- The result has not been reproduced on the V4L2 production-candidate backend.

**To earn a wider envelope** (future work): a multi-minute soak + repeated start/stop cycling
at the dual/3-stream config, on **both** RSUSB and V4L2, on this clean bus, demonstrating
sustained zero `-110` and zero Stop-Endpoint events. Until then: single-stream for anything
that must not fail; multi-stream only for eyes-open testing.

## Caveats / new minor bugs
- **4-stream fps not captured** — the inline test reported frame count but not rate; if it
  silently throttled it would not have saturated the bus, weakening that one row. The `-110`=0
  finding holds regardless (no trigger armed).
- Under REFUSE, P7 surfaces as "No device connected" (see usability caveat above) — a candidate
  improvement is to propagate a distinct error code/message through `create_usb_device`.
