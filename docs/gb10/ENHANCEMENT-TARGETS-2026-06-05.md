# GB10 librealsense — enhancement targets (usability / reliability / performance), prioritized

Grounded audit of remaining opportunities in the David-Martel GB10 fork, ranked by (impact × low-risk).
Each target cites evidence. "Shipped default" = affects the promoted GB10 build today. Measured items link
the doc that measured them. Updated 2026-06-05 @ master f1abbcb8c.

## Reliability (highest priority — these affect the promoted default or are known crashes)
| # | target | evidence | fix | effort |
|---|--------|----------|-----|--------|
| R1 | **Cached-pool errors are `assert()`, which `-DNDEBUG` strips in Release** → a failed `cudaMalloc`/`cudaMemcpy` in the now-DEFAULT cached path is **unchecked in production** (silent corruption, not a clean error) | `cuda-pointcloud.cu:40`, `cuda-conversion.cu:45,52` + branches; build sets `-O3 -DNDEBUG`, `CMAKE_BUILD_TYPE=Release` | Replace asserts in the `#if`-guarded cached code with a `cuda_or_throw()` helper (throws `std::runtime_error` w/ `cudaGetErrorString`). Happy-path identical; upstream baseline untouched (still `#if`-guarded → byte-identical) | **XS — DONE this turn** |
| R2 | GL processing-lane teardown SIGSEGV — **✅ FIXED** | static-lane GPU-object dtors freed GL after the context was gone | **Resolved:** call `rs2::gl::shutdown_processing()` while the context is still current, before `glfwDestroyWindow`/`glfwTerminate`. **Verified in the P1 viewer: clean `return 0`, exit code 0, no SIGSEGV** (was the crash). | DONE |
| R3 | P7 REFUSE remediation text swallowed — **✅ DONE** | the throw is caught by `create_usb_device` → app saw a generic "No device connected" | **`LOG_ERROR(advice)` before the throw** in `check_device_reacquire` (device-libusb.cpp) so the remediation is visible regardless of the swallowed exception. Gated by `RS2_GB10_USB_TUNING` (upstream byte-identical); rebuilt clean; P7 path intact (hil-p7 no false-fire, GREEN). | DONE |
| R4 | Cached-pool unit tests — **✅ DONE** | `scripts/gb10/test_cached_pools.cu` + `test-cached-pools.sh` (`just test-cached`) | both pools (pointcloud + conversion) **byte-identical mode0-vs-mode1 across a grow/shrink sequence** (848→1280→424→1280→640: grow, reuse-when-fits, no-shrink, regrow) + mode selection. No camera (GPU only), warning-clean. PASS. | DONE |

## Performance (measured or high-surface)
| # | target | evidence | expected | effort |
|---|--------|----------|----------|--------|
| P1 | Keep-on-GPU colorize→render — **✅ DONE (+ enhanced)** | live viewer `rs-gb10-keepongpu-viewer.cpp` (`just hil-keepongpu`): live depth → `gl::colorizer` (GL texture) → drawn straight to the on-screen window, **NO D2H**. Enhanced: **`--record` NVENC GPU-to-disk** + rich per-frame telemetry (ts/domain/exposure/fps/render-p50) + `--stream color`. | **Verified live: NVIDIA GB10/PCIe, render p50 3.4ms, NVENC mp4 written, live auto-exposure metadata, controller GREEN, clean teardown (R2) even with record.** | DONE |
| P2 | **Scalar post-proc filters have ZERO acceleration** | spatial 499L, temporal 282L, hole-filling 103L, decimation/disparity/threshold — 0 NEON/OMP/CUDA hits | NEON+OpenMP (cross-platform, no GPU copy) — only if a live consumer enables them (opt-in) | M, gated on consumer need |
| P3 | depth-format conversion CUDA path — **✅ MEASURED, don't cache** | `bench_depth_format_cuda.cu`: Y8I (both-IR) is the hot per-frame path (Y12I is cold/calibration); both have the same per-frame `cudaMalloc` churn as YUYV | caching would save ~57–65% kernel-level (~140–283µs/call) BUT end-to-end is expected plumbing-bound/neutral (Finding A) — **verdict: don't cache**; the real lever for depth latency is keep-on-GPU/async (P1/P4), not per-helper caching | DONE |
| P4 | **No async pipelining** (single synchronous call per frame) | pinned-mem experiment showed copies aren't the bottleneck; sync IS | double-buffer + CUDA streams to overlap H2D(N+1)/compute(N) — bigger restructure, real for high-fps multi-op | L |

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
| O3 | ROS2 depth-only **live** HIL through the node (camera, serialized) | launch ready | S |

## Recommended order
R1 (done) → R2 (unblocks P1) → P1 keep-on-GPU integration → R3/R4 → P3 → O3 ROS2 live HIL → O1 Kilted →
P2 filters (if a consumer needs them) → P4 async pipelining (largest, last).
