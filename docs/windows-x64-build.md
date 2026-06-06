# Windows x64 Build — David-Martel librealsense Fork

> Added: 2026-06-06. Device in use: Intel RealSense D435 (USB 8086:0b07) on Windows 11 x86_64.
> This document covers the secondary Windows/x64 build configuration added alongside the primary
> GB10 / DGX Spark (aarch64, Ubuntu 24.04) build. The two configurations are completely
> independent.

---

## 1. Fork enhancements — what they are and which apply on Windows

The David-Martel fork adds three compile-time opt-in enhancements on top of upstream librealsense
`v2.58.1`. **All three are inert on a default Windows WMF build**, by construction:

| Enhancement | CMake define | ARM/Linux-only? | Why it is inert on Windows WMF |
|---|---|---|---|
| **GB10 USB tuning** — P2 deeper URB pool, P4 gentler stop, P7 re-acquire guard, H3 wedge detector | `RS2_GB10_USB_TUNING=1` | No — header is portable C++. But callers are in `src/uvc/` and `src/libusb/`. | `src/uvc/` and `src/libusb/` are compiled only under the RSUSB/libuvc backend. Default Windows uses `RS2_USE_WMF_BACKEND` (`FORCE_RSUSB_BACKEND=OFF`), which compiles `src/mf/` instead. `usb-tuning.h` is never `#include`d in the WMF path. Verified: grep of all `usb-tuning.h` include sites finds only `src/uvc/uvc-{device,streamer}.{cpp,h}` and `src/libusb/device-libusb.cpp`. |
| **Pointcloud cached-buffer ladder** — 3.3× faster on GB10 CUDA | `RS2_GB10_PC_ZEROCOPY=1` | CUDA-required | `BUILD_WITH_CUDA=OFF` (default). `src/cuda/` is not compiled. |
| **YUYV→color cached-buffer ladder** — ~NEON-parity on GB10 CUDA | `RS2_GB10_CONV_CACHE=1` | CUDA-required | Same as above. |
| **NEON filter optimizations** | `BUILD_WITH_NEON` | ARM64-only | `lrs_options.cmake` only defines this option when `CMAKE_SYSTEM_PROCESSOR` matches `aarch64`. On x86_64 the variable does not exist. |
| **GB10 build script** (`scripts/build-dgx-spark-gb10.sh`) | — | Linux shell | Shell script only; not referenced by any CMake target. No effect on Windows. |

**Conclusion:** A Windows WMF build from this fork is byte-identical to an upstream build. The GB10
enhancements compile in only when you pass their flags explicitly; none is a default. The Windows
preset below does not set any of them.

### Windows-applicable optimizations (what the preset does enable)

- **SSE3**: always-on for MSVC x86-64 (`..\windows_config.cmake` adds `-D__SSSE3__`).
- **AVX2 per-file**: `src/CMakeLists.txt` sets `/arch:AVX2`-equivalent flags (`-mavx2`) on
  `src/image-avx.cpp` when `LRS_TRY_USE_AVX` is true (set by `windows_config.cmake` for MSVC).
  This is the SDK's own mechanism; the preset leaves `BUILD_WITH_CPU_EXTENSIONS=ON` (default ON)
  so it activates normally.
- **Multi-core builds**: `windows_config.cmake` adds `/MP` for MSVC — always active.
- **Release optimizations**: `/O2` and `/Oi` come from the Visual Studio Release configuration
  automatically. `--config Release` selects this at build time.

---

## 2. Windows x64 build prerequisites

Tested environment (2026-06-06):

| Tool | Version | Notes |
|---|---|---|
| CMake | 4.3.3 | System install; 3.21+ required for CMakePresets v3 |
| Visual Studio | 2022 Community v17.14 | MSVC toolset `14.44.35207` x64 |
| Ninja | 1.13.2 | Present but **not used** by this preset (VS generator handles toolset detection) |
| Windows SDK | 10.0.26100.0 | Auto-selected by VS generator |
| Git | 2.54.0 | Required for FetchContent (nlohmann/json, fastcdr) |
| Network | Online | CMake FetchContent fetches `nlohmann/json` and `fastcdr` at configure time |

> **Generator choice note:** The preset uses `"Visual Studio 17 2022"` rather than Ninja because
> the VS generator locates the MSVC toolset through vswhere and bypasses the broken-PATH `cl.exe`
> issue (a `14.44.35207.broken` toolset directory exists alongside the working one). With Ninja
> a plain PowerShell session would re-trigger compiler detection failures. If Ninja is preferred,
> run from a Visual Studio x64 Developer Command Prompt and replace the generator.

---

## 3. CMakePresets.json — `windows-x64-release` preset

