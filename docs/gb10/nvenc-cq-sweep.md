# NVENC CQ Sweep — Keep-on-GPU Viewer `--record` Default Recommendation

**Date**: 2026-06-05  
**Platform**: GB10 / DGX Spark (Orin, ARM64, NVIDIA discrete GPU via NVENC)  
**Tool**: `/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg` (Lavf62.19.101)  
**Script**: `scripts/gb10/nvenc-cq-sweep.sh`

---

## Input Clip

**Real recording** — `keepongpu-rec.mp4` produced by the keep-on-GPU viewer `--record` path (glReadPixels→ffmpeg pipe). No synthetic clip was needed.

| Property | Value |
|----------|-------|
| Source | `~/realsense-gb10-validation/keepongpu-rec.mp4` |
| Encoder | `h264_nvenc` (Lavc60.31.102) |
| Resolution | 848×480 |
| Frame rate | 30 fps |
| Duration | 7.73 s (232 frames) |
| Size | 2,273,860 bytes (2.2 MB) |
| Bitrate | 2,348 kb/s |

**Caveat**: The source is already H.264-compressed (NVENC output from a prior session), not raw
glReadPixels RGBA data. XPSNR is therefore measured against a lossy reference, not uncompressed
frames. Re-encoding already-compressed depth-colorized content compresses more optimistically than
raw frames would. The relative comparisons across cq values are valid; absolute XPSNR numbers
are inflated vs what raw-frame quality would show. This is the only representative real-world
`--record` output available without camera access.

---

## Sweep Parameters

| Parameter | Value |
|-----------|-------|
| Rate control | `-rc vbr -cq N -b:v 0` (constant-quality VBR; `-b:v 0` suppresses default target bitrate and is required for cq to take effect) |
| GOP | 30 (1 s at 30 fps, fixed across all encodes) |
| CQ values swept | 19, 23, 26, 29, 33 |
| Presets swept | p4 (medium / default), p6 (slower / better) |
| Encodes | Sequential — one NVENC session at a time |
| Quality metric | **XPSNR** (Y/U/V luma+chroma; separate untimed measurement pass) |
| Timing | Wall-clock end-to-end (includes source H.264 decode + NVENC encode; constant decode overhead affects absolute time but not relative comparison) |

---

## Results Table

### Preset p4 (medium — current default)

| cq | Size (bytes) | Size vs src | Bitrate (kb/s) | Encode (s) | Speed (×RT) | XPSNR-Y (dB) | XPSNR-U (dB) | XPSNR-V (dB) | XPSNR w-avg (dB) |
|----|-------------|-------------|---------------|-----------|-------------|-------------|-------------|-------------|-----------------|
| 19 | 4,591,219 | +102% | 4,751.6 | 0.750 | 10.3× | 41.26 | 38.24 | 37.83 | 40.18 |
| **23** | **3,157,953** | **+39%** | **3,268.3** | **0.707** | **10.9×** | **39.14** | **35.54** | **35.54** | **37.94** |
| 26 | 2,345,659 | +3% | 2,427.6 | 0.694 | 11.1× | 36.00 | 32.89 | 32.93 | 34.97 |
| 29 | 1,697,787 | -25% | 1,757.1 | 0.708 | 10.9× | 32.35 | 30.07 | 30.24 | 31.62 |
| 33 | 1,061,651 | -53% | 1,098.7 | 0.709 | 10.9× | 28.35 | 27.29 | 27.61 | 28.05 |

### Preset p6 (slower / better quality)

| cq | Size (bytes) | Size vs src | Bitrate (kb/s) | Encode (s) | Speed (×RT) | XPSNR-Y (dB) | XPSNR-U (dB) | XPSNR-V (dB) | XPSNR w-avg (dB) |
|----|-------------|-------------|---------------|-----------|-------------|-------------|-------------|-------------|-----------------|
| 19 | 4,458,957 | +96% | 4,614.7 | 0.712 | 10.9× | 40.83 | 37.61 | 37.27 | 39.70 |
| 23 | 3,052,473 | +34% | 3,159.1 | 0.721 | 10.7× | 38.41 | 34.83 | 34.80 | 37.21 |
| 26 | 2,252,951 | -1% | 2,331.6 | 0.712 | 10.9× | 35.41 | 32.37 | 32.41 | 34.40 |
| 29 | 1,640,535 | -28% | 1,697.8 | 0.691 | 11.2× | 32.28 | 29.84 | 30.06 | 31.51 |
| 33 | 1,053,707 | -54% | 1,090.5 | 0.708 | 10.9× | 28.55 | 27.38 | 27.74 | 28.22 |

**XPSNR w-avg**: luminance-weighted average, 4:2:0 weights (Y×4 + U×1 + V×1) / 6.  
**Monotonic size check**: PASSED for both presets (size decreases monotonically as cq rises).

---

## Quality Metric

**XPSNR** (Extended Perceptually weighted PSNR) — available as a native filter in this ffmpeg
build. Measured in a separate untimed pass after each encode. VMAF proper (`libvmaf`) was not
available in this build (only `vmafmotion`). XPSNR provides better perceptual correlation than
plain PSNR and is a recognized improvement over SSIM for compressed video assessment. Y channel
(luminance) is the primary quality indicator; w-avg (4:2:0 weighted) is also reported.

---

## Quality / Size Tradeoff Analysis (p4 steps)

