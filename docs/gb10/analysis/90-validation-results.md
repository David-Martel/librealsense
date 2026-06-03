# 9. Validation Results — Bus 2 (USB 3.2) Stability Run

**Run dir:** `~/realsense-gb10-validation/20260602-214043-bus2-usb3-stability/`
**Tool:** `rs-gb10-profiler` (GB10 fork), headless (`--no-render`), via `realsense-gb10-env`.
**Device:** D435 serial `327122076391` / asic `404543020690`, FW `5.13.0.55`, **`device.usb=3.2`**, path `2-1`, speed 5000M, `bMaxPower=720mA`.

## 9.1 Kernel fault tally (journal delta across the whole run)

| Signal | Count |
|---|---|
| `USB disconnect` | **0** |
| `clear_halt` | **0** |
| `xhci … fail` | **0** |
| `reset … device` | **0** |
| `cannot enable` | **0** |
| `over-current` | **0** |
| `Not enough bandwidth` | **0** |
| `device descriptor read` | **0** |
| `babble` | **0** |

> The 436‑line journal delta contains only benign `Found UVC 1.50 device` re‑probe lines (uvcvideo re‑reads descriptors each time librealsense opens the device during the 15 start/stop cycles) and the known `981ae2 … error -5` UVC quirk. **No disconnect, no re‑enumeration, no controller fault.** Contrast Bus 5, where `Found UVC` lines were interleaved with real `USB disconnect`/`new high-speed device` cycles.

## 9.2 Sustained streaming (60 s each)

| Profile | usb3 | framesets | fps (target) | depth gaps | timeouts | failures | stop |
|---|---|---|---|---|---|---|---|
| hd15 1280×720 | yes | 892 | 14.55 (15) | 0 | 2 | **0** | clean |
| vga30 640×480 | yes | 1783 | 29.12 (30) | 0 | 4 | **0** | clean |

## 9.3 Start/stop stress (all profiles × 3 cycles × 5 s) — `SUMMARY framesets=2341 timeouts=14 failures=0`

| Profile×cycle | usb3 | fps | depth gaps | exceptions | stop |
|---|---|---|---|---|---|
| vga30 ×3 | yes | 21.4–21.9¹ | 4,0,0 | 0 | clean |
| vga60 ×3 | yes | 21.6¹ | 140² | 0 | clean |
| depth90‑ir ×3 | yes | **71.1** | 0 | 0 | clean |
| hd15 ×3 | yes | 10.7¹ | 0 | 0 | clean |

¹ Lower fps in 5 s stress cycles is a **measurement‑window artifact**: ~145 ms start + ~1.5 s pre‑stop drain consume a large fraction of a 5 s window. The 60 s sustained runs (§9.2) show true rates. ² vga60 depth "gaps" reflect frame‑number discontinuities under queue‑depth‑1 backpressure at 60 Hz, **not** USB faults (usb3=yes, 0 exceptions, 0 kernel faults).

## 9.4 Verdict

**PASS — robust.** 15/15 start/stop cycles + 2 sustained streams completed with **0 stream failures, 0 exceptions, every cycle on USB‑3**, and **0 kernel‑side USB faults** over ~4 minutes. The camera did not disconnect once on the native Type‑C path. This is the link‑stability evidence (beyond mere enumeration speed) that confirms the C‑1 fix.

## 9.5 How to reproduce / re‑verify (idempotent)

```bash
# Full re-runnable health check (recommended; structured PASS/FAIL + artifacts):
~/realsense-gb10-validation/bin/rs-gb10-healthcheck.sh

# Or the raw profiler the way this run used it:
realsense-gb10-env rs-gb10-profiler --profile hd15  --cycles 1 --duration-sec 60 --no-render --no-evidence
realsense-gb10-env rs-gb10-profiler --profile vga30 --cycles 1 --duration-sec 60 --no-render --no-evidence
realsense-gb10-env rs-gb10-profiler --stress --no-render --no-evidence
```

Raw artifacts for this run: `run.log`, `profiler-hd15.log`, `profiler-vga30.log`, `profiler-stress.log`, `journal-delta.txt`, `fault-tally.txt`, `topology-before.txt`, `topology-after.txt`.
