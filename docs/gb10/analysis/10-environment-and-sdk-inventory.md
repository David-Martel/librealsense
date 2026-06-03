# 1. Environment & RealSense SDK Inventory

## 1.1 Platform

| Property | Value |
|---|---|
| Hostname | `spark-3066` |
| Model | NVIDIA DGX Spark / **GB10** (ARM64 / aarch64) |
| OS | Ubuntu **24.04.4 LTS** (Noble), kernel `6.17.0-1021-nvidia` |
| GPU/SoC | NVIDIA GB10, CUDA 13.0 runtime, driver **580.159.03** |
| xHCI controllers | NVIDIA platform xHCI (`NVDA8000:0x`), each exposes a USB‑2.0 root (e.g. `usb5`, `bcdUSB 2.00`) + a SuperSpeed companion root (e.g. `usb6`, `bcdUSB 3.10`). USB buses 2/4/6/8/10/12 are SuperSpeed (20000M/x2). |

> Note: no x86 `lspci` USB rows — USB is delivered via Tegra/GB10 **platform** xHCI nodes under `/sys/bus/platform/devices/NVDA8000:*`, not PCIe. Device sysfs path is `/devices/platform/NVDA8000:02/usb5/...`.

## 1.2 RealSense software present on this machine

There are **three** distinct librealsense footprints plus ROS:

| # | Location | What it is | Notes |
|---|---|---|---|
| A | `/usr/local/lib/librealsense2.so.2.58.1` | "known‑good" upstream **v2.58.1** RSUSB Release build | Referenced by the fork TODO as the baseline to preserve. |
| B | `/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/` | **Isolated GB10 fork build** (CUDA, OpenMP, Python, DDS, tools, wrappers) | Active install. `realsense-gb10-env` (`/usr/local/bin`) selects it; `~/.local/bin/*` RealSense tools resolve here. |
| C | `~/dev/repos/librealsense` | The **git working tree** of the David‑Martel fork | Build source for B. |
| — | `ros-jazzy-librealsense2 2.57.7-1noble` (dpkg) | ROS 2 Jazzy wrapper package | Separate from A/B; version‑skewed (2.57.7 vs 2.58.1). |

`~/.local/bin/realsense-viewer` is a **symlink to `realsense-gb10-command`** (a wrapper), i.e. the user‑facing tools are routed through the GB10 prefix, not a stock install.

### Tools available (from GB10 prefix `…/bin`)
`rs-enumerate-devices`, `rs-fw-update`, `rs-depth`, `rs-sensor-control`, `rs-align*`, `rs-benchmark`, `rs-capture`, `rs-convert`, `rs-data-collect`, `rs-dds-adapter`, **`rs-gb10-profiler`** (custom), etc.

## 1.3 GitHub fork mapping (David‑Martel)

`~/dev/repos/librealsense` remotes:

```
origin    https://github.com/David-Martel/librealsense.git
upstream  https://github.com/realsenseai/librealsense.git   (the current upstream org; formerly IntelRealSense)
```

| Item | Value |
|---|---|
| Working branch | `david/dgx-spark-gb10-realsense-v2.58.1` (also pushed to origin) |
| Base | `v2.58.1` (`git describe` → `v2.58.1-12-g81f35eb81`) |
| GB10 commits on top of v2.58.1 | 12 commits, 26 files, **+2020 / −31 lines** |
| Other fork branches | `add-to-cmake`, `d401-bundle-beta-cycle`, `development`, `disable-multi-cam`, `feature/aus`, `fix/aikido-auto` |

### The GB10 commit series (newest → oldest)
```
81f35eb  Harden GB10 RealSense USB validation
a7a3f47  Merge GB10 RealSense interface tooling
db0889f  Bound GB10 sysfs tuning walks
da22eb1  Follow sysfs links for GB10 power tuning
56fe754  Harden GB10 sysfs tuning writes
8970d96  Document GB10 RealSense optimization clusters
3008f38  Harden GB10 RealSense stop lifecycle
6fc7c75  Add GB10 RealSense profiler and power tuning
f7ae4ca  Optimize librealsense build for DGX Spark GB10
```

Key added/modified files:
- `tools/gb10-profiler/rs-gb10-profiler.cpp` (1013 LOC) — custom lifecycle/throughput profiler.
- `scripts/build-dgx-spark-gb10.sh` (224 LOC) — repeatable configure/build/install/validate (`FORCE_RSUSB_BACKEND=ON`, CUDA arch 121→120 fallback, C++20, `-mcpu=native`).
- `scripts/dgx-spark-performance-tuning.{sh,service}` + `99-dgx-spark-performance.rules` + `99-dgx-spark-usbcore.conf` + `99-dgx-spark-performance-grub.cfg` — system power/perf tuning.
- `scripts/realsense-rsusb-metal.sh` — RealSense‑only uvcvideo unbind/rebind + power tuning helper.
- `config/librealsense*.pc.in`, `CMake/cuda_config.cmake`, `wrappers/{opencv,openni2,pcl}/*` — ARM64 build repairs.
- `doc/gb10-realsense-optimization.md`, `realsense.TODO.md` — design notes / project ledger.

## 1.4 Installed udev rules touching the camera

| File | Purpose | Verdict |
|---|---|---|
| `/etc/udev/rules.d/99-realsense-libusb.rules` | Stock RealSense plugdev/0666 rules (incl. `0b07`) | ✅ correct, required |
| `/etc/udev/rules.d/99-realsense-d4xx-mipi-dfu.rules` | MIPI / IPU6 D457 rules | ⚠️ irrelevant to a USB D435 (MIPI only) |
| `/etc/udev/rules.d/99-vigil-realsense-power.rules` | Pin D435 `power/control=on` | ⚠️ band‑aid (see §2) |
| `/etc/udev/rules.d/99-dgx-spark-performance.rules` | Force `power/control=on`, `autosuspend=-1` on **all** USB+PCI; SATA max_performance | ⚠️ blunt instrument (see §2) |

## 1.5 Prior validation artifacts (same day, earlier sessions)

`~/realsense-gb10-validation/` already contains profiler‑driven runs from 14:39–14:57:
- `20260602-143904/` — baseline profiler run (flood of `messenger-libusb control_transfer returned error, index:768`).
- `20260602-144941-rsusb-unbound/` — uvcvideo unbind experiment: **profiler_warnings=330, control_transfer=322, errors=8**.
- `20260602-145651/145747-no-camera-diagnostic/` — camera absent: **kernel_xhci_failures=9, kernel_clear_halt=6, kernel_disconnects=14**.

These are the **"before" evidence** and are preserved. The fork doc's "Current Runtime Blocker" section records that the D435 *would not enumerate at all* at the time of writing — consistent with the marginal‑link story.
