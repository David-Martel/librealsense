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

## Open Items

- Run a CUDA arch configure test for `121`; if unsupported, use `120`.
- Decide whether `LRS_GB10_WITH_IPO=ON` is stable with CUDA in this tree.
- Benchmark RealDDS/FastDDS tools against the ROS wrapper path and decide
  whether DDS should remain enabled in production builds or only in debug
  builds.
- Build or install CUDA-enabled OpenCV under `/opt/vigil/opt/opencv-cuda` before
  moving VIGIL preview processing onto `cv2.cuda`.
- Re-run VIGIL display and SAM profiles after the camera is physically stable
  and permanently authorized by USBGuard.
