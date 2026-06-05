# GB10 Frame/Video Quality — measured data (2026-06-03)

> **Historical snapshot (2026-06-03).** The NVENC quality data and verdict (SSIM/XPSNR tables) remain
> valid. The "tune cq/preset by perceptual metric" recommendation has been actioned: **cq sweep complete,
> cq=23/p4 is the deployed `--record` default** (see [nvenc-cq-sweep.md](nvenc-cq-sweep.md)).

**Directive: get real data on frame/video rendering, don't assume it renders properly.**
Host `spark-3066`, D435 on the clean USB-3 bus, full CUDA build (`build-gb10-full`, RSUSB +
CUDA + pyrealsense2 3.12.3), CUDA OpenCV 4.14 (`cv2.cuda`), NVENC ffmpeg. Single color stream
848×480@30 (no controller risk; ran GREEN, 0 `-110`). Harnesses: `scripts/gb10/rs-gb10-quality-hil.py`
(`just hil-quality`) and `scripts/gb10/rs-gb10-nonheadless-verify.py` (`just hil-nonheadless`).

PSNR/SSIM are **full-reference** — each measurement below states its reference explicitly, and
no-reference metrics are used where there is no ground truth. (libvmaf is not compiled into this
ffmpeg; SSIM/PSNR/XPSNR are.) `cv2.quality`/skimage are absent — SSIM is a numpy implementation,
PSNR is `cv2.PSNR`/ffmpeg.

## (a) NVENC encode fidelity — reference = lossless ffv1 of the raw capture, 120 frames
| Codec | SSIM | PSNR (dB) | XPSNR (dB, perceptual) | Size | Ratio vs lossless |
|-------|------|-----------|------------------------|------|-------------------|
| h264_nvenc (p5, cq23) | **0.965** | **38.7** | 30.8 | 1.06 MB | 34× |
| hevc_nvenc (p5, cq23) | **0.971** | **39.2** | 31.8 | 0.91 MB | 40× |
| (lossless ffv1 ref) | 1.000 | ∞ | — | 36.6 MB | 1× |

Both pass SSIM/PSNR visually-lossless thresholds (SSIM > 0.95, PSNR > 35 dB) at 34–40× compression.
**Yellow flag (foregrounded for a medical pipeline):** the perceptually-weighted **XPSNR is ~31 dB**
(h264 30.8 / hevc 31.8) — in the literature that is "good", **not** "excellent", and the gap between
SSIM-says-great and XPSNR-says-only-good means **`cq23` is likely leaving perceptual quality on the
table** for the 34× ratio. For medical imaging, run a **cq/preset sweep** and pick by XPSNR, not SSIM.
**Conclusion: the GPU encoder preserves frames well, but the operating point is not yet tuned for
perceptual quality** (single ~4 s clip, one scene; codec-comparison thresholds).

## (b) GPU-vs-CPU — fidelity AND whether the GPU is actually used
`cv2.cuda.cvtColor(BGR2GRAY)` vs CPU per frame: **PSNR 361 dB, SSIM 1.000, max abs diff 0** — the GPU
output is **bit-identical** (fidelity verified). BUT a 361 dB / invariant result is also the signature
of a no-op, so on-device execution was checked separately (camera-free, 3008² workload):
| op | GPU ms/op | CPU ms/op | speedup | reading |
|----|-----------|-----------|---------|---------|
| `cvtColor` BGR2GRAY | 0.80 | 0.38 | **0.5× (GPU SLOWER)** | trivial memory-bound op; GB10 unified-mem dispatch overhead dominates |
| `resize` 2× upscale | 6.2 | 23.5 | **3.8× (GPU faster)** | heavy op → real GPU win, proves cv2.cuda is on-device (not a fallback) |

**So: cv2.cuda genuinely runs on the GPU (resize 3.8×), but per-op color-converts do NOT benefit on
GB10 — they are slower than CPU.** The acceleration win requires **heavy ops and keep-on-GPU pipelines**
(upload once, chain ops, download once), NOT per-frame trivial cuda calls. **Scope caveat:** only
`cvtColor`/`resize` were tested head-to-head; the ops vigil actually depends on — `rs.align`(depth→color),
`rs.colorizer`, pointcloud — were **not** benchmarked GPU-vs-CPU here and remain to be validated
on-device (next task). `cv2.cuda.applyColorMap` + the `cv2.quality` module are absent from this build.

## (c) No-reference capture quality — 120 live frames (scene-dependent)
| Metric | mean | min | p05 | reading |
|--------|------|-----|-----|---------|
| Sharpness (Laplacian variance) | 417.7 | 402.5 | 404.0 | ≫100 ⇒ in-focus, textured |
| Brightness (gray mean) | 109.2 | 107.2 | 107.6 | mid-range, good exposure |
| Contrast (gray std) | 73.2 | 71.7 | 71.7 | healthy dynamic range |
| Saturated/clipped fraction | 0.002 | 0.001 | 0.001 | 0.2 % ⇒ minimal clipping |
These are **scene-dependent** (whatever the camera framed), not an absolute camera grade — but they
confirm the live frames are sharp and well-exposed for this scene.

## Non-headless render verification (`$DISPLAY=:1`, active X11)
Painted 120 frames to an on-screen cv2 window and `ffmpeg x11grab`-captured the screen:
**non-blank (mean 91.7, std 76.8) ⇒ real pixels rendered.** Visual confirmation: the grab shows the
window displaying a sharp, correctly-colored D435 frame with the `GB10 RENDER` overlay — i.e. the
on-screen render genuinely works (verified by eye, not assumed). The automated window-region SSIM
match did not locate the WM-decorated window by fixed offset (−0.036) — a harness refinement
(locate the window via `xdotool`/`wmctrl` geometry); it is NOT a render fault.

## Verdict
- Video encode: **frames preserved** (SSIM ≥ 0.965) at 34–40×, but **XPSNR ~31 dB ⇒ tune cq/preset by
  perceptual metric before medical use** — measured.
- cv2.cuda: **confirmed on-GPU** (resize 3.8× vs CPU) and bit-identical output; **but trivial per-op
  color-converts are slower on GB10** (unified-mem overhead) — accelerate via heavy/keep-on-GPU only.
  vigil's `align`/`colorize`/pointcloud **not yet benchmarked GPU-vs-CPU** (next task).
- Live capture: **sharp + well-exposed** (scene-dependent) — measured + visually confirmed.
- On-screen (non-headless) render: **confirmed** (non-blank grab + I looked at it: real frame + overlay).

## Open / follow-ups
- libvmaf into the gb10-cuda ffmpeg for the perceptual VMAF metric (currently SSIM/PSNR/XPSNR only).
- `cv2.cuda.applyColorMap` + `cv2.quality` (BRISQUE/SSIM) modules absent — rebuild OpenCV with the
  `cudaimgproc` + `quality` contrib modules.
- Non-headless auto-SSIM: locate the window via `xdotool getwindowgeometry` for an exact region match.
- Quality soak over varied scenes/lighting + a depth-accuracy metric (vs a known planar target).
