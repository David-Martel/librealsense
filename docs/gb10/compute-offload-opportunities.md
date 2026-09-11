# What librealsense can take off vigil-spark's compute budget

Survey against the deployed fork (`v2.58.4`, `LRS_GB10_FORCE_RSUSB=ON`,
`BUILD_WITH_CUDA=ON`, `BUILD_WITH_NEON=ON`) and the live consumers in
`vigil-spark`, 2026-09-11. Every claim below is verified in one of the two
trees or by enumeration on `spark-3066`, with the path cited.

## The headline inverts the question

**librealsense is not where the compute burden is, and it cannot touch the part
that is.** MediaPipe inference dominates:

| stage | cost | where |
|---|---|---|
| MediaPipe pose | **~26 ms/frame** | `person_segmentation_node.py` header |
| MediaPipe hand mask | **~29 ms of a 33 ms budget** | `chest_rise_node.py` own comment |
| CUDA `align` | **0.29-0.32 ms** | `docs/gb10/benchmarks.md` §9 |

There is no cuDNN, TensorRT, NPP or nvJPEG in the SDK. So the SDK levers worth
spending time on are the ones that **avoid wasted 26 ms inferences** or
**replace an inference outright** — not the ones that shave sub-millisecond
depth ops. This ranking reflects that, and it is not the ranking a
"what has a CUDA path" survey would produce.

## The structural observation

**The node aligns 307,200 depth pixels every frame; its three consumers need a
9x9 patch and two rectangle means.**

- `realsensenode.py` enables colour+depth 640x480, builds
  `realsense.align(realsense.stream.color)`, and runs it on every frame.
- `person_segmentation_node.depth_at_bbox()` takes the median of a **9x9 patch**
  (`depth_patch_half_px` 4) at the bbox centroid. One scalar.
- `chest_rise_node` uses a **percent-ROI rectangle** plus a reference region.
  Region means only.
- `eti_sniffer` is a third subscriber on the same topic.

`align` still earns its keep — the bbox and ROI are produced in *colour* space
by MediaPipe, so depth must be in colour coordinates to index them — and at
0.3 ms on CUDA it is not worth removing. But it means **no SDK block that
reduces depth arithmetic can matter much**: that arithmetic is already ~1% of
the frame.

## vigil-spark currently uses ZERO processing blocks

Verified: the only librealsense surface used is `pipeline`, `config`, and
`align`. Every block below is present in the **deployed** bindings on
spark-3066 and none is used:

```
decimation_filter  temporal_filter  spatial_filter  threshold_filter
hole_filling_filter  disparity_transform  hdr_merge  pointcloud
rs2_deproject_pixel_to_point  rs2_project_point_to_pixel
```

## Ranked opportunities

### 1. Replace the chest-rise hand network with a depth-plane test

**The single biggest win available, and it removes a network rather than
speeding one up.**

`chest_rise_node.py` calls `self._hand_detector.detect_mask(rgb, ...)` every
sample — a MediaPipe hand-landmarker, annotated in that file as the "~26 ms"
call. Its only output is a boolean `ignore_mask` fed to `mean_depth_in_roi`,
`mean_depth_by_lateral_half`, and `mean_reference_depth_outside_roi`.

Excluding hands looks like an appearance problem. It is not: **a hand occluding
the chest is nearer the camera than the chest plane.** The design envelope is
3-10 mm of chest rise (`constants.py`), while a hand sits ~30 mm+ proud — an
order of magnitude of separation. A per-pixel deviation test against a plane
fitted to the ROI produces the same mask from data already in hand.

The seam is clean: `ignore_mask` is a plain boolean array, `True` = ignore,
shape-checked against its operand.

**Two complications this proposal must solve, found while checking it:**

1. `mean_reference_depth_outside_roi` takes a **full-frame** mask, but a
   chest-plane fit is only defined *inside* the ROI. The reference region needs
   either a separate treatment or an explicit `None`.
2. `hand_pixels_pct` is published telemetry and is fed to
   `ChestRiseAnalyzer.add_sample`. Changing it from "hand pixels" to "depth
   outlier pixels" changes the meaning of a signal a clinician-facing validator
   consumes.

**This must be validated against recorded data before it ships** — agreement
between the two masks is an empirical question, and this is a medical
procedure. `rs-record`/playback (`.db3`, rosbag2) gives a deterministic harness
for exactly that comparison.

### 2. Auto-exposure ROI, AE limit, and visual preset — MEASURED AND LARGELY REJECTED

