# GB10 Frame/Video Quality — measured data (2026-06-03)

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

Both pass visually-lossless thresholds (SSIM > 0.95, PSNR > 35 dB) at 34–40× compression. HEVC
edges H264, as expected. XPSNR (perceptually weighted, more conservative) ≈ 31 dB — still good.
**Conclusion: the GPU video encoder preserves the captured frames; video encode is real and good,
not assumed.** (Single ~4 s clip of one scene; thresholds are codec-comparison conventions.)

## (b) GPU-vs-CPU render fidelity — reference = the CPU result, per frame
`cv2.cuda.cvtColor(BGR2GRAY)` vs CPU `cv2.cvtColor`: **PSNR 361 dB, SSIM 1.000** (bit-identical).
The CUDA render path produces the same pixels as CPU — the acceleration is correct, not lossy.
(Note: `cv2.cuda.applyColorMap` is absent in this OpenCV build → CPU colormap fallback; tracked.)

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
- Video encode: **real & visually-lossless** (SSIM ≥ 0.965) at 34–40× — measured.
- GPU render path: **bit-identical to CPU** — measured.
- Live capture: **sharp + well-exposed** — measured + visually confirmed.
- On-screen (non-headless) render: **confirmed** (non-blank grab + visual).

## Open / follow-ups
- libvmaf into the gb10-cuda ffmpeg for the perceptual VMAF metric (currently SSIM/PSNR/XPSNR only).
- `cv2.cuda.applyColorMap` + `cv2.quality` (BRISQUE/SSIM) modules absent — rebuild OpenCV with the
  `cudaimgproc` + `quality` contrib modules.
- Non-headless auto-SSIM: locate the window via `xdotool getwindowgeometry` for an exact region match.
- Quality soak over varied scenes/lighting + a depth-accuracy metric (vs a known planar target).
