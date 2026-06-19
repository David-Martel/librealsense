# Windows build helper

`scripts/build-windows.ps1` is the Windows-first entrypoint for local
librealsense builds. It keeps the default Windows WMF backend, builds the
consumer-relevant tools/examples, and leaves all GB10-only switches off unless
future work explicitly adds a separate profile.

The helper avoids hard-coded downstream paths. Consumers that need Python
bindings pass their interpreter explicitly:

```powershell
pwsh -NoLogo -NoProfile -File C:\codedev\librealsense\scripts\build-windows.ps1 `
  -BuildDir C:\codedev\librealsense\build\windows-x64-clarius-python `
  -BuildPythonBindings `
  -PythonExecutable C:\path\to\consumer\.venv\Scripts\python.exe `
  -ValidateImport `
  -ValidateDevice
```

CUDA is opt-in and host-derived:

```powershell
pwsh -NoLogo -NoProfile -File C:\codedev\librealsense\scripts\build-windows.ps1 `
  -Cuda `
  -BuildPythonBindings `
  -PythonExecutable C:\path\to\python.exe
```

When `-Cuda` is set, the script discovers `CUDAToolkit_ROOT` from
`CUDA_PATH` or `nvcc.exe`, and discovers `CMAKE_CUDA_ARCHITECTURES` from
`nvidia-smi` unless `-CudaArchitectures` is supplied. This avoids baking in a
single GPU such as `sm_120`.

Default targets are:

- `realsense2`
- `rs-enumerate-devices` when tools are enabled
- `rs-align` and `rs-pointcloud` when graphical examples are enabled
- `pyrealsense2` when Python bindings are enabled

Use `-Targets` for a narrower probe, or `-NoBuild` to configure only.

## Validation notes

Validated on this workstation with the `C:\codedev\clarius` Python 3.13
environment:

```powershell
pwsh -NoLogo -NoProfile -File C:\codedev\librealsense\scripts\build-windows.ps1 `
  -BuildDir C:\codedev\librealsense\build\windows-clarius-cuda-python `
  -Generator Ninja `
  -Cuda `
  -BuildPythonBindings `
  -PythonExecutable C:\codedev\clarius\.venv\Scripts\python.exe `
  -Targets pyrealsense2 `
  -ValidateImport
```

The build completed successfully with CUDA 13.1, MSVC 19.44, Ninja,
`BUILD_WITH_CUDA=ON`, `BUILD_PYTHON_BINDINGS=ON`, and
`CMAKE_CUDA_ARCHITECTURES=120;89`, derived from the visible RTX 5060 Ti and
RTX 2000 Ada GPUs. Do not hard-code a single architecture on multi-GPU
workstations unless the runtime process is pinned to that GPU.

Python import validation is necessary but not sufficient. A prior
`sm_120`-only build imported successfully, but CUDA depth-to-color alignment
failed at runtime on the `sm_89` GPU with `no kernel image is available for
execution on the device`. Validate the CUDA kernels with a live align probe:

```powershell
cmd /c "set LIBREALSENSE_PYTHON_BUILD=C:\codedev\librealsense\build\windows-clarius-cuda-python&& uv run python C:\codedev\clarius\vigil-ultrasound\probe_realsense.py --align-backend cuda --align-backend sse"
```

On this workstation the corrected build produced nonzero aligned-depth pixels
for both backends on the attached D435.

Fixed helper issues found during validation:

- Visual Studio environment import now uses a temporary `.cmd` file so
  `VsDevCmd.bat` paths under `Program Files (x86)` are quoted correctly.
- CMake path-valued definitions for Python and CUDA are normalized to forward
  slashes before passing them to CMake/FindCUDA.
- Native `cmake`, Python import validation, and `rs-enumerate-devices` failures
  now throw instead of allowing PowerShell to continue after a non-zero native
  exit code.
- Runtime CUDA align failures are checked in `cuda-align.cu` instead of being
  silently converted into empty aligned-depth frames.

Known remaining caveat: the Visual Studio generator profile can take materially
longer than the Ninja lane for configure-only probes on this machine. Prefer
`-Generator Ninja` for consumer Python-binding pipelines unless Visual Studio
solution files are explicitly required.
