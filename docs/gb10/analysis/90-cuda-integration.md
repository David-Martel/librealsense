# 90. CUDA Media Stack Integration — GB10 librealsense + Preview Helper

**Date:** 2026-06-03  
**Status:** Build-config + static code delivered. HIL validation (live camera frames) BLOCKED — xHCI controller dead; resume after reboot.  
**Hardware constraint:** xHCI controller killed 3× today (see `70-controller-crash-finding.md`). NO USB activity performed in this session. All measurements are on synthetic numpy frames.

---

## 1. What was wired

### 1.1 `scripts/build-dgx-spark-gb10.sh` — two fixes

**Fix A — CUDA_HOME default (P0 from roadmap §1.1):**

```
- CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.2}"
+ CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
```

`/usr/local/cuda-13.2` does not exist on this machine. The actual toolkit is
`/usr/local/cuda -> /etc/alternatives/cuda -> cuda-13.0`. Builds were
accidentally succeeding because `nvcc` was on `PATH`, but
`CUDA_TOOLKIT_ROOT_DIR` pointed at a missing directory — a silent header/lib
inconsistency risk. CMakeCache post-fix:

```
CMAKE_CUDA_COMPILER:FILEPATH=/usr/local/cuda/bin/nvcc
CUDA_TOOLKIT_ROOT_DIR:UNINITIALIZED=/usr/local/cuda
```

Both point at CUDA 13.0 consistently.

**Fix B — CUDA OpenCV wiring (`LRS_GB10_OPENCV_DIR`):**

New variable added to the script (line ~28–33):

```bash
LRS_GB10_OPENCV_DIR="${LRS_GB10_OPENCV_DIR:-/opt/gb10-cuda/install/opencv}"
```

In `configure()`, before the `cmake` invocation:

```bash
local opencv_args=()
local _opencv_cmake_dir="${LRS_GB10_OPENCV_DIR}/lib/cmake/opencv4"
if [[ -n "${LRS_GB10_OPENCV_DIR:-}" && -f "${_opencv_cmake_dir}/OpenCVConfig.cmake" ]]; then
    opencv_args=(-DOpenCV_DIR="${_opencv_cmake_dir}")
    echo "LRS_GB10: using CUDA OpenCV from ${_opencv_cmake_dir}"
elif [[ -n "${LRS_GB10_OPENCV_DIR:-}" ]]; then
    echo "LRS_GB10: WARNING: ... not found; falling back to system OpenCV"
fi
```

The array is appended to the cmake invocation as `"${opencv_args[@]}"`.
Guard: the cmake config file must exist — set `LRS_GB10_OPENCV_DIR=""` to
fall back to system OpenCV.

### 1.2 Configure-only proof (Deliverable 2)

Run command:

```bash
LRS_GB10_BUILD_DIR=/tmp/lrs-cuda-cfgtest \
  LRS_GB10_PREFIX=/tmp/lrs-cfg-prefix \
  LRS_GB10_WITH_DDS=OFF \
  bash scripts/build-dgx-spark-gb10.sh configure
```

CMake output line confirming CUDA OpenCV is found:

```
-- Found OpenCV: /opt/gb10-cuda/install/opencv (found version "4.14.0")
```

CMakeCache entries confirming CUDA path consistency:

```
CMAKE_CUDA_COMPILER:FILEPATH=/usr/local/cuda/bin/nvcc
CUDA_TOOLKIT_ROOT_DIR:UNINITIALIZED=/usr/local/cuda
```

No full build was run (HIL-unvalidatable with dead xHCI).

### 1.3 `bin/rs_gpu_preview.py` — GPU frame-processing helper

Path: `~/realsense-gb10-validation/bin/rs_gpu_preview.py`

Provides `RsGpuPreview.process(depth_z16, color_bgr)` → `(depth_preview, color_preview)`:

- **GPU path** (`cv2.cuda` available, `getCudaEnabledDeviceCount() > 0`):
  1. Upload `depth_z16` (uint16 → float32) to `GpuMat` via `.upload()`
  2. `cv2.cuda.normalize(gpu_depth, 0, 255, NORM_MINMAX, CV_8U)` → uint8 GpuMat
  3. `cv2.cuda.resize(gpu_norm, preview_size, INTER_LINEAR)` → resized GpuMat
  4. `.download()` to host + `cv2.applyColorMap(…, COLORMAP_JET)` (CPU; see note below)
  5. Upload color BGR GpuMat, `cv2.cuda.resize`, `.download()`
- **CPU fallback** (no CUDA device or `use_gpu=False`):
  numpy normalize + `cv2.resize` + `cv2.applyColorMap` — identical output
- **GpuMat buffers** are held as instance state to avoid per-frame allocation.
  The normalize/resize functions return new GpuMats (the `dst=` kwarg form of
  `cv2.cuda.normalize` is broken in this build — returns a `(0,0)` GpuMat).