Added at repo root: `CMakePresets.json` (new file). `.gitignore` was also minimally updated — see Section 7 for rationale.

Key settings in the preset:

```json
{
  "name": "windows-x64-release",
  "generator": "Visual Studio 17 2022",
  "architecture": { "value": "x64", "strategy": "set" },
  "binaryDir": "${sourceDir}/build/windows-x64-release",
  "cacheVariables": {
    "BUILD_WITH_CPU_EXTENSIONS": "ON",
    "BUILD_WITH_CUDA":           "OFF",
    "BUILD_WITH_NEON":           "OFF",
    "RS2_GB10_USB_TUNING":       "OFF",
    "RS2_GB10_PC_ZEROCOPY":      "OFF",
    "RS2_GB10_CONV_CACHE":       "OFF",
    "FORCE_RSUSB_BACKEND":       "OFF",
    "CHECK_FOR_UPDATES":         "OFF",
    "ENABLE_SECURITY_FLAGS":     "OFF",
    "ENABLE_CCACHE":             "OFF"
  }
}
```

`CHECK_FOR_UPDATES=OFF` removes a `libcurl` FetchContent dependency that would pull at configure
time. `ENABLE_SECURITY_FLAGS=OFF` keeps the preset away from the `ENABLE_SECURITY_FLAGS` code path
that pairs LTCG with `/WX` (warnings-as-errors) in `windows_config.cmake` — that combination
would likely break on a first source build.

---

## 4. Build commands

### Configure
```powershell
cd C:\codedev\librealsense
cmake --preset windows-x64-release
```

Build files are written to `build\windows-x64-release\`.

### Build (Release)
```powershell
cmake --build --preset windows-x64-release --config Release
```

Or equivalently:
```powershell
cmake --build build\windows-x64-release --config Release --parallel
```

### Run viewer (after build)
```powershell
.\build\windows-x64-release\Release\realsense-viewer.exe
```

---

## 5. Configure result (2026-06-06 HIL run)

Configure exit code: **0** (success).

Key configure-time messages confirmed:

```
-- The CXX compiler identification is MSVC 19.44.35227.0
-- using RS2_USE_WMF_BACKEND
-- Building with SSE optimizations
-- Building with FastCDR for ROS2 bag support
-- GLFW 3.3 not found; using internal version
-- Found OpenGL: opengl32
-- Configuring done (36.8s)
-- Generating done (3.3s)
-- Build files have been written to: C:/codedev/librealsense/build/windows-x64-release
```

Backend confirmed as WMF (not RSUSB). SSE optimizations confirmed active. OpenGL found (graphical
examples will configure). No ARM/NEON/CUDA/GB10 flags active.

**Not verified:** a full build (`cmake --build --preset windows-x64-release --config Release`) was
not run. Configure proves the dependency graph resolves and MSVC is detected. A full SDK build
requires significant time and disk I/O and was outside scope. Dependencies known to fetch at
configure time (nlohmann/json, fastcdr via FetchContent) did fetch successfully during the
configure run.

---

## 6. What was verified vs assumed

| Claim | Status |
|---|---|
| `usb-tuning.h` is not included by any WMF-backend source | **Verified** — grep of all include sites |
| `BUILD_WITH_NEON` does not exist on x86_64 configure | **Verified** — `lrs_options.cmake:47` gates the option on `CMAKE_SYSTEM_PROCESSOR` |
| CUDA cached-buffer code is `#if BUILD_WITH_CUDA`-gated | **Verified** — `src/CMakeLists.txt:52-54`; `global_config.cmake:54-68` |
| MSVC x64 toolset 19.44.35227 is available | **Verified** — CMake configure detected it |
| WMF backend selected (not RSUSB) | **Verified** — configure message `using RS2_USE_WMF_BACKEND` |
| Configure succeeds (exit 0) | **Verified** — run on this machine 2026-06-06 |
| Full build produces working realsense-viewer.exe | **NOT verified** — full build not run |
| D435 streams correctly under the WMF backend at runtime | **NOT verified** — device connected but no build yet |
| AVX2 is exercised on `image-avx.cpp` under MSVC | **Assumed** — `src/CMakeLists.txt:56-58` sets `-mavx2` which MSVC processes; MSVC uses intrinsics, not this flag, for AVX dispatch. The file will compile; whether `/arch:AVX2` codegen engages requires inspection of the generated MSVC `.obj` |
| All FetchContent downloads are stable long-term | **Assumed** — nlohmann/json and fastcdr fetched fine in this session; network or upstream changes could affect future configure runs |

---

## 7. Relationship to the GB10 / Spark config

