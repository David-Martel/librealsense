# GB10 librealsense — enhancement targets (usability / reliability / performance), prioritized

Grounded audit of remaining opportunities in the David-Martel GB10 fork, ranked by (impact × low-risk).
Each target cites evidence. "Shipped default" = affects the promoted GB10 build today. Measured items link
the doc that measured them. Updated 2026-06-05 @ master `f38fd3471`.

> **Reliability roadmap & status now live in [gb10-hardening-optimization-proposals](gb10-hardening-optimization-proposals-2026-06-05.md)**
> (ranked H1–H10 + what's landed vs next). Landed + HIL-proven this session: the **single-opener cross-process
> lock** (prevents the 2-process-open death), **H1 safe-stop** (relocates `clear_halt` out of the post-stop
> window), **F5/F6** (atomic `_active`, NDEBUG-safe teardown), and the libusb error-path fixes (`errno`→`sts`).
> CUDA acceleration is now LIVE-validated per-op in [accel-validation](accel-validation-2026-06-05.md)
> (pointcloud cached **1.8×**, align 15–19×, keep-on-GPU 3.43 ms no-D2H; colorize/conversion = honest parity).

## Reliability (highest priority — these affect the promoted default or are known crashes)
| # | target | evidence | fix | effort |
|---|--------|----------|-----|--------|
| R1 | **Cached-pool errors are `assert()`, which `-DNDEBUG` strips in Release** → a failed `cudaMalloc`/`cudaMemcpy` in the now-DEFAULT cached path is **unchecked in production** (silent corruption, not a clean error) | `cuda-pointcloud.cu:40`, `cuda-conversion.cu:45,52` + branches; build sets `-O3 -DNDEBUG`, `CMAKE_BUILD_TYPE=Release` | Replace asserts in the `#if`-guarded cached code with a `cuda_or_throw()` helper (throws `std::runtime_error` w/ `cudaGetErrorString`). Happy-path identical; upstream baseline untouched (still `#if`-guarded → byte-identical) | **XS — DONE this turn** |
| R2 | GL processing-lane teardown SIGSEGV — **✅ FIXED** | static-lane GPU-object dtors freed GL after the context was gone | **Resolved:** call `rs2::gl::shutdown_processing()` while the context is still current, before `glfwDestroyWindow`/`glfwTerminate`. **Verified in the P1 viewer: clean `return 0`, exit code 0, no SIGSEGV** (was the crash). | DONE |
| R3 | P7 REFUSE remediation text swallowed — **✅ DONE** | the throw is caught by `create_usb_device` → app saw a generic "No device connected" | **`LOG_ERROR(advice)` before the throw** in `check_device_reacquire` (device-libusb.cpp) so the remediation is visible regardless of the swallowed exception. Gated by `RS2_GB10_USB_TUNING` (upstream byte-identical); rebuilt clean; P7 path intact (hil-p7 no false-fire, GREEN). | DONE |
| R4 | Cached-pool unit tests — **✅ DONE** | `scripts/gb10/test_cached_pools.cu` + `test-cached-pools.sh` (`just test-cached`) | both pools (pointcloud + conversion) **byte-identical mode0-vs-mode1 across a grow/shrink sequence** (848→1280→424→1280→640: grow, reuse-when-fits, no-shrink, regrow) + mode selection. No camera (GPU only), warning-clean. PASS. | DONE |
| R5 | **2-process open of one camera → xHCI wedge → reboot** (the #1 death cause; e.g. the dormant UMichProjection standalone run while vigil streams) — **✅ DONE** | `device-libusb.cpp` (`08c0360cb`); vigil-realsense-reliability §footgun | **Single-opener cross-process `flock`** keyed on the USB bus-port id, refcounted per process (depth+color share one lock), acquired before `libusb_ref` (no per-retry leak); auto-released on process death. 2nd process queues, never simultaneous. **Live: Test A queue + Test B same-process depth+color, GREEN.** Gated by `RS2_GB10_USB_TUNING`. | DONE |
| R6 | **Stop-path `clear_halt` armed in the post-`-110` teardown window** (what vigil's `restart_pipeline` stop→start hits every cycle) — **✅ DONE (H1)** | `uvc-streamer.cpp:225` (`f38fd3471`); FINDINGS death #3 | **H1 SAFE-STOP:** skip the stop-path `reset_endpoint`; rely on `start()`'s unconditional reset (line 184) — relocates `clear_halt` into the settled start window. **Live: 3× stop→start, frames resume, GREEN.** Upstream byte-identical. | DONE |
| R7 | **`_active` data race + `~usb_context` NDEBUG teardown hang** (deferred audit F5/F6 — the hang is exactly what vigil's restart teardown would hit) — **✅ DONE** | `request-libusb.h`, `context-libusb.cpp` (`8a08572e0`); reliability-audit | F5: `std::atomic<bool>`. F6: kill-flag + `libusb_interrupt_event_handler` before `join()` (gated; libusb 1.0.27). Compile-verified. | DONE |
| R8+ | **Remaining reliability hardening** (next steps) | gb10-hardening-optimization-proposals (H3/H4/H6/H10) | **H4 reconfigure-without-`stop()`** (re-`config` instead of destroy/recreate — directly removes vigil's `stop()→start()` from the recovery loop) is the highest-value next item; H3 wedge-watchdog (fail-fast); H6 bounded URB-drain; H10 P7 counter robustness; a lock fail-fast variant. | **next** |

## Performance (measured or high-surface)
| # | target | evidence | expected | effort |
|---|--------|----------|----------|--------|
| P1 | Keep-on-GPU colorize→render — **✅ DONE (+ enhanced)** | live viewer `rs-gb10-keepongpu-viewer.cpp` (`just hil-keepongpu`): live depth → `gl::colorizer` (GL texture) → drawn straight to the on-screen window, **NO D2H**. Enhanced: **`--record` NVENC GPU-to-disk** (default cq=23/p4, see [nvenc-cq-sweep](nvenc-cq-sweep.md)) + rich per-frame telemetry (ts/domain/exposure/fps/render-p50) + `--stream color`. | **Verified live: NVIDIA GB10/PCIe, render p50 3.4ms, NVENC mp4 written, live auto-exposure metadata, controller GREEN, clean teardown (R2) even with record.** | DONE |
| P1b | **NVENC cq/preset default — ✅ DONE (#32-NVENC)** | [`nvenc-cq-sweep.md`](nvenc-cq-sweep.md): 5-point cq sweep (19/23/26/29/33) × 2 presets (p4/p6); XPSNR + size + realtime-factor. **Knee at cq=23/p4**: 39.1 dB XPSNR-Y, 39% larger than source, 10.9× realtime. cq19→23 costs only 2.1 dB and saves 1.4 MB/7.7s (31%); next step costs 3.1 dB for 26%. p6 saves 3–4% file size at 0.4–0.7 dB lower XPSNR — not worth it. | DONE — `--record` default is `-rc vbr -cq 23 -b:v 0 -preset p4` | DONE |
| P2 | **Scalar post-proc filters have ZERO acceleration** | spatial 499L, temporal 282L, hole-filling 103L, decimation/disparity/threshold — 0 NEON/OMP/CUDA hits | NEON+OpenMP (cross-platform, no GPU copy) — only if a live consumer enables them (opt-in) | M, gated on consumer need |
| P3 | depth-format conversion CUDA path — **✅ MEASURED, don't cache** | `bench_depth_format_cuda.cu`: Y8I (both-IR) is the hot per-frame path (Y12I is cold/calibration); both have the same per-frame `cudaMalloc` churn as YUYV | caching would save ~57–65% kernel-level (~140–283µs/call) BUT end-to-end is expected plumbing-bound/neutral (Finding A) — **verdict: don't cache**; the real lever for depth latency is keep-on-GPU/async (P1/P4), not per-helper caching | DONE |
| P4 | **No async pipelining — ✅ MEASURED, NO-GO** | [`p4-async-pipelining.md`](p4-async-pipelining.md): op already 80–270× camera rate and <2.5% of frame budget; overlap collapses on unified memory at higher res (pointcloud 1280×720: −0.8% efficiency); breaking byte-identical cache contract is pure downside. Conversion shows +58% aggregate throughput at 848×480 but only in offline/batch, not real-time. | DONE — NO-GO; revisit only for offline/batch multi-camera aggregate throughput workload | — |

## Usability
| # | target | evidence | fix | effort |
|---|--------|----------|-----|--------|
| U1 | **Cached-bench mode label cosmetics** — reports `pc_mode: "0"` when the compiled default is now 1 (env unset) | `rs-gb10-pc-zerocopy.py` env-default label | report "(compiled default)" when env unset | XS |
| U2 | **ABI trap** (FindPython grabbing 3.15) | — | **already mitigated** (build script ABI guard, lines 13-19) | DONE |
| U3 | one-command GB10 environment doctor — **✅ DONE** | `scripts/gb10/gb10-doctor.sh` (`just gb10-doctor`) | PASS/WARN/FAIL over toolchain, pyrealsense2 import, cv2 (opencv+ffmpeg libs), CUDA, GL SDK, DISPLAY, NVENC, controller health, camera + USB-3 — no device opened. Robust to `set -o pipefail`+`grep -q` SIGPIPE. HEALTHY. | DONE |

## Other / integration
| # | target | evidence | effort |
|---|--------|----------|--------|
| O1 | ROS2 `realsense2_camera` rebuild vs **Kilted** (newest on 24.04); the L-release on 26.04 | ROS2-GL-PINNED §4 | M |
| O2 | py3.14 SDK build (py3.13 already proven) | build-gb10-py313 verified | S |
| O3 | ROS2 depth-only live HIL — **✅ DONE (#26 SOLVED)** | [`ros2-stream-start-analysis.md`](ros2-stream-start-analysis.md): minimal config streams 4/4 @ 30 fps, 0 drops; H1 (manual-exposure-under-AE) REFUTED by 8-run A/B; fix is the param combination | DONE | — |

## Recommended order (updated — completed items struck)
~~R1~~ → ~~R2~~ → ~~P1/P1b~~ → ~~R3/R4~~ → ~~P3~~ → ~~O3~~ → O1 Kilted ROS2 → O2 py3.14 →
P2 NEON filters (only if a live consumer enables them).
