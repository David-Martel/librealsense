#!/usr/bin/env python3
"""
rs_gpu_preview.py — GPU-accelerated RealSense frame preview helper.

Provides colorize+resize of depth (Z16) and optional passthrough of color
frames using cv2.cuda (GpuMat) when a CUDA device is available, with a
transparent CPU fallback when it is not.

Environment (required for CUDA cv2 under the uv venv):
    PYTHONPATH=/opt/gb10-cuda/install/opencv/lib/python3.12/site-packages
    LD_LIBRARY_PATH=/opt/gb10-cuda/install/opencv/lib:/opt/gb10-cuda/install/ffmpeg/lib:/usr/local/cuda/lib64:$LD_LIBRARY_PATH

Usage as a module:
    from bin.rs_gpu_preview import RsGpuPreview
    preview = RsGpuPreview(preview_size=(640, 480))
    depth_preview, color_preview = preview.process(depth_z16, color_bgr)

Usage from the command line (selftest on synthetic frames):
    python rs_gpu_preview.py --selftest [--preview-size 640x480] [--iterations 200]

NVENC compressed preview path (two options):
    Option A — cv2.cudacodec.VideoWriter (if available in this build):
        writer = cv2.cudacodec.createVideoWriter(
            "preview.mp4",
            frameSize=(width, height),
            codec=cv2.cudacodec.Codec_H264,
            fps=30.0,
            colorFormat=cv2.cudacodec.ColorFormat_BGR,
        )
        gpu_color = cv2.cuda.GpuMat(); gpu_color.upload(color_bgr_frame)
        writer.write(gpu_color)  # encodes directly from GpuMat via NVENC

    Option B — pipe to /opt/gb10-cuda/install/ffmpeg/bin/ffmpeg with NVENC:
        import subprocess, sys
        enc = subprocess.Popen([
            "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg",
            "-y", "-f", "rawvideo", "-pixel_format", "bgr24",
            "-video_size", f"{width}x{height}", "-framerate", "30",
            "-i", "pipe:0",
            "-vcodec", "h264_nvenc", "-preset", "p4", "-b:v", "2M",
            "preview.mp4"
        ], stdin=subprocess.PIPE)
        enc.stdin.write(color_bgr_frame.tobytes())
        enc.stdin.close(); enc.wait()

Note: cudacodec.VideoWriter writes a GpuMat directly; the ffmpeg pipe
      path takes a host numpy frame.  Option A avoids any D2H for the
      encode step; Option B is simpler when the frame is already on host.

Constraint: xHCI controller is DEAD as of 2026-06-03.  This file contains
NO camera access; all code paths use synthetic numpy arrays only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# cv2 import — tolerates missing CUDA (falls back to CPU)
# ---------------------------------------------------------------------------

_CUDA_AVAILABLE: bool = False

try:
    import cv2

    _n_cuda = cv2.cuda.getCudaEnabledDeviceCount()
    _CUDA_AVAILABLE = _n_cuda > 0
except Exception as _cv2_err:  # noqa: BLE001
    # cv2 not importable (wrong PYTHONPATH/LD_LIBRARY_PATH, ABI mismatch, etc.)
    try:
        import cv2  # bare re-import in case only getCudaEnabledDeviceCount failed
    except ImportError:
        cv2 = None  # type: ignore[assignment]
    _CUDA_AVAILABLE = False


def _cv2_available() -> bool:
    return cv2 is not None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Depth colormap depth range in metres (Z16 units = mm; 0–10 m = 0–10000)
_DEPTH_MAX_MM: float = 10_000.0


# ---------------------------------------------------------------------------
# RsGpuPreview
# ---------------------------------------------------------------------------


class RsGpuPreview:
    """
    Stateful helper that holds GpuMat scratch buffers so repeated calls
    do not re-allocate GPU memory.  The upload-once pattern is explicit:
    callers hand in host numpy arrays; this class owns the GpuMat lifetime.

    Parameters
    ----------
    preview_size : (width, height)
        Output resolution for both depth colormap and color previews.
    use_gpu : bool | None
        True = require CUDA (raises if unavailable), False = force CPU,
        None (default) = use CUDA when available, CPU otherwise.

    GpuMat allocation notes (cv2 4.14.0-pre, verified 2026-06-03):
        _gpu_depth_8u and _gpu_color are upload targets that are reused
        across frames via .upload() — these avoid per-frame allocation.
        cv2.cuda.normalize() and cv2.cuda.resize() return NEW GpuMats on
        every call in this build (the dst= keyword form of normalize writes a
        (0,0) mat and is broken; the return-value form is the only working
        API).  So normalize/resize outputs are NOT reused across frames.
        A future build that adds dst= support for these ops would enable
        full scratch-buffer reuse.
    """

    def __init__(
        self,
        preview_size: Tuple[int, int] = (640, 480),
        use_gpu: Optional[bool] = None,
    ) -> None:
        self.preview_size = preview_size  # (w, h)

        if use_gpu is True and not _CUDA_AVAILABLE:
            raise RuntimeError(
                "use_gpu=True requested but cv2.cuda reports no CUDA device. "
                "Set PYTHONPATH and LD_LIBRARY_PATH for the gb10-cuda OpenCV; "
                "see module docstring."
            )
        self._use_gpu: bool = _CUDA_AVAILABLE if use_gpu is None else bool(use_gpu)

        # Upload-target GpuMats — reused across frames to avoid per-call allocation.
        # normalize/resize outputs are NOT reused (API returns new GpuMats each call).
        self._gpu_depth_8u: Optional[object] = None   # upload target for depth float32
        self._gpu_color: Optional[object] = None       # upload target for color BGR

        backend = "CUDA" if self._use_gpu else "CPU"
        print(f"[RsGpuPreview] backend={backend} preview_size={preview_size}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        depth_z16: np.ndarray,
        color_bgr: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Convert depth Z16 + color BGR to display-ready previews.

        Parameters
        ----------
        depth_z16 : np.ndarray  shape (H, W), dtype uint16
            Raw depth frame in Z16 format (millimetres, 0 = invalid).
        color_bgr : np.ndarray  shape (H, W, 3), dtype uint8
            Color frame in BGR order (RealSense default).

        Returns
        -------
        depth_preview : np.ndarray  shape (ph, pw, 3), dtype uint8
            False-colour depth image (JET colormap) at preview_size.
        color_preview : np.ndarray  shape (ph, pw, 3), dtype uint8
            Color image resized to preview_size.
        """
        if self._use_gpu:
            return self._process_gpu(depth_z16, color_bgr)
        return self._process_cpu(depth_z16, color_bgr)

    # ------------------------------------------------------------------
    # GPU path
    # ------------------------------------------------------------------

    def _process_gpu(
        self,
        depth_z16: np.ndarray,
        color_bgr: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """GPU path: upload once, normalize, colorize, resize, download."""
        assert cv2 is not None, "cv2 is None in GPU path"
        pw, ph = self.preview_size
        dsize = (pw, ph)

        # --- Depth: upload float32, normalize to uint8, apply JET colormap ---
        # cv2.cuda.normalize operates on single-channel GpuMat.
        # API note (verified 2026-06-03 against cv2 4.14.0-pre on GB10):
        #   cv2.cuda.normalize(src, alpha, beta, norm_type, dtype) -> dst  [returns new GpuMat]
        #   The dst= keyword form writes a (0,0) GpuMat and must NOT be used.

        depth_f32 = depth_z16.astype(np.float32)
        if self._gpu_depth_8u is None:
            self._gpu_depth_8u = cv2.cuda.GpuMat()
        self._gpu_depth_8u.upload(depth_f32)

        # Normalize to uint8 on GPU — returns a NEW GpuMat (type CV_8UC1).
        # The dst= keyword form of cv2.cuda.normalize is broken in this build
        # (returns a (0,0) mat); only the return-value form works.
        gpu_depth_norm = cv2.cuda.normalize(
            self._gpu_depth_8u, 0.0, 255.0, cv2.NORM_MINMAX, cv2.CV_8U
        )

        # Resize normalized depth — returns a new GpuMat.
        gpu_depth_resized = cv2.cuda.resize(
            gpu_depth_norm, dsize, interpolation=cv2.INTER_LINEAR
        )

        # Download to host and apply JET colormap (cv2.applyColorMap is CPU-only
        # in this build — cv2.cuda.applyColorMap is absent from cudaimgproc here)
        depth_8u_host = gpu_depth_resized.download()
        depth_preview = cv2.applyColorMap(depth_8u_host, cv2.COLORMAP_JET)

        # --- Color: upload BGR, resize on GPU, download ---
        if self._gpu_color is None:
            self._gpu_color = cv2.cuda.GpuMat()
        self._gpu_color.upload(color_bgr)

        # Returns a new GpuMat
        gpu_color_resized = cv2.cuda.resize(
            self._gpu_color, dsize, interpolation=cv2.INTER_LINEAR
        )
        color_preview = gpu_color_resized.download()

        return depth_preview, color_preview

    # ------------------------------------------------------------------
    # CPU fallback path
    # ------------------------------------------------------------------

    def _process_cpu(
        self,
        depth_z16: np.ndarray,
        color_bgr: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """CPU fallback: numpy normalize + cv2 resize + applyColorMap."""
        assert cv2 is not None, "cv2 is None in CPU path"
        pw, ph = self.preview_size
        dsize = (pw, ph)

        # Normalize depth to 0-255 uint8
        depth_f = depth_z16.astype(np.float32)
        d_max = depth_f.max()
        if d_max > 0:
            depth_8u = (depth_f * (255.0 / d_max)).astype(np.uint8)
        else:
            depth_8u = np.zeros_like(depth_f, dtype=np.uint8)

        depth_resized = cv2.resize(depth_8u, dsize, interpolation=cv2.INTER_LINEAR)
        depth_preview = cv2.applyColorMap(depth_resized, cv2.COLORMAP_JET)
        color_preview = cv2.resize(color_bgr, dsize, interpolation=cv2.INTER_LINEAR)
        return depth_preview, color_preview


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------


def _selftest(
    preview_size: Tuple[int, int] = (640, 480),
    frame_size: Tuple[int, int] = (848, 480),
    n_warmup: int = 3,
    n_iter: int = 200,
) -> None:
    """
    Run CPU vs CUDA timing on synthetic numpy frames (no camera needed).

    Timing model: each iteration is upload+normalize+resize+download (depth)
    and upload+resize+download (color).  The CPU fallback path is also timed
    for comparison.  Warmup iterations are discarded.

    Per CLAUDE.md rule #4: results are measured here and reported as measured.
    GPU may not win on small preview sizes with fast memcpy; report what is.
    """
    if not _cv2_available():
        print("ERROR: cv2 not importable.  Set PYTHONPATH and LD_LIBRARY_PATH.", file=sys.stderr)
        sys.exit(1)

    fw, fh = frame_size
    pw, ph = preview_size

    print(f"Selftest: frame={fw}x{fh} -> preview={pw}x{ph}  n_iter={n_iter}  warmup={n_warmup}")
    print(f"cv2 version: {cv2.__version__}")
    print(f"CUDA devices: {cv2.cuda.getCudaEnabledDeviceCount()}")
    print()

    # Synthetic frames — reproducible, no camera required
    rng = np.random.default_rng(42)
    depth_z16 = (rng.random((fh, fw)) * 8000.0).astype(np.uint16)
    color_bgr = rng.integers(0, 255, (fh, fw, 3), dtype=np.uint8)

    # ---- CPU timing ----
    cpu_preview = RsGpuPreview(preview_size=preview_size, use_gpu=False)

    # warmup
    for _ in range(n_warmup):
        cpu_preview.process(depth_z16, color_bgr)

    t0 = time.perf_counter()
    for _ in range(n_iter):
        dp, cp = cpu_preview.process(depth_z16, color_bgr)
    t_cpu = (time.perf_counter() - t0) / n_iter * 1000.0  # ms/frame

    print(f"CPU  resize+colormap: {t_cpu:.3f} ms/frame  (mean over {n_iter} iters)")
    print(f"  depth_preview shape={dp.shape} dtype={dp.dtype}  "
          f"color_preview shape={cp.shape} dtype={cp.dtype}")

    # ---- CUDA timing ----
    if not _CUDA_AVAILABLE:
        print()
        print("CUDA not available — skipping GPU path.")
        print("To enable: set PYTHONPATH and LD_LIBRARY_PATH (see module docstring).")
        return

    gpu_preview = RsGpuPreview(preview_size=preview_size, use_gpu=True)

    # warmup — mandatory: first CUDA call pays JIT / context-init overhead
    for _ in range(n_warmup):
        gpu_preview.process(depth_z16, color_bgr)

    # Synchronize before starting timer to flush any async work from warmup
    cv2.cuda.Stream_Null().waitForCompletion()

    t0 = time.perf_counter()
    for _ in range(n_iter):
        dp_gpu, cp_gpu = gpu_preview.process(depth_z16, color_bgr)
    # The .download() calls at the end of _process_gpu are synchronous (they
    # block until the GpuMat is ready on host) so no extra sync is needed here.
    t_gpu = (time.perf_counter() - t0) / n_iter * 1000.0

    print()
    print(f"CUDA resize+colorize: {t_gpu:.3f} ms/frame  (mean over {n_iter} iters)")
    print(f"  depth_preview shape={dp_gpu.shape} dtype={dp_gpu.dtype}  "
          f"color_preview shape={cp_gpu.shape} dtype={cp_gpu.dtype}")

    speedup = t_cpu / t_gpu
    print()
    print(f"Ratio CPU/CUDA: {speedup:.2f}x  "
          f"({'GPU faster' if speedup > 1.0 else 'CPU faster — likely small-frame or H2D-dominated'})")
    print()
    print("Note: CUDA timing includes H2D upload + GPU compute + D2H download.")
    print("On GB10 ATS (unified memory, coherent) H2D is cheap, but applyColorMap")
    print("still runs on CPU in this build (cuda.applyColorMap absent in cudaimgproc).")
    print("A future build with applyColorMap on GPU would improve the CUDA path further.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_size(s: str) -> Tuple[int, int]:
    w, h = s.lower().split("x")
    return int(w), int(h)


def main() -> None:
    parser = argparse.ArgumentParser(description="RealSense GPU preview helper selftest")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Run timing selftest on synthetic frames (no camera needed)",
    )
    parser.add_argument(
        "--preview-size",
        default="640x480",
        type=_parse_size,
        metavar="WxH",
        help="Preview output size (default: 640x480)",
    )
    parser.add_argument(
        "--frame-size",
        default="848x480",
        type=_parse_size,
        metavar="WxH",
        help="Input synthetic frame size (default: 848x480)",
    )
    parser.add_argument(
        "--iterations",
        default=200,
        type=int,
        help="Timing iterations (default: 200)",
    )
    args = parser.parse_args()

    if args.selftest:
        _selftest(
            preview_size=args.preview_size,
            frame_size=args.frame_size,
            n_iter=args.iterations,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
