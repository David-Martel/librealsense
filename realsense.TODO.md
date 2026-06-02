# RealSense DGX Spark GB10 TODO

## Scope

This branch targets NVIDIA DGX Spark / GB10 class ARM64 machines running the
D435/D400 RealSense stack through the librealsense RSUSB backend. The goal is a
measured, side-by-side optimized SDK build that preserves the known-good
`/usr/local` install while exposing more SDK tools, examples, wrappers, and
accelerated processing paths from an isolated prefix.

## Local Findings

- Current local SDK source is `v2.58.1` at commit
  `bf2778061d5dd29776e9aca8765f75852671760b`.
- Current installed SDK is `/usr/local/lib/librealsense2.so.2.58.1`.
- Current build is a Release RSUSB build with `-O3 -DNDEBUG`, NEON, CPU
  extensions, tools, ROS bag support, ccache, and async logging.
- Current build does not enable CUDA, OpenMP, Python bindings, graphical
  examples, OpenCV examples, PCL examples, pointcloud stitching, or OpenNI2
  bindings.
- The system is ARM64 with NVIDIA GB10, CUDA 13.2, driver 580.159.03, compute
  capability 12.1, `nvidia_uvm`, C2C mode, ATS addressing, and shared CPU/GPU
  memory characteristics.
- The RealSense D435 was seen as USB3-capable and firmware `5.17.0.10`, but
  runtime reliability depends on USBGuard authorization, cable/hub quality, and
  avoiding default hardware resets.
- The installed `realsense2.pc` incorrectly points at an x86_64 libdir on this
  ARM64 machine. The source templates now use `@CMAKE_INSTALL_LIBDIR@`.
- Current Python bindings are inconsistent: local vendored bindings target older
  SDK sonames, while a matching `2.58.1` binding exists only in a uv cache.

## Build Proposal

Build and validate an isolated GB10 SDK prefix:

```bash
scripts/build-dgx-spark-gb10.sh all
```

Default prefix:

```text
/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10
```

Default build directory:

```text
/opt/vigil/build/librealsense-v2.58.1-dgx-spark-gb10
```

Default feature set:

- RSUSB backend for newer kernel compatibility without RealSense kernel patches.
- CUDA enabled for SDK align, pointcloud, and conversion paths.
- CUDA architecture `121` for GB10, with fallback to `120` if NVCC rejects
  explicit compute capability 12.1.
- ARM native release flags: `-O3 -DNDEBUG -mcpu=native`.
- NEON and librealsense CPU extensions.
- OpenMP enabled for CPU-side parallel paths.
- Tools, examples, graphical examples, GLSL extensions, OpenCV examples, PCL
  examples, pointcloud stitching, OpenNI2 bindings, Python bindings, ROS bag2,
  ccache/sccache, and async logging.
- External LZ4 remains disabled by default because Ubuntu `liblz4-dev` does not
  provide the `lz4Config.cmake` package file expected by this build path.
- OpenNI2 remains enabled for compatibility testing. The upstream wrapper needed
  a Linux include-path repair so Ubuntu ARM64 can find `Driver/OniDriverAPI.h`.
- Compile caching should use one launcher. The GB10 script prefers `sccache` and
  disables librealsense's legacy `ENABLE_CCACHE` wrapper when that launcher is
  set.
- DDS support is enabled by default for this GB10 branch after installing the
  local FastDDS tooling. Disable it with `LRS_GB10_WITH_DDS=OFF` only if the
  middleware surface or build time becomes a problem.

## Performance Work

- Establish baseline throughput from the existing `/usr/local` SDK before
  switching any application code.
- Compare baseline, GB10 CUDA, and optional OpenMP/LTO variants on identical
  profiles.
- Keep CUDA as a benchmark-gated optimization. Librealsense CUDA paths still
  copy frames between host and device buffers; GB10 C2C/ATS and unified memory
  should reduce the penalty, but copies and synchronization can still dominate
  640x480@30 workloads.
- For downstream GPU consumers, upload each color/depth frame once, then keep
  resize, preprocessing, inference, pointcloud, and encoding operations on GPU.
- Use pinned or mapped host staging buffers only in a custom C++ bridge after
  profiling. Do not assume RealSense-owned frame memory can be safely registered
  directly for CUDA access.
- Avoid CPU/GPU round trips for display-only paths. Publish RGB plus compressed
  depth preview for UI and reserve raw Z16 depth for compute.