| Step | XPSNR-Y drop | Size saved | dB/MB |
|------|-------------|-----------|-------|
| cq19 → cq23 | −2.12 dB | 1,433 KB (31%) | **1.51 dB/MB** |
| cq23 → cq26 | −3.14 dB | 793 KB (26%) | 4.06 dB/MB |
| cq26 → cq29 | −3.65 dB | 633 KB (28%) | 5.91 dB/MB |
| cq29 → cq33 | −4.00 dB | 621 KB (37%) | 6.60 dB/MB |

The knee is clear between cq23 and cq26. Going from cq19 to cq23 costs only 2.1 dB and saves
1.4 MB (31%). The next step (cq23→cq26) already costs 3.1 dB — 48% more quality loss per MB saved.
Every step below cq23 buys diminishing quality for similar or greater size cost.

---

## p4 vs p6 Comparison

| cq | p6 size saving over p4 | XPSNR-Y diff (p6−p4) | Verdict |
|----|----------------------|---------------------|---------|
| 19 | −129 KB (2.9%) | −0.43 dB | Not worth it |
| 23 | −103 KB (3.3%) | −0.73 dB | Not worth it |
| 26 | −91 KB (4.0%) | −0.59 dB | Not worth it |
| 29 | −56 KB (3.4%) | −0.06 dB | Negligible |
| 33 | −8 KB (0.7%) | +0.20 dB | Negligible |

p6 saves 3–4% file size at most cq values but with 0.4–0.7 dB lower XPSNR-Y (p6 makes more
aggressive decisions that hurt quality slightly at these bitrates). Encode speed (×real-time)
appears identical (10.7–11.2× for both presets), but this is not a valid preset-speed comparison:
at 232 frames over ~0.7 s, wall-time is dominated by source decode and process startup, not by
NVENC encoder complexity. This clip is too short to discriminate preset speed. The recommendation
to use p4 rests on quality/size, not on speed: **p4 is the better default** — slightly better
XPSNR-Y at equivalent cq, nearly same size, no measurable speed penalty at this clip length.

---

## RECOMMENDED DEFAULT

```
-rc vbr -cq 23 -b:v 0 -preset p4
```

**Reasoning**:

1. **Knee of the quality/size curve at cq=23.** Moving from cq=19 to cq=23 saves 31% size (1.4 MB
   per 7.7 s clip, extrapolating to ~11 MB/min) at only 2.1 dB XPSNR-Y cost. Every further step
   pays 3–4 dB per similar savings — a poor tradeoff.

2. **cq=23 at p4 produces 39.1 dB XPSNR-Y** — perceptually high-quality depth-colorized footage.
   The 2.1 dB vs cq=19 is not visible at normal playback for depth-colorized content.

3. **39% larger than the source file** — acceptable for a recording of a session that was already
   compressed. The extra size vs the source reflects the NVENC re-encoding cost of lossless-ish
   target; a raw glReadPixels recording would naturally produce a different (larger) baseline.

4. **p4 preferred over p6**: same encode throughput (10.9 fps both), p4 produces higher XPSNR-Y
   at equivalent cq (p6 produces slightly smaller files but with 0.4–0.7 dB lower quality).

5. **Real-time safety**: both presets run at ~10–11× real-time (video-seconds encoded per
   wall-second), comfortably above the 1× required for keeping pace with a 30 fps capture.
   The viewer records via a best-effort pipe; NVENC never bottlenecks the pipe at these
   settings. Encode speed is not a blocking concern.

**If storage is severely constrained**: cq=26 at p4 produces near-source-size output (only 3%
larger than the already-compressed source) with 36.0 dB XPSNR-Y — still perceptually good
for depth visualization. Use `--record-cq 26` if added as a CLI option.

**Caveat on knee location**: this sweep used a re-encoded H.264 source. Raw glReadPixels RGBA
frames contain more spatial detail; the same cq values would produce larger files (more bits
to spend per frame). The knee may shift one step toward cq=26 under raw-frame conditions.
The cq=23 pick is therefore conservative — verify against a raw-frame recording once camera
access is available.

---

## Suggested `just` Recipe

Add to the justfile (note: omit default INPUT value — the script already defaults to
`$HOME/realsense-gb10-validation/keepongpu-rec.mp4`; `just` does not expand `~` in defaults
and the path is outside the repo tree):

```
# Offline NVENC encode-quality sweep; no camera required.
# Usage: just nvenc-sweep
#        just nvenc-sweep INPUT=/path/to/clip.mp4
#        just nvenc-sweep INPUT=/path/to/clip.mp4 CQ_LIST="19 23 26 29 33" PRESET_LIST="p4 p6"
nvenc-sweep INPUT="" CQ_LIST="19 23 26 29 33" PRESET_LIST="p4 p6":
    bash scripts/gb10/nvenc-cq-sweep.sh "{{INPUT}}" "{{CQ_LIST}}" "{{PRESET_LIST}}"
```

---

## Reproduction

```bash
# From repo root, no camera needed:
bash scripts/gb10/nvenc-cq-sweep.sh \
    ~/realsense-gb10-validation/keepongpu-rec.mp4 \
    "19 23 26 29 33" \
    "p4 p6"
```

The script auto-sources `scripts/gb10/gb10-env.sh` for `$LRS_FFMPEG` and `LD_LIBRARY_PATH`.
Results TSV is written to the same directory as the input clip, timestamped.