**NVENC compressed preview** (two documented paths — see module docstring):

- Option A: `cv2.cudacodec.createVideoWriter("out.mp4", frameSize, Codec_H264, 30, ColorFormat_BGR)` + `writer.write(gpu_color_gpumat)` — encodes directly from GpuMat, zero D2H for the encode step.
- Option B: pipe raw BGR frames to `/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg -vcodec h264_nvenc` via `subprocess.Popen` — simpler when the frame is already on host.

---

## 2. Measured selftest timing (Deliverable 3, per CLAUDE.md rule #4)

```
Selftest: frame=848x480 -> preview=640x480  n_iter=200  warmup=3
cv2 version: 4.14.0-pre
CUDA devices: 1

CPU  resize+colormap: 0.854 ms/frame  (mean over 200 iters)
CUDA resize+colorize: 0.994 ms/frame  (mean over 200 iters)

Ratio CPU/CUDA: 0.86x  (CPU faster — H2D-dominated at this frame size)
```

**Interpretation (honest per roadmap §6 decision rule):**

At 848x480→640x480 preview, the CUDA path (H2D upload + GPU resize + D2H download + CPU applyColorMap) is ~16% *slower* than the CPU path. This matches the roadmap §6 prediction: "on GB10 the upload that usually kills small cv2.cuda ops may be cheap via ATS — so this is genuinely worth *measuring*, not dismissing." It was measured; for this specific op-mix the CPU wins at preview resolution.

**What would tip the balance in favour of GPU:**

1. `cv2.cuda.applyColorMap` — absent in this build's `cudaimgproc`. If present, it would move the most expensive per-pixel operation onto GPU and eliminate one extra D2H.
2. Larger frames or deeper GPU pipelines (SAM3 inference upstream) where the GpuMat is already resident and no H2D is needed for the preview step (the "upload-once, keep-on-GPU" pattern, §3 below).
3. A batch of frames or a sustained encode pipeline where NVENC amortizes setup overhead.

**Per CLAUDE.md rule #4:** this measurement is the evidence. Do NOT claim "GPU faster for preview" until a build with `cv2.cuda.applyColorMap` or an end-to-end pipeline measurement shows otherwise.

---

## 3. Upload-once, keep-on-GPU pattern for VIGIL

The goal (from roadmap §5 and the realsense.TODO.md "Performance Work") is:
**upload each color+depth frame once, then keep resize / preprocessing / inference / encoding on GPU — no re-upload for each consumer.**

Current working pattern (host numpy in, host preview out):

```python
# Works today — uses the existing process() API
from bin.rs_gpu_preview import RsGpuPreview
import numpy as np

preview = RsGpuPreview(preview_size=(640, 480))

# Inside the capture/callback loop:
depth_z16 = np.frombuffer(depth_frame.get_data(), dtype=np.uint16).reshape(h, w)
color_bgr  = np.frombuffer(color_frame.get_data(), dtype=np.uint8).reshape(h, w, 3)
depth_preview, color_preview = preview.process(depth_z16, color_bgr)
# depth_preview/color_preview are (ph, pw, 3) uint8 numpy arrays — ready for display.
```

**Future upload-once pattern (NOT YET IMPLEMENTED — requires a
`process_from_gpumat()` method to be added post-HIL):**

```python
# ILLUSTRATIVE — process_from_gpumat() does not yet exist.
# This shows the target design once the VIGIL bridge is built (roadmap §5).

# Step 1: Upload once per frame
gpu_depth = cv2.cuda.GpuMat(); gpu_depth.upload(depth_z16.astype(np.float32))
gpu_color = cv2.cuda.GpuMat(); gpu_color.upload(color_bgr)

# Step 2: Preview path — skip the upload inside process()
depth_preview, color_preview = preview.process_from_gpumat(gpu_depth, gpu_color)  # TODO

# Step 3: Inference path — pass gpu_color to SAM3/Torch without re-upload
#   torch_tensor = torch.as_tensor(gpu_color.download(), device='cuda')  # still a D2H copy
#   OR: DLPack/CUDA IPC to share the GpuMat buffer with Torch (zero-copy)
#   — this is the VIGIL bridge item (roadmap §5, effort: high)
```

The current `RsGpuPreview.process()` does its own upload from host numpy. For
the upload-once pattern, a future `process_from_gpumat(gpu_depth, gpu_color)`
method that skips the upload step would be the clean interface. This is
HIL-gated: measure per-frame H2D crossings with `nsys` on the live capture
loop before and after the bridge change.

---

## 4. Environment for CUDA cv2 under the uv venv (3.12)