## Runtime Reliability Work

- Make USBGuard allow rules permanent by RealSense serial, not by broad device
  class.
- Keep the single-owner RealSense lock used by VIGIL.
- Require USB3 for production profiles and fail early on USB2 fallback unless a
  degraded profile is explicitly selected.
- Avoid default `hardware_reset()`. Use reset only in an explicit recovery
  profile after collecting USB/librealsense/kernel artifacts.
- Keep acquisition in a worker thread with bounded waits and reconnect backoff.
- Drain to the newest frameset and use queue depth 1 or 2 for display paths.
- Move raw depth off Python custom `uint16[]` messages and toward
  `sensor_msgs/Image` `16UC1` or an rclcpp bridge.
- Evaluate upstream `realsense2_camera` for production capture with
  `serial_no`, `usb_port_id`, `align_depth.enable`, `enable_sync`,
  `wait_for_device_timeout`, `reconnect_timeout`, `SENSOR_DATA` QoS, and
  diagnostics.

## Validation Gates

- Configure succeeds with the GB10 script.
- Build succeeds with CUDA enabled and broad local feature set.
- Install succeeds into the isolated prefix.
- `rs-enumerate-devices --version` reports `2.58.1`.
- `pkg-config --libs realsense2` resolves to the isolated ARM libdir.
- A native C++ smoke program links through `pkg-config` and queries devices.
- `pyrealsense2` imports from the same prefix and queries devices.
- With a D435 attached and authorized, a 60 second profile run reaches at least
  25 Hz for RGB plus depth preview on USB3 and records frame drops, CPU use, GPU
  use, USB bandwidth, and reconnect behavior.
- `rs-gb10-profiler --list-profiles` succeeds from the isolated prefix.
- DDS command-line tools at least handle `--help` without loader or shutdown
  crashes from the isolated prefix.
- Non-headless profiler runs produce bounded log output plus one framebuffer
  evidence image per rendered cycle under `/tmp/rs-gb10-profiler/`.

## Session Results

- Fork created at `https://github.com/David-Martel/librealsense`.
- Local branch: `david/dgx-spark-gb10-realsense-v2.58.1`.
- Installed prefix:
  `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10`.
- Convenience launcher installed as `/usr/local/bin/realsense-gb10-env`.
- Added `scripts/build-dgx-spark-gb10.sh` for repeatable configure, build,
  install, and validate steps.
- Added `scripts/realsense-gb10-env` to expose the isolated SDK prefix without
  overwriting the existing `/usr/local` RealSense install.
- Patched `config/librealsense.pc.in` and `config/librealsense-gl.pc.in` so
  pkg-config uses `@CMAKE_INSTALL_LIBDIR@` instead of a hard-coded x86_64 path.
- Patched CUDA CMake handling so a caller-specified
  `CMAKE_CUDA_ARCHITECTURES=121` is preserved for GB10.
- Patched ARM64 build failures in optional wrappers:
  - OpenCV depth-filter no longer adds x86-only `-msse4.1` on ARM.
  - OpenNI2 wrapper discovers `/usr/include/openni2` and builds with GCC 13.
  - OpenCV KinFu and PCL wrappers link their ImGui/GLFW sources.
- Installed additional local tooling and dependencies:
  - FastDDS tools/generator and ROS FastRTPS packages.
  - PCL ROS utilities and `pcl-tools`.
  - Vulkan tools.
  - Kernel `perf` tools.
  - `libpcap-dev` for packet-capture-capable optional IO paths.
- Validation completed:
  - `rs-enumerate-devices --version` reports `2.58.1.0`.
  - `pkg-config --modversion realsense2` reports `2.58.1`.
  - Native C++ smoke links through prefix-local `pkg-config` and sees
    `devices=1`.
  - Prefix-local Python `pyrealsense2` imports and sees `devices=1`.
  - `rs-enumerate-devices -s` sees `Intel RealSense D435`, serial
    `346522072418`, firmware `5.17.0.10`.
  - RGB + Z16 depth `640x480@30` steady-state stream test with SDK alignment to
    color processed `300/300` aligned frames at `29.98` wall FPS and `29.99`
    sensor FPS with no observed timeouts.