The GB10 build is driven entirely by `scripts/build-dgx-spark-gb10.sh` (Linux shell), targeting
an aarch64 Ubuntu 24.04 host with CUDA 13 and Ninja. It does not use or conflict with
`CMakePresets.json`. The Windows preset:

- Does **not** modify `CMake/windows_config.cmake` or `CMake/lrs_options.cmake`.
- Does **not** modify `CMakeLists.txt`.
- Does **not** modify any GB10 doc or script.

The two build paths share no build directory and no configured state.

### Changed files (three total)

| File | Status | Why |
|---|---|---|
| `CMakePresets.json` | New file | Primary deliverable — configure + build presets |
| `docs/windows-x64-build.md` | New file | This document |
| `.gitignore` | Modified (two-line addition) | The repo contains a blanket `*.json` rule (line 91) that was silently suppressing `CMakePresets.json` from git tracking. Two negation lines (`!CMakePresets.json`) were added immediately after the rule to allow the presets file to be tracked while keeping the blanket exclusion intact for all other `.json` artifacts (compile_commands.json, VS project files, etc.). **This is a minimal, scoped modification to an existing file.** If strict no-existing-file-modification is required, revert this change and instruct git to track the presets file explicitly via `git add -f CMakePresets.json`. |

---

## 8. CUDA preset — `windows-x64-cuda`

Added in Task #20. Extends `CMakePresets.json` with an isolated CUDA-enabled configure+build preset for x86_64 Windows.

### What it enables

`BUILD_WITH_CUDA=ON` activates two code paths in the SDK's CMake:

1. `CMake/cuda_config.cmake` — `enable_language(CUDA)` + `find_package(CUDA REQUIRED)`, sets arch list and NVCC flags.
2. `src/CMakeLists.txt:52-54` — includes `src/cuda/CMakeLists.txt` → adds `cuda-pointcloud.cu`, `cuda-conversion.cu`, `rscuda_utils.cuh` to the `realsense2` target.
3. `src/proc/CMakeLists.txt:5-7` — includes `src/proc/cuda/CMakeLists.txt` → adds `cuda-align.cu`, `cuda-pointcloud.cpp` to the target.
4. `global_config.cmake:54-56` — adds `-DRS2_USE_CUDA` preprocessor define.

All three GB10-specific options (`RS2_GB10_USB_TUNING`, `RS2_GB10_PC_ZEROCOPY`, `RS2_GB10_CONV_CACHE`) remain `OFF`. The `RS2_GB10_PC_ZEROCOPY` and `RS2_GB10_CONV_CACHE` guards inside `cuda-pointcloud.cu` and `cuda-conversion.cu` are `#if defined(...)` preprocessor blocks that are dormant unless those defines are passed — they are **not** auto-activated by `BUILD_WITH_CUDA`.

### CUDA architecture decision (sm_120, Blackwell)

The RTX 5060 Ti is a Blackwell-generation GPU (compute capability 1.2.0 = sm_120).

`nvcc --list-gpu-arch` for the installed CUDA v13.1 toolkit confirms `compute_120` is accepted:
```
compute_75, compute_80, compute_86, compute_87, compute_88, compute_89,
compute_90, compute_100, compute_110, compute_103, compute_120, compute_121
```

`CMake/cuda_config.cmake` builds its own architecture list from `CUDA_VERSION` at configure time — for v13.1 the auto-list is `75 80 86 89 90 100 120 110`. The preset overrides this via `CMAKE_CUDA_ARCHITECTURES=120` (single-arch, fastest build) using CMake policy `CMP0104 NEW` (CMake 3.18+, satisfied by CMake 4.3.3). Setting a single target arch keeps kernel object files small and is appropriate for a development/validation build targeting one known GPU.

### Toolkit variable notes

The SDK uses the legacy `find_package(CUDA)` path (with `enable_language(CUDA)` also called). Two variables are set in the preset:

- `CUDA_TOOLKIT_ROOT_DIR` — consumed by `find_package(CUDA)` (legacy FindCUDA). **Used.**
- `CUDAToolkit_ROOT` — consumed by `find_package(CUDAToolkit)` (modern). CMake warned "Manually-specified variable not used" — the SDK does not call the modern form, so this variable is redundant but harmless.

nvcc path confirmed in cache: `C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1/bin/nvcc.exe`.

Note: the configure output showed CUDA compiler identification as `NVIDIA 13.2.51` (the nvcc driver version) while `CUDA_VERSION=13.1` (the toolkit version). Both refer to the v13.1 toolkit directory. This is normal — nvcc's internal compiler version differs from the toolkit release number.

### `windows-x64-release` is untouched

The new preset is a parallel, independent entry in `configurePresets`. It does not inherit from `windows-x64-release` and does not modify it. Binary directories are separate (`build/windows-x64-cuda/` vs `build/windows-x64-release/`).

