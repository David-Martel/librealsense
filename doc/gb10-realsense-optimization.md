# DGX Spark GB10 RealSense Optimization

This branch is a focused DGX Spark / GB10 optimization fork of librealsense
`v2.58.1`. It keeps the upstream SDK shape intact while adding an isolated
GB10 build, RSUSB-oriented tooling, lifecycle profiling, and stop/restart
hardening for Intel RealSense D400-class cameras.

## Branch

- Upstream base: `v2.58.1`
- Fork remote: `david` (`git@github.com:David-Martel/librealsense.git`)
- Branch: `david/dgx-spark-gb10-realsense-v2.58.1`
- Install prefix:
  `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10`
- Build directory:
  `/opt/vigil/build/librealsense-v2.58.1-dgx-spark-gb10`

## Commit Clusters

### 1. GB10 SDK Build And Wrapper Enablement

Commit: `f7ae4ca Optimize librealsense build for DGX Spark GB10`

Purpose:
- Build an isolated, repeatable SDK prefix for ARM64 GB10 systems.
- Enable RSUSB, CUDA, OpenMP, Python, DDS, graphical tools, OpenCV, PCL,
  OpenNI2, and ROS bag support where the local ARM64 toolchain can support it.
- Repair ARM64 wrapper build issues without changing the upstream runtime API.

Main files:
- `scripts/build-dgx-spark-gb10.sh`
- `scripts/realsense-gb10-env`
- `CMake/cuda_config.cmake`
- `config/librealsense.pc.in`
- `config/librealsense-gl.pc.in`
- `wrappers/opencv/*`
- `wrappers/openni2/*`
- `wrappers/pcl/pcl/CMakeLists.txt`
- `realsense.TODO.md`

Validation:
- Configure, build, install, and prefix validation completed.
- `pkg-config` resolves to the isolated ARM64 prefix.
- Prefix-local `pyrealsense2` imports.
- Earlier attached-camera validation saw the D435 and sustained
  `640x480@30` color/depth streaming.

### 2. Profiler, RSUSB, DDS, And System Performance Tooling

Commit: `6fc7c75 Add GB10 RealSense profiler and power tuning`

Purpose:
- Add a repeatable non-headless profiler for capture, rendering, evidence
  images, start/stop timing, and profile stress testing.
- Add targeted RSUSB helper tooling for interface inspection and optional
  RealSense-only `uvcvideo` unbind/rebind.
- Install system-level USB, PCIe, NVMe, and CPU performance tuning.
- Stabilize DDS command-line validation paths enough for this GB10 build.

Main files:
- `tools/gb10-profiler/*`
- `tools/CMakeLists.txt`
- `scripts/dgx-spark-performance-tuning.sh`
- `scripts/dgx-spark-performance-tuning.service`
- `scripts/99-dgx-spark-performance.rules`
- `scripts/99-dgx-spark-usbcore.conf`
- `scripts/99-dgx-spark-performance-grub.cfg`
- `scripts/realsense-rsusb-metal.sh`
- `tools/dds/dds-adapter/*`
- `tools/dds/dds-config/CMakeLists.txt`
- `realsense.TODO.md`

Validation:
- `rs-gb10-profiler --list-profiles` validates from the installed prefix.
- DDS `--help` surfaces validate from the installed prefix.
- Performance tuning service is installed and enabled.
- USB autosuspend and PCIe/NVMe performance settings are configured; a reboot is
  required for all kernel command-line settings to take effect.

### 3. Stop Lifecycle And C++20 Hardening

Commit: `3008f38 Harden GB10 RealSense stop lifecycle`

Purpose:
- Treat stop/restart as a bounded lifecycle transition instead of an immediate
  `pipeline.stop()` call.
- Reduce chances that retained frames or processing blocks keep RSUSB backend
  frame ownership alive during stop.
- Move the profiler interface to C++20 and use stronger RAII for stop watchdog
  cleanup.
- Prove that the GB10 SDK path can build with C++20 after a small rsutils
  compatibility fix.

Main files:
- `tools/gb10-profiler/rs-gb10-profiler.cpp`
- `tools/gb10-profiler/CMakeLists.txt`
- `scripts/build-dgx-spark-gb10.sh`
- `third-party/rsutils/include/rsutils/concurrency/concurrency.h`
- `realsense.TODO.md`

Validation:
- `librealsense2.so`, `librealsense2-gl.so`, and `rs-gb10-profiler` linked
  under the C++20 build configuration.
- `rs-gb10-profiler` compiles as strict C++20 and exposes:
  `--pre-stop-drain-ms`, `--pre-stop-settle-ms`, `--cooldown-ms`,
  `--stop-warn-ms`, and `--hard-stop-ms`.
- Installed prefix validation completed after the C++20 rebuild.

## Technical Notes

- The GB10 build uses `FORCE_RSUSB_BACKEND=ON`, which selects the libuvc/libusb
  backend (`RS2_USE_LIBUVC_BACKEND`).
- RSUSB claims interfaces through libusb and can auto-detach kernel drivers.
  The Linux kernel can still probe the device before RSUSB owns the interfaces.
- Do not globally blacklist `uvcvideo`; use `realsense-rsusb-metal
  unbind-uvcvideo` only for targeted RealSense recovery or interference tests.
- `rs2::pipeline::stop()` has no public timeout parameter. The profiler now
  drains frames, releases processing objects, waits briefly, then calls stop
  under a hard watchdog.
- C++20 exposed one vendored rsutils constructor spelling issue. That was fixed
  by changing `single_consumer_frame_queue<T>(...)` to
  `single_consumer_frame_queue(...)`.

## Current Runtime Blocker

The D435 currently does not enumerate on USB. `lsusb`, USB sysfs inventory,
USBGuard inventory, `realsense-rsusb-metal status`, and
`rs-enumerate-devices -s` all show no `8086:0b07` device.

Recommended next run after physical USB3 reconnection or reboot:

```bash
realsense-gb10-env rs-gb10-profiler --profile vga30 --cycles 1 --duration-sec 15 --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250 --cooldown-ms 1000
realsense-gb10-env rs-gb10-profiler --profile all --cycles 3 --duration-sec 5 --pointcloud --filters --no-render --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250 --cooldown-ms 1000
sudo realsense-rsusb-metal status
```

Optional RSUSB isolation test after reconnection:

```bash
sudo realsense-rsusb-metal unbind-uvcvideo
realsense-gb10-env rs-gb10-profiler --profile all --cycles 3 --duration-sec 5 --no-render --pre-stop-drain-ms 1200 --pre-stop-settle-ms 250
sudo realsense-rsusb-metal rebind-uvcvideo
```
