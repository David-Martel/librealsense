# What's different: David-Martel fork vs upstream, and the camera/firmware confound (2026-06-03)

Answers two pointed questions: (1) how does the local/David-Martel librealsense differ from the
upstream "buggy" library, and (2) is there a **firmware/camera difference** between the current and
the death-era cameras? Short answer to (2): **YES, and it is the biggest unaccounted confound in the
entire investigation.**

## 1. The current camera is a DIFFERENT unit with NEWER firmware than the one that died

| | Death-era (incidents #1/#2/#3) | Current (all 2026-06-03 soaks) |
|---|---|---|
| SDK / ASIC serial | `327122076391` | **`347622075921`** |
| USB descriptor / FW-update id | `404543020690` | **`344223022564`** |
| **Firmware** | **5.13.0.55** | **5.15.1.55** |
| USB type / port | (USB-2/dock-contended era) | USB-3.2, `2-1-2` (clean bus) |

These are **different physical D435 units** (serials differ; a firmware update does not change the
serial) running **different firmware**. The 87-roadmap §0.5 even warned a "different physical unit; a
human must confirm which camera is in the rack" — that warning was correct and went unheeded.

### Why this matters — it recontextualizes every recent "survival" result
The recent finding "dual/quad/churn/soak survive on the clean USB-3 bus with **zero `-110`**" was
attributed (carefully, as preliminary) to topology + mitigations. **It is now confounded by a third,
larger camera-side variable: a different unit on newer firmware.** The most parsimonious model that
fits ALL the data:
- The GB10 xHCI **Stop-Endpoint-after-`-110` defect is real** (incident #3 proved it on the in-kernel
  V4L2 path — that stands and the NVIDIA escalation is unaffected).
- The **trigger** — the `-110` control-transfer storm — was produced by the **death-era camera's
  firmware 5.13.0.55** (an old D400 firmware with known UVC control errata), aggravated by the USB-2/
  dock topology.
- The **current camera's firmware 5.15.1.55 produces zero `-110`** on the clean USB-3 link, so the
  Stop-Endpoint is never issued in the error state, so the controller never dies.

**Consequence:** the recent soaks do **NOT** demonstrate the death-era camera/firmware is now safe.
They demonstrate that *this* unit on *this* firmware on *this* topology does not produce the trigger.
The conservative single-stream envelope for the OLD firmware/units stays; and **a firmware update to
≥5.15 (the device-firmware TODO) is now a leading candidate for the actual trigger fix**, not just a
nice-to-have. The honest stability claim: "no `-110` observed with FW 5.15.1.55 on a clean USB-3 bus."

**To actually attribute cause** (future HIL, hardware-gated): run the same non-headless soak on the
**death-era unit/firmware (5.13.0.55)** if it can be located, on the clean bus — if it produces `-110`
where 5.15.1.55 does not, firmware is confirmed as the trigger. Until then, camera/firmware is an
**uncontrolled variable** in all post-reboot results.

## 2. David-Martel fork vs upstream `realsenseai/librealsense`

`origin/master` is **22 commits ahead of `upstream/master`, 0 behind** — a clean superset of upstream
master. The delta is **~99% additive: 8407 insertions / only 32 deletions across 78 files.** It does
**not** fix upstream bugs in the core paths; it **adds** GB10-specific, opt-in tooling:

- **Opt-in mitigations, gated by `RS2_GB10_USB_TUNING`** (P2/P3/P4/P7): `src/usb-tuning.h` (new),
  `src/libusb/device-libusb.cpp` (+126), `src/uvc/uvc-device.cpp`, `src/uvc/uvc-streamer.{cpp,h}`.
  **Undefined ⇒ byte-identical to upstream** (verified: the `#else` paths compile identically; unit +
  standalone tests prove the policy is a no-op when the define is off).
- **New tooling (additive):** `tools/gb10-profiler/` (1013 lines), `tools/dds` adapter tweaks,
  `scripts/realsense-gb10-*`, `scripts/rs-gb10-test-usb-tuning.sh`, `unit-tests/usb-tuning/`, the
  `scripts/gb10/` HIL suite, `justfile`, `docs/gb10/`.
- **Build/wrapper CMake tweaks** (CUDA OpenCV wiring, openni2/pcl/opencv wrapper CMakeLists).
- One 4-line change in vendored `rsutils/concurrency.h`.

**Key conclusion:** the local library is **not meaningfully "less buggy" than upstream in its core
behavior** — built without the GB10 define it *is* upstream. The recent stability change is therefore
**not explained by the library customizations** (they're opt-in/gated) — it is explained by the
**camera/firmware/topology** change above. The fork's value is trigger-reduction + tooling + the
forensic record, not a core-path bug fix.

## 3. Build reproducibility & idempotency — current status + gaps
- **Good:** no `__DATE__`/`__TIME__` and no git-hash embedding found in `src/`/`common/` → no
  per-build timestamp nondeterminism from those.
- **Gap (portability/reproducibility):** the GB10 profile uses **`-mcpu=native`** (`build-dgx-spark-gb10.sh`,
  justfile). Binaries are then **host-CPU-specific** — reproducible on the same GB10, but not bit-identical
  on another ARM CPU, and `-mcpu=native` on GCC 13.3 silently degrades to an `armv8-a` baseline (88-roadmap).
  **Fix:** pin an explicit arch (`-mcpu=cortex-x925`/`-march=armv9.2-a`) for reproducible, optimal binaries.
- **Gap (idempotency):** `build-dgx-spark-gb10.sh` does not clean the build dir or pass `cmake --fresh`;
  re-runs are **incremental** (stable, but can carry stale CMake cache state). Add a `--clean` mode.
- **Gap (Python ABI — the recurring trap):** the pyrealsense2 fix that forces the uv venv 3.12 for the
  new `FindPython` (`-DPython_EXECUTABLE=… -DPython_ROOT_DIR=… -DPython_FIND_VIRTUALENV=ONLY`) is **only
  in ad-hoc build commands / the `just build-hil` recipe, NOT baked into `build-dgx-spark-gb10.sh`.** A
  plain build defaults `PYTHON_EXECUTABLE` to bare `python3` (= ABI-broken 3.15.0b1) and silently builds
  a pyrealsense2 that won't import in the 3.12 venv. **Fix:** bake the venv-pinning into the build script.

## 4. Python integration robustness
- pyrealsense2 is cpython-ABI-locked to **3.12.3** (uv venv); bare `python3` is 3.15.0b1 and cannot load
  it (`undefined symbol _PyThreadState_UncheckedGet`). numpy pinned **1.26.4** (cv2 ABI).
- vigil-spark loads pyrealsense2 via a **shim** (`qobi/pyrealsense2/__init__.py` imports a copied-in
  `.so`) and pins **2.55.1** while the GB10 SDK is **2.58.1** — realignment needed (drop the 2.58 `.so`
  into the shim). cupy-cuda12x on CUDA-13 is a latent ABI flag.

## 5. Still uncertain / not robustly validated (needs non-headless HIL)
- **Firmware attribution** (above): OLD-firmware-camera soak to confirm 5.13.0.55 is the `-110` trigger.
- **`rs.align` CUDA-vs-CPU** on-device (the op vigil uses every frame; has a CUDA path; untested —
  pointcloud CUDA measured *slower*, colorize has no CUDA path).
- **V4L2 backend** long-soak (all recent soaks were RSUSB).
- **Build reproducibility**: two-build hash compare; explicit-arch pinning; clean-build mode.
- **Python build robustness**: bake the venv pin into the build script so a fresh build can't ABI-trap.
- **NVENC cq/preset sweep** by XPSNR (medical); vigil `realsensenode.py` robustness (restart=churn).