**Update 2026-09-11, after measuring on spark-3066.** This was ranked #2 on the
reasoning below. Three of its four claims did not survive contact with the
device. Read this block before acting on the rationale that follows it.

Live option state, independently verified: `visual_preset` = 0 (CUSTOM),
`auto_exposure_limit_toggle` = 0, `auto_exposure_limit` = 165000 us,
depth `exposure` = 8500 us with AE on, colour `exposure` = 166 us with AE on.

**(a) The AE-limit toggle cannot be set. Silent write refusal.**
`set_option(auto_exposure_limit_toggle, 1.0)` returns without raising and reads
back **0**, in every combination tried: toggle-then-limit, limit-then-toggle,
while streaming, while not streaming, and with AE disabled first. The *limit
value* writes fine (165000 -> 16000); the toggle that would arm it does not.

This is precisely the failure class `realsensenode._set_verified_option()`
exists to catch. A naive `set_option` here would have looked like it worked and
done nothing, forever.

**(b) The rationale for the AE limit — "silent fps halving in dim rooms" — does
not apply to this device.** Measured directly by forcing manual depth exposure
past the frame period, 10 s per point:

| depth exposure | achieved fps | gap p50 | valid px |
|---:|---:|---:|---:|
| 8 500 us | 59.60 | 16.81 ms | 77.65% |
| 16 000 us | 59.60 | 16.80 ms | 79.14% |
| 20 000 us | 59.60 | 16.80 ms | 77.87% |
| 33 000 us | 59.60 | 16.80 ms | 73.37% |
| 60 000 us | 59.60 | 16.81 ms | 59.81% |
| 100 000 us | 59.60 | 16.79 ms | 43.86% |

**Frame rate is flat from 8.5 ms to 100 ms** — six times the 16.67 ms frame
period. The D435 does not trade frame rate for exposure at 60 fps. So the
uncapped 165 ms limit is not a threat to the rate this campaign bought.

What long exposure *does* cost is **depth validity**, monotonically: 77.65% ->
43.86%. That is a real effect and a milder problem, and AE already sits near
its optimum (8 500 us, with the best fill at 16 000).

**(c) `HIGH_DENSITY` does not raise valid-depth coverage here — it lowers it.**
12 s per leg on one open pipeline, options changed without restarting (repeated
probe-commit is what wedges the xHCI controller on this fleet):

| preset | valid px | vs baseline |
|---|---:|---:|
| CUSTOM (as deployed) | **93.72%** | — |
| HIGH_DENSITY | 93.01% | **-0.71 pts** |
| HIGH_ACCURACY | 78.30% | **-15.42 pts** |

The deployed CUSTOM preset already beats HIGH_DENSITY. HIGH_ACCURACY trades
away 15 points of fill, which is the wrong direction for consumers whose
failure mode is `return None` on an empty patch. **Set no preset.**

**(d) Colour exposure is already 166 us**, so the "pin a short colour exposure
to sharpen temporal alignment" idea is already banked and yields nothing today.
It survives only as a *guard*: AE may legally raise colour to 10 ms, which
would matter. Pin it so AE cannot; do not expect a win now.

**What survives:** the AE **ROI** (`rs2_set_region_of_interest`), which was not
measured and is the one item here still worth testing — constraining AE to the
patient region is about *where* the exposure is metered, which none of the above
tested. The original reasoning for it follows.

---

#### Original reasoning (AE ROI only; the rest is superseded above)

The node sets **only** `inter_cam_sync_mode`, `global_time_enabled`, and
`frames_queue_size`. No visual preset, no AE ROI, no AE limit, no exposure —
and `_set_verified_option()` (set-then-read-back) is already there as the
insertion point.

- `rs2_set_region_of_interest` — D435 supports AE ROI on **both** sensors
  (colour gated at fw >= 5.10.9.0; this fleet is 5.17.3.10).
- `RS2_OPTION_AUTO_EXPOSURE_LIMIT` — gated at fw >= 5.12.10.11 + global
  shutter; passes. **This is the classic cause of silent fps halving in dim
  rooms**, so it protects the 60 Hz that §26 just bought.
- `RS2_OPTION_VISUAL_PRESET` (`HIGH_DENSITY`) — raises valid-depth coverage,
  which directly reduces the `return None` path in `depth_at_bbox`.

