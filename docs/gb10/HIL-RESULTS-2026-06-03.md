# GB10 HIL Results — 2026-06-03 (post-rebuild, full CUDA + pyrealsense2)

Host `spark-3066`, D435 on `2-1` / `NVDA8000:00` @ USB-3.2 5000 Mbps (own bus). Build:
`build-gb10-full` (RSUSB + `BUILD_WITH_CUDA=ON` + `RS2_GB10_USB_TUNING=1` + pyrealsense2
against the uv venv CPython 3.12.3), CUDA OpenCV 4.14 (`cv2.cuda`, 1 device), numpy 1.26.4.

## P7 re-acquire guard — confirmed on the production-config build
Single-context open under strict `RS2_GB10_REFUSE_REACQUIRE=1`: `create_rsuvc_device` ran
5×, **both sensors enumerated fully** (Stereo Module 104 + RGB Camera 161 profiles), depth
streamed 60 frames, **guard did NOT false-fire**, controller stayed GREEN. (Earlier
minimal-RSUSB build gave the same result with 4× construction.) The guard ships clean in the
CUDA/RSUSB production configuration.

## Advanced single-stream feature stress (SAFE envelope)
`rs-gb10-hil-advanced.py` — single depth 848×480@60, 300 frames after 30-frame warmup.
Per-op latency (ms):

| op | n | mean | p50 | p95 | max | note |
|----|---|------|-----|-----|-----|------|
| acquire (`wait_for_frames`) | 300 | 10.50 | 10.66 | 11.84 | 16.04 | ~60 fps cadence |
| `rs.colorizer` (CUDA) | 300 | 1.53 | 1.53 | 1.60 | 2.01 | stable |
| `rs.pointcloud.calculate` (CUDA) | 300 | 1.80 | 0.98 | 1.62 | **222.3** | first-call init stall |
| cv2.cuda pipeline (upload→resize→8u→download) | 300 | 0.54 | 0.40 | 0.52 | **36.4** | first-frame CUDA warmup |
| post-proc chain (dec+spatial+temporal+hole) | 300 | 3.11 | 3.10 | 3.15 | 4.61 | CPU, stable |

Effective throughput **57.17 fps**, **1 stream gap** over 300 frames. Controller GREEN after.

### New findings / candidate bugs (HIL-surfaced)
1. **CUDA first-call init stall (perf bug).** `rs.pointcloud` max 222 ms (p50 0.98 ms) and the
   cv2.cuda pipeline max 36 ms (p50 0.40 ms) are one-time lazy-CUDA-context / first-allocation
   stalls. For a real-time consumer this is a multi-hundred-ms hitch on the first frame after
   start. **Fix:** pre-warm the CUDA path (run one throwaway `pointcloud.calculate` / one
   `cv2.cuda` upload+op) before the hot loop; aligns with the `88-performance-roadmap`
   upload-once/keep-on-GPU recommendation.
2. **`cv2.cuda.applyColorMap` absent in the CUDA OpenCV 4.14 build (packaging gap).** The harness
   fell back to CPU `applyColorMap`. `cudaimgproc`'s color-map entry point is not compiled into
   `/opt/gb10-cuda/install/opencv`. **Fix:** rebuild that OpenCV with the contrib `cudaimgproc`
   color-map, or keep the CPU colormap (cheap at preview res — matches doc 90's measured result).
3. **60 fps not sustained with the full per-frame CUDA+pointcloud+postproc chain** (57.17 fps,
   1 gap). The chain (colorize+pointcloud+cv2.cuda+4 filters) per frame occasionally exceeds the
   16.6 ms budget. Acceptable for capture+offload; for strict 60 fps, move pointcloud/cv2.cuda
   off the acquire thread (drain-to-newest, keep-on-GPU).
4. **NVENC available** via `cv2.cudacodec` (HEVC writer constructs) — usable for GPU encode.

## Concurrent multi-stream stress
See `forensics/` for any new controller-death set. Result recorded in
`HIL-MULTISTREAM-2026-06-03.md` (eyes-open run on the clean USB-3 bus).

## ROS2
ROS2 Jazzy + colcon present. `realsense2_camera` against this custom SDK: see ROS section of
the multistream doc / open items.

> **Update (2026-06-05):** ROS2 depth stream-start **#26 SOLVED** — minimal config streams 4/4 @ 30 fps,
> 0 drops, controller GREEN; H1 (manual-exposure-under-AE) REFUTED by 8-run live A/B. See
> [ros2-stream-start-analysis.md](ros2-stream-start-analysis.md) for the full analysis and results.