The cv2 CPython 3.12 binding at:
```
/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages/cv2/python-3.12/cv2.cpython-312-aarch64-linux-gnu.so
```
was built against **numpy 1.26.4** (verified in CMakeCache:
`PYTHON3_NUMPY_VERSION:INTERNAL=1.26.4`). The uv venv shipped numpy 2.4.6
which is ABI-incompatible (hard failure: `numpy.core.multiarray failed to
import`). The venv numpy was deliberately downgraded:

```bash
uv pip install --python ~/realsense-gb10-validation/.venv/bin/python "numpy==1.26.4"
```

**Integration verified (no camera needed):** after downgrade, both cv2 AND
pyrealsense2 import cleanly in the same venv:

```bash
# cv2:
PYTHONPATH=/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages \
  LD_LIBRARY_PATH=.../opencv/lib:.../ffmpeg/lib:/usr/local/cuda/lib64 \
  .venv/bin/python -c "import cv2; print(cv2.__version__, cv2.cuda.getCudaEnabledDeviceCount())"
# -> 4.14.0-pre 1

# pyrealsense2 (build tree, no camera):
PYTHONPATH=/opt/vigil/build/librealsense-v2.58.1-dgx-spark-gb10/Release \
  LD_LIBRARY_PATH=.../Release \
  .venv/bin/python -c "import pyrealsense2 as rs; print(rs.__version__, len(rs.context().query_devices()))"
# -> 2.58.1 0   (0 devices expected: xHCI dead)
```

Both can coexist in the same interpreter. The numpy usage in the HIL harness
scripts (`asanyarray`, `astype`, `hstack`) is all numpy 1.x/2.x compatible.

**Why 1.26.4 is correct:** the cv2 .so ABI is compiled-in and cannot be changed
without rebuilding OpenCV. The downgrade is forced by the Codex session choosing
numpy 1.26.4 during the OpenCV 3.12 build. To get numpy 2.x back: rebuild the
cv2 CPython-3.12 binding from source with numpy 2.x headers, or use the 3.14
cv2 build (which was compiled against numpy 2.4.6 in the `media` venv).

**Required env for every Python invocation using the CUDA cv2:**

```bash
export PYTHONPATH=/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages
export LD_LIBRARY_PATH=/opt/gb10-cuda/install/opencv/lib:/opt/gb10-cuda/install/ffmpeg/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
```

These must be set BEFORE importing cv2. They can be placed in:
- `~/realsense-gb10-validation/scripts/realsense-gb10-env` (existing env wrapper) — recommended
- A `.env` file sourced by the HIL harness

The cv2 build info summary:

```
cv2 4.14.0-pre   CUDA: YES (ver 13.0)   GPU arch: 121   cuDNN: YES (9.23.0)
Modules: cudaarithm cudacodec cudafilters cudaimgproc cudawarping cudev dnn
```

---

## 5. What remains HIL-gated (resume after reboot)

| Item | Why gated | First test after reboot |
|------|-----------|------------------------|
| Live camera GPU preview via `rs_gpu_preview.py` | Needs D435 attached + healthy xHCI | `realsense-gb10-env python bin/rs_gpu_preview.py --selftest` then `rs-gb10-profiler --profile vga30` with preview callback |
| End-to-end timing: CPU path vs CUDA path on live frames | ATS coherence benefits only visible with real camera H2D + downstream inference | `nsys profile` on the capture+preview loop, measure cudaMemcpy bytes/frame |
| Upload-once bridge to SAM3 inference | VIGIL pipeline not testable without camera + inference stack | measure `nsys` crossings/frame before vs after bridge |
| NVENC encode latency | Needs sustained frame stream | `cv2.cudacodec.VideoWriter` write loop on 200 live frames |
| Verify `cv2.cuda.applyColorMap` absent / present | Check if a newer contrib build adds it | `hasattr(cv2.cuda, 'applyColorMap')` after any opencv rebuild |
| Full librealsense rebuild with CUDA OpenCV 4.14 | Build takes 30–60 min; not validatable until camera confirmed stable | Run `scripts/build-dgx-spark-gb10.sh all` after successful `validate` mode on the installed SDK |

---

## 6. Files changed / created

| File | Change |
|------|--------|
| `~/dev/repos/librealsense/scripts/build-dgx-spark-gb10.sh` | Fix `CUDA_HOME` default (l.7); add `LRS_GB10_OPENCV_DIR` var (l.28–33); add `opencv_args[]` array + guard (l.149–161); append to cmake invocation (l.201–202); update `usage()` docs |
| `~/realsense-gb10-validation/bin/rs_gpu_preview.py` | New file — GPU preview helper with CPU fallback, NVENC docs, selftest |
| `~/realsense-gb10-validation/.venv` | `numpy` downgraded 2.4.6→1.26.4 (required for ABI compat with cv2 cpython-312 .so built against numpy 1.26.4) |
| `~/realsense-gb10-validation/ANALYSIS-20260602/90-cuda-integration.md` | This document |