- Added `rs-gb10-profiler`, a GB10-specific benchmark and lifecycle stress
  tool:
  - Default mode opens a GLFW/OpenGL render window and writes a PPM framebuffer
    evidence image after each rendered cycle.
  - `--no-render` runs the same capture/start/stop path without the visible
    window for stress and automation.
  - Built-in profiles cover `vga30`, `vga60`, `depth90-ir`, `hd15`, and `all`.
  - The tool uses a process lock, serial/device preflight, bounded frame waits,
    clean RAII stop, and a hard stop watchdog so failed stops do not silently
    leave camera ownership stuck.
  - Summary output records USB3 state, start and stop latency, frame count,
    effective FPS, timeouts, inter-frame gaps, processing/render timing, RSS,
    and evidence paths.
- Hardened `rs-gb10-profiler` stop management after repeated D435 stop/restart
  failures:
  - It drains and drops frames for `--pre-stop-drain-ms` before calling
    `pipeline.stop()`.
  - It destroys processing objects that can retain frame references, including
    align, colorizer, pointcloud, and temporal/spatial filters, before
    `pipeline.stop()`.
  - It adds a firmware/USB settle period via `--pre-stop-settle-ms`.
  - Defaults now use `1200 ms` pre-stop drain, `250 ms` pre-stop settle,
    `1000 ms` post-stop cooldown, and a `30000 ms` hard stop watchdog.
  - Result lines now include `pre_stop_ms`, `drain_framesets`, and
    `drain_timeouts`.
- Rebuilt the GB10 SDK path with C++20 enabled for the main C++ targets and the
  profiler compiled as strict C++20:
  - `rs-gb10-profiler` now uses a C++20 `std::jthread` RAII watchdog instead of
    a detached watchdog thread.
  - A C++20 compatibility fix was required in vendored `rsutils`:
    `single_consumer_frame_queue<T>(...)` was changed to the standard constructor
    spelling `single_consumer_frame_queue(...)`.
  - `librealsense2.so`, `librealsense2-gl.so`, and `rs-gb10-profiler` linked
    successfully under the updated C++20 build configuration.
  - Known C++20 warning debt remains in older vendored/upstream code, including
    deprecated implicit `this` captures and EasyLogging++ fortify warnings.
- Added validation coverage for `rs-gb10-profiler --list-profiles`,
  `rs-dds-sniffer --help`, `rs-dds-config --help`, and
  `rs-dds-adapter --help`.
- Added system-level GB10 performance tuning:
  - `/usr/local/sbin/dgx-spark-performance-tuning`
  - `/etc/systemd/system/dgx-spark-performance-tuning.service`
  - `/etc/udev/rules.d/99-dgx-spark-performance.rules`
  - `/etc/modprobe.d/99-dgx-spark-usbcore.conf`
  - `/etc/default/grub.d/99-dgx-spark-performance.cfg`
- Installed `uhubctl` for supported hub per-port power inspection/cycling.
- The performance service is enabled and active. Current-boot sysfs tuning set
  USB autosuspend to `-1`, PCIe ASPM policy to `performance`, NVMe default
  power-state latency to `0`, and CPU governors to `performance` where exposed.
- `update-grub` installed kernel command-line defaults for
  `usbcore.autosuspend=-1`, `pcie_aspm=off`, `pcie_port_pm=off`, and
  `nvme_core.default_ps_max_latency_us=0`. Reboot is still required before those
  boot-time settings are fully active.
- Added `realsense-rsusb-metal` for low-level RSUSB/user-space ownership work:
  - `status` reports RealSense USB device speed, authorization, power, and
    bound interface drivers.
  - `tune` applies RealSense USB power tuning.
  - `unbind-uvcvideo` detaches only RealSense UVC interfaces from `uvcvideo`.
  - `rebind-uvcvideo` restores those interfaces if needed.
- RSUSB source review:
  - `FORCE_RSUSB_BACKEND=ON` selects the libuvc/libusb backend through
    `RS2_USE_LIBUVC_BACKEND`.
  - The Linux RSUSB path compiles `src/libuvc/rsusb-backend-linux.cpp`.
  - The libusb handle sets auto-detach on claimed interfaces before claiming
    them, so normal streaming already bypasses the kernel UVC data path after
    the device has enumerated.
  - Kernel `uvcvideo` can still probe the device at attach time and emit UVC
    format/control warnings. Do not globally blacklist `uvcvideo`; use
    `realsense-rsusb-metal unbind-uvcvideo` only as a targeted recovery or
    interference test for RealSense interfaces.