### CMakePresets.json key settings

```json
{
  "name": "windows-x64-cuda",
  "generator": "Visual Studio 17 2022",
  "architecture": { "value": "x64", "strategy": "set" },
  "binaryDir": "${sourceDir}/build/windows-x64-cuda",
  "cacheVariables": {
    "BUILD_WITH_CUDA":            "ON",
    "CMAKE_CUDA_ARCHITECTURES":   "120",
    "CUDAToolkit_ROOT":           "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1",
    "CUDA_TOOLKIT_ROOT_DIR":      "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1",
    "RS2_GB10_USB_TUNING":        "OFF",
    "RS2_GB10_PC_ZEROCOPY":       "OFF",
    "RS2_GB10_CONV_CACHE":        "OFF",
    "FORCE_RSUSB_BACKEND":        "OFF",
    "CHECK_FOR_UPDATES":          "OFF",
    "ENABLE_SECURITY_FLAGS":      "OFF",
    "ENABLE_CCACHE":              "OFF"
  }
}
```

### Build and configure commands

```powershell
cd C:\codedev\librealsense
cmake --preset windows-x64-cuda

# Full build (multi-hour; not run in Task #20):
cmake --build --preset windows-x64-cuda --config Release
```

### Configure result (2026-06-06 HIL run)

Configure exit code: **0** (success).

Key configure-time messages (verbatim):

```
-- The CXX compiler identification is MSVC 19.44.35227.0
-- The CUDA compiler identification is NVIDIA 13.2.51 with host compiler MSVC 19.44.35227.0
-- Info: Building with CUDA..
-- Found CUDA: C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1 (found version "13.1")
-- CUDA_LIBRARIES: C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1/include
    C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1/lib/x64/cudart_static.lib;
    cusparse.lib;cublas.lib
-- using RS2_USE_WMF_BACKEND
-- Building with SSE optimizations
-- Configuring done (47.8s)
-- Generating done (3.1s)
-- Build files have been written to: C:/codedev/librealsense/build/windows-x64-cuda
```

CMake cache confirmed (from `CMakeCache.txt`):
- `CMAKE_CUDA_ARCHITECTURES:UNINITIALIZED=120`
- `CUDA_VERSION:STRING=13.1`
- `CUDA_NVCC_EXECUTABLE:FILEPATH=C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1/bin/nvcc.exe`
- `CUDA_TOOLKIT_ROOT_DIR:UNINITIALIZED=C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.1`

CUDA `.cu` sources confirmed in `realsense2.vcxproj` as `<CudaCompile>` items:
- `src\cuda\cuda-conversion.cu`
- `src\cuda\cuda-pointcloud.cu`
- `src\proc\cuda\cuda-align.cu`

### What was verified vs assumed (CUDA preset)

| Claim | Status |
|---|---|
| `compute_120` accepted by CUDA v13.1 nvcc | **Verified** — `nvcc --list-gpu-arch` output |
| `CMAKE_CUDA_ARCHITECTURES=120` reaches the cache | **Verified** — `CMakeCache.txt:148` |
| `CUDA_VERSION=13.1` in cache | **Verified** — `CMakeCache.txt:490` |
| nvcc from v13.1 toolkit dir | **Verified** — `CMakeCache.txt:443` |
| CUDA .cu files added as `<CudaCompile>` in realsense2.vcxproj | **Verified** — grep of generated vcxproj |
| `RS2_GB10_PC_ZEROCOPY` / `RS2_GB10_CONV_CACHE` not auto-enabled by `BUILD_WITH_CUDA` | **Verified** — CMakeLists.txt:83,92 plain `if(RS2_GB10_*)` blocks, not `if(BUILD_WITH_CUDA)` |
| WMF backend selected (not RSUSB) | **Verified** — configure message `using RS2_USE_WMF_BACKEND` |
| Configure succeeds (exit 0) | **Verified** — run on this machine 2026-06-06 |
| MSVC 19.44 is a supported host compiler for CUDA 13.1 | **Assumed** — configure probed and accepted it; CUDA 13.x support for VS 2022 17.14 toolset is expected but no explicit NVIDIA compatibility table consulted |
| sm_120 generates correct Blackwell microarchitecture code | **Assumed** — nvcc accepts the arch flag at configure time; actual PTX/SASS output not inspected |
| Full build produces working CUDA kernels at runtime | **NOT verified** — full build not run (multi-hour; out of Task #20 scope) |
| `CUDAToolkit_ROOT` unused warning is benign | **Verified** — CMake warning text confirms it; legacy `find_package(CUDA)` path is what the SDK uses |