Constraining AE to the patient region raises landmark quality, and a
low-confidence frame costs a **full 26 ms inference for a discarded result**.
The win is statistical rather than a fixed ms/frame, so it needs an A/B on
valid-pixel fraction and landmark confidence, not an assertion.

Caveat: `VISUAL_PRESET` is only registered when advanced mode is enabled, and
D435's valid preset set excludes `REMOVE_IR_PATTERN`.

### 3. Frame metadata as an inference gate

The cheapest possible win: read per-frame metadata and **skip the 26 ms
inference** on frames that cannot produce a usable result. This is the only
lever besides #1 that removes whole inferences, and the 60/30 asymmetry makes
it cheap — depth-side work is spent against a 16.67 ms budget that nothing else
is competing for.

### 4. Decimation filter

Now affordable in a way it was not at 30 Hz: ~0.5 ms of scalar filtering is 3%
of a 16.67 ms budget, spent on the cheap side of the asymmetry.

### 5. Point cloud — accelerated, and currently unused

`pointcloud::create()` dispatches to `pointcloud_cuda` when
`rs2_is_cuda_available()`, falling back to SSE then NEON
(`src/proc/pointcloud.cpp`). So the CUDA path is real and reaches the deployed
bindings.

**But nothing in vigil-spark needs it.** The consumers want ~85 pixels, not
307,200 points. Recorded here so the next person does not spend a day wiring up
an accelerated block for a pipeline that has no use for its output.

### 6. `rs2_deproject_pixel_to_point` — a correctness gap, not a speed one

`lower_limb/aligned_depth_geometry.deproject_aligned_pixel` is a hand-rolled
pinhole deprojection that **ignores distortion entirely**. That is not
academic here: `realsensenode._standard_distortion` *correctly refuses* to
publish this camera's model as ROS `plumb_bob`, because it is
`inverse_brown_conrady` (model=2) and plumb_bob means the forward transform.
That refusal is right — and it means the standard `CameraInfo` path is disabled
for this camera, so a consumer doing its own pinhole math has no distortion
coefficients to apply even if it wanted to.

`rs2_deproject_pixel_to_point` handles `inverse_brown_conrady` natively and is
exposed in the deployed bindings. Using it requires plumbing `rs2_intrinsics`
(or the five coefficients plus the model) through to the consumer, which the
current `DepthCalibrationMessage` does not carry.

**This is an accuracy finding, not a performance one** — metres-in-space error
at the frame edges — and it belongs to the `lower_limb` owners.

## Does NOT apply — recorded so nobody re-runs it

- **Nothing reduces the 4.2 ms depth/colour skew.** `rs2::syncer` only *pairs*
  nearest frames; it selects, it cannot shift a capture instant. Two
  free-running imagers with independent readout is structural.
  `inter_cam_sync_mode` is already measured inert (§26.2).
  - Worth knowing: **colour exposure time sets the temporal *width* of a colour
    frame.** If AE integrates for 20-30 ms, the 4.2 ms pairing residual is
    already below the noise floor of what "alignment" means. Pinning a short
    fixed `RS2_OPTION_EXPOSURE` on colour genuinely sharpens effective temporal
    alignment — and pairs with the AE-limit item above.
  - For **chest rise specifically the skew is irrelevant**: at 0.2-0.5 Hz the
    chest moves microns in 4 ms. It matters only for fast hand motion.
- **No pose/tracking from the SDK.** The T265 tracking camera is a different
  product and its pose features are not available here.
- **No IMU.** D435 has none — that is the D435i.
- **`BACKEND_TIMESTAMP` reads 0 under RSUSB.** Use `SENSOR_TIMESTAMP`, which is
  populated on both streams and turns the unknown 4.2 ms into a *known*
  per-pair correction that can be compensated downstream.
- **Threshold filtering cannot replace the hand mask** as a drop-in — see #1
  for why the depth-plane formulation is the version that works.

## What to do first

1. ~~Measure the AE/preset A/B (#2).~~ **DONE, and it closed the question the
   other way** — the toggle cannot be set, the fps rationale is refuted, and
   HIGH_DENSITY is worse than what is deployed. Only the AE **ROI** is still
   open. This is what "either justifies itself or closes the question" looks
   like when the answer is no.
2. **Prototype #1 against recorded data**, not live. It is the biggest win and
   the only one that removes a network, but it changes a clinician-facing
   signal and must be shown to agree with the mask it replaces.
3. Leave #5 alone, and route #6 to the `lower_limb` owners as a correctness
   item.