- Stop-path source review:
  - `rs2::pipeline::stop()` has no public timeout parameter. It synchronously
    stops sync/aggregation, stops the multistream, closes stream profiles, and
    stops its dispatcher.
  - RSUSB UVC stop cancels request callbacks, stops the watchdog, stops backend
    frame allocation, clears queued frames, cancels outstanding USB requests,
    waits for backend frame ownership to empty, resets the read endpoint, and
    stops the publish thread.
  - Application-held frames and processing objects matter because retained frame
    references can extend the backend archive drain and make stop slow or dirty.
  - The D400 errata history includes multiple start/stop and reset-adjacent
    failures, so the production interface should treat stop/restart as a
    state-machine transition with drain, settle, bounded stop, cooldown, and
    telemetry.
- USB3 profiler observations before the camera disconnected:
  - Visible `vga30` with pointcloud and filters reached `26.79` effective FPS
    over 5 seconds, with `134` framesets, `2` timeouts, `4` inter-frame gaps,
    clean stop, and NVIDIA GB10 OpenGL rendering.
  - Visible `vga30` without the heavy pointcloud/filter path reached `28.91`
    effective FPS over 15 seconds, with `434` framesets, `2` timeouts, `4`
    gaps, clean stop, and about `2.75 ms` mean render time.
  - No-render `all` stress over `vga30`, `vga60`, `depth90-ir`, and `hd15`
    completed without profile failures and produced `779` total framesets.
  - A later short no-render stream hit libusb/UVC control-transfer errors,
    endpoint reset timeouts, and a hardware disconnect.
- Current blocker:
  - `lsusb -t` no longer shows the D435.
  - Kernel logs show `usb 4-1: USB disconnect, device number 2` at
    `2026-06-01 23:20:43`.
  - After a manual camera power cycle at `2026-06-01T23:43:05-04:00`,
    `lsusb -t`, sysfs USB device enumeration, USBGuard device listing, and
    `realsense-rsusb-metal status` still showed no `8086:0b07` device.
  - Kernel logs for that retry window contained no new RealSense attach event.
  - Current validation therefore reports `devices=0` until the camera is
    physically reconnected on a live USB3 link or the machine is rebooted.

## Open Items

- Reconnect the D435 or reboot the DGX Spark, then run:

  ```bash
  realsense-gb10-env rs-gb10-profiler --profile vga30 --cycles 1 --duration-sec 15 --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250 --cooldown-ms 1000
  realsense-gb10-env rs-gb10-profiler --profile all --cycles 3 --duration-sec 5 --pointcloud --filters --no-render --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250 --cooldown-ms 1000
  sudo realsense-rsusb-metal status
  ```

- If kernel UVC probing or reattach appears to interfere with RSUSB after
  reconnect, run a targeted test:

  ```bash
  sudo realsense-rsusb-metal unbind-uvcvideo
  realsense-gb10-env rs-gb10-profiler --profile all --cycles 3 --duration-sec 5 --no-render --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250
  sudo realsense-rsusb-metal rebind-uvcvideo
  ```

- Reboot once to apply the installed kernel command-line power settings, then
  repeat the visible and no-render profiler runs.
- Keep CUDA architecture `121`; the GB10 configure/build path accepted it.
- Keep `LRS_GB10_CXX_STANDARD=20` for GB10 experiments unless a downstream
  wrapper shows an ABI or source-compatibility issue. If production stability is
  prioritized over toolchain modernization, rebuild with
  `LRS_GB10_CXX_STANDARD=14` and keep only `rs-gb10-profiler` on C++20.
- Keep `LRS_GB10_WITH_IPO=OFF` for now. LTO should only be enabled after a clean
  A/B benchmark because pybind/CUDA builds are more sensitive to link-time
  optimization and no measured win has been shown yet.
- Investigate the remaining RealDDS duplicate static/shared symbol issue before
  relying on normal `rs-dds-adapter` shutdown in production. The `--help` and
  `--version` paths are fixed and covered by validation.
- Build or install CUDA-enabled OpenCV under `/opt/vigil/opt/opencv-cuda` before
  moving VIGIL preview processing onto `cv2.cuda`; the current system `cv2`
  install did not expose usable CUDA devices.
- Re-run VIGIL display and SAM profiles after the camera is physically stable,
  authorized by USBGuard, and validated by `rs-gb10-profiler`.
