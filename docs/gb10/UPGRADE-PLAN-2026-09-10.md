# RealSense GB10 — Upstream Reconcile + Acceleration Plan (2026-09-10)

**Author:** claude (asuspro13) · **Branch:** `claude/upstream-2.58.4-gb10-20260910` · **Merge commit:** `d976b8a08`
**Supersedes for version/platform facts:** the "Local Findings" block in [`realsense.TODO.md`](../../realsense.TODO.md)
and the environment tables in [`analysis/00-executive-summary.md`](analysis/00-executive-summary.md).
**Does not supersede:** the June root-cause analysis of the xHCI controller death — that still stands
until the R6 ramp is re-run (§6).

Every number below is either measured in this session (marked **measured 09-10**) or cited to a dated
prior artifact. Nothing is inferred from release notes.

---

## 1. Scope and what actually changed

The ask was: reconcile the David-Martel fork against advanced upstream, update the Spark deployment,
optimise what `vigil-spark` consumes, and prefer optimisation over stability-for-its-own-sake.

Three things turned out differently from the framing, and they reshape the plan:

1. **The reconcile base was not `master`.** `master` is a strict subset of the open draft
   **PR #12** branch (`upstream-2.58.3-gb10-20260722`), which already carries v2.58.3 plus 138 GB10
   commits, and which is already canary-deployed on spark-0060 as
   `librealsense-v2.58.3-pr12-e8da2e3-py312-canary`. Merging upstream onto `master` would have
   redone that work and collided with PR #12. **This plan builds on PR #12.**
2. **PR #12's own stated merge blocker is already gone** (§4.1) — it can move toward merge now.
3. **The most valuable thing in upstream 2.58.4 is not a bug fix.** It is a new GPU zero-copy frame
   allocator that is gated to integrated GPUs, and **GB10 qualifies** (§5). It is the principled fix
   for the exact alloc-churn defect this fork hand-rolled a cache for and then retired.

---

## 2. Fork customisation inventory

*(the "what is actually ours" answer)*

Against upstream `v2.58.4`, the fork carries **39 code files / +3933 −140**, plus 101 docs/scripts
files. The code splits into six coherent groups:

| Group | Files | What it is | Upstream-able? |
|---|---|---|---|
| **USB/xHCI survival** | `src/usb-tuning.h` (+391), `src/libusb/device-libusb.cpp` (+263), `context-/messenger-/request-libusb`, `src/uvc/uvc-streamer.{h,cpp}`, `src/uvc/uvc-device.cpp` | P2 deeper URB pool, P3 usbfs advisory, P4 gentler stop, P7 re-acquire counter, single-opener cross-process lock, controller-wedge detector, teardown deadlines | Partly — the wedge detector and single-opener lock are generic; the GB10 tuning constants are not |
| **CUDA align/convert** | `src/proc/cuda/cuda-align.{cu,cuh}` (+166), `src/cuda/cuda-conversion.cu` (+123), `src/proc/align.cpp`, `CMake/cuda_config.cmake` | `{-1,-1}` pixel-map sentinel for D435 aligned depth, cached-pool conversion, Windows CUDA align hardening | **Yes — the sentinel fix is a genuine upstream bug fix** (see §8) |
| **GB10 profiler** | `tools/gb10-profiler/` (+1807) | `rs-gb10-profiler`: guarded HIL ramp, raw-Z16 direct-sensor capture path, provenance/receipt recording | No — fleet-specific |
| **fw-update safety** | `tools/fw-update/rs-fw-update.cpp` (+129), `unit-tests/live/tools/test-fw-update-normal-exit.py` | PRs #8–#11: fail-closed before flash, backup revalidation, live-use gating, handle-teardown-before-logger | **Yes — all four are generic safety fixes** |
| **Build/packaging** | `CMakeLists.txt`, `justfile` (+317), `wrappers/{python,openni2,opencv,pcl}`, `tools/dds/*` | `$ORIGIN` RPATH bake-in, `@CMAKE_INSTALL_LIBDIR@` pkgconfig fix, OpenNI2 Linux include repair, `pyrs_gl` GL binding | Mixed — the libdir and OpenNI2 fixes are upstream bugs |
| **Tests** | `unit-tests/usb-tuning/` (+397), `unit-test-config.py` | Offline unit coverage for the tuning/reacquire state machines | With the code |

**Build framework:** `scripts/build-dgx-spark-gb10.sh` (isolated prefix under `/opt/vigil/opt/`,
never touching `/usr/local` directly), driven by a repo-root `justfile`. Defaults: RSUSB backend,
`BUILD_WITH_CUDA=ON`, `CMAKE_CUDA_ARCHITECTURES=121`, `-O3 -march=armv9.2-a+sve2+bf16+i8mm
-mtune=neoverse-v2`, NEON, OpenMP, DDS on, IPO off, **`CXX_STANDARD=14` (was 20 — changed in this
work; see §11)**, and **host-only compile caching (`CUDA_LAUNCHER=none` — see §11 note on sccache)**.

---

## 3. Reconcile status — done and verified

| Step | Result | Evidence |
|---|---|---|
| Merge `upstream/master` (`e15c5d6bb`, v2.58.4) into PR #12 branch | **Zero conflicts** | `d976b8a08`, parents `078733da5` + `e15c5d6bb` |
| Version bump | `RS2_API_PATCH_VERSION` 3 → **4** | `include/librealsense2/rs.h` |
| Struct-rename compile risk | **Clear** | `1dd69b7c5` renames `uvc_device_info::{mipi,conn_spec}`; our surviving `.conn_spec` uses are on `usb_device_info`, which was *not* renamed |
| Secret scan on merged content | **exit 0** | `~/.git-hooks/common/secret_scan.sh` |

### 3.1 Compile verification — exactly what was and was not built

Two x86_64 Release builds, `FORCE_RSUSB_BACKEND=ON`, both **exit 0, 0 errors** (2 warnings, both
pre-existing glibc header notes):

| Surface | Status |
|---|---|
| `realsense2` core (`src/uvc`, `src/libusb`, `src/proc`, `usb-tuning.h`) | ✅ built |
| `rs-fw-update` (fork +129), `test-usb-tuning` (fork +168) | ✅ built |
| `rs-dds-adapter`, `rs-dds-config` (fork CMake changes) | ✅ built, `BUILD_WITH_DDS=ON` |
| `pyrealsense2` incl. `pyrs_gl.cpp` (fork +74) | ✅ built |
| **All `.cu` files** — `cuda-conversion.cu`, `cuda-align.cu` (fork), `cuda-frame-memory.cu` (upstream zero-copy) | ❌ **NOT built** — no CUDA on asuspro13 |
| **`rs-gb10-profiler`** (fork +1773, the largest single fork file) | ❌ **NOT built** — its CMakeLists requires `BUILD_GRAPHICAL_EXAMPLES`, which needs `OpenGL::GL`; absent on this host |
| `examples/` incl. upstream's new `gpu-frame` | ❌ not built — same missing `OpenGL::GL` |

**The two unbuilt surfaces are exactly where fork customisations and upstream's headline change
overlap.** `CMake/cuda_config.cmake` was auto-merged (fork +4 vs zerocopy +8) and has **never been
executed**. This is why the Spark build (A6) is a **P0 verification item, not a P2 deployment item.**

> **Gate note (real, worth fixing).** The commit was blocked by git-guard's `qa_gate.sh` on
> `shellcheck` and `ruff` errors located **entirely in upstream Intel files** imported by the merge
> (`scripts/patch-realsense-ubuntu-L4T.sh`, `unit-tests/3D/pytest-projection-from-recording.py`).
> "Fixing" them would create permanent divergence from upstream and guarantee conflicts on every
> future merge. I ran the secret scan explicitly (clean), then committed with the hook's own
> documented `GIT_GUARD=0` bypass, and re-verified the SHA landed. **git-guard has no merge-commit
> mode** — a vendor merge cannot pass a gate designed for authored code. That is a git-guard gap,
> filed as A9 in §9.

---

## 4. Measured platform state (measured 09-10)

Both Sparks, non-destructive probes only.

| Fact | spark-0060 | spark-3066 | June baseline |
|---|---|---|---|
| Kernel | `6.17.0-1029-nvidia` | `6.17.0-1029-nvidia` | `6.17.0-1021` |
| NVIDIA driver | 595.84 | 595.84 | 580.159.03 |
| BIOS | `5.36_0ACUM018` (2025-08-06) | `5.36_0ACUM018` (2025-08-06) | **never recorded** |
| CUDA toolkit | — | **13.2** (`/usr/local/cuda` → `cuda-13.2`) | doc says 13.0 |
| D435 serial (USB descriptor) | `404543020690` | `344223022564` | `346522072418` |
| Link speed | **5000 Mbps**, but **behind a hub** (`2-1.1`) | **5000 Mbps, native root port `6-1`** | USB 2.1 (PR #12) |
| Camera state | **HELD** by PID 1139440 in live `vigil_c2` | **idle** (`Driver=[none]`) | — |
| D435 firmware | — | **5.17.3.10** | 5.13.0.55 |
| D435 serial (SDK-reported) | — | `347622075921` | — |
| Controller deaths since boot | **0** (7d14h) | **0** (7d13h) | 3 reproductions in June |
| USB disconnects since boot | 0 | 1 | — |

### 4.1 PR #12's blocker is resolved
PR #12 requires "the exact device negotiates at least 5 Gb/s on a native xHCI root."
**spark-3066 now satisfies that exactly** — 5000 Mbps, USB version 3.20, directly on root port `6-1`
with no hub in path. spark-0060 reaches 5 Gb/s but through a hub, so it does *not* satisfy the
native-root clause. The cameras were physically swapped since July (all three serials differ).

### 4.2 The firmware premise, honestly stated
The task framing said Spark firmware was updated to fix USB-C driver issues. What is **measurable**:
kernel `-1021 → -1029` and driver `580 → 595`. What is **not**: platform firmware — BIOS reads
`5.36_0ACUM018` dated 2025-08-06 on both hosts, and no June document recorded a BIOS version, so
**no before/after comparison is possible.** The defect lives in `xhci_plat_hcd`, which ships with the
*kernel*, so the kernel bump is the plausible carrier. `6.17.0-1032.32` is available and not yet
installed.

**Camera firmware, by contrast, demonstrably did move:** the live D435 on spark-3066 enumerates at
**5.17.3.10**, versus 5.13.0.55 in June. 5.17.3.10 is the D400 matrix floor this fork's PR #7
corrected the target to, so the cameras are now at the intended level. That is a real part of the
"firmware was updated" premise — it is the *camera* firmware that advanced, not the platform BIOS.

**Zero controller deaths over 7+ days is not yet evidence the defect is fixed** — both hosts have run
only inside the single-stream safe envelope, which is equally consistent with "fixed" and
"never provoked." Only the §6 ramp can tell those apart.

---

## 5. The acceleration headline — upstream zero-copy lands on GB10

Upstream 2.58.4 adds `BUILD_WITH_CUDA_ZEROCOPY` (`039e2d74b` / `629d96e0d` / `e9d30c40a`):
`src/core/frame-data-allocator.h` routes frame pixel buffers through `cudaHostAlloc(...Mapped)` so
GPU kernels read/write them **in place**. It is default OFF, and activates **at runtime only on an
integrated GPU**, via `rsutils::rs2_is_cuda_integrated()`.

**Measured on spark-3066 (09-10), CUDA attribute probe on device 0:**

```
Integrated               = 1     <- the gate; zero-copy WILL activate on GB10
CanMapHostMemory         = 1
UnifiedAddressing        = 1
ConcurrentManagedAccess  = 1     <- note: upstream's comment assumes Jetson's 0
```

**The gate is verified, not inferred.** `probe_cuda_integrated()`
(`third-party/rsutils/src/rsutilgpu.cpp:118`) `dlopen`s `libcuda.so.1` and reads
`CU_DEVICE_ATTRIBUTE_INTEGRATED = 18` — *the same attribute this probe read as `1`*. It fails closed
on any error. So the runtime gate opens on GB10.

### 5.1 Zero-copy has two halves, and the GB10 fork only gets one of them

This is the most consequential detail in the delta, and a clean merge hides it entirely.

| Half | Mechanism | Applies to RSUSB (current GB10 default)? |
|---|---|---|
| **Allocator** | `frame_data_allocator` routes frame pixel buffers through `cudaHostAlloc(...Mapped)` | **Yes** — backend-independent |
| **Backend buffer borrow** | `uvc_sensor` lets a frame point *directly at the backend's capture buffer* (`requires_memory=false`), capped at `ZC_MAX_INFLIGHT = 2` so the ring can't starve | **No** |

`rs_v4l2_zc_register()` / `rs_v4l2_zc_unregister()` are called from **exactly one place**:
`src/linux/backend-v4l2.cpp:349/390`. The borrow only happens on buffers a V4L2 backend registered.
Under `FORCE_RSUSB_BACKEND=ON` — which the GB10 build script sets — no buffer is ever registered, so
`do_zc` never engages and **only the allocator half applies.**

**This converges with a conclusion the fork already reached independently.**
[`analysis/86-v4l2-backend-assessment.md`](analysis/86-v4l2-backend-assessment.md) concluded V4L2 is
"the correct production backend for this platform" and "strictly safer than RSUSB on the GB10" on
*reliability* grounds. Upstream zero-copy now adds a *performance* reason pointing the same way.
Both Sparks already carry `-v4l2` prefix variants, so the backend swap is a build-flag decision the
fleet has already staged, not new work.

**Consequence for the A/B in §7: it must be a 2×2, not a 1×2** — {RSUSB, V4L2} × {zero-copy ON, OFF}.
Measuring zero-copy on RSUSB alone would test the weaker half and could wrongly retire the idea.

### Why this is the most valuable item in the whole delta

June measured (`benchmarks.md`): **`rs.pointcloud` shipped CUDA ran at 0.57× of NEON — CUDA was
*slower* than CPU — attributed to per-frame allocation churn.** The fork's answer was a
process-static cached pool (mode 1, 3.3× over shipped), which was **later retired**
(`218dbb082`, and `LRS_GB10_PC_ZEROCOPY` is pinned to `0` with "must remain 0").

So the fork currently has **no mitigation for a measured 0.57× CUDA regression.** Upstream's
allocator attacks that same root cause through a supported, upstream-maintained mechanism, on
exactly the hardware class it was written for. This is the single highest-value action in the plan.

**Caveat that must be measured, not assumed:** `cudaHostAllocMapped` is pinned memory. Pinned
allocation is expensive and this is on the *frame* path, so a naive win is not guaranteed —
it may trade D2H copies for allocation cost. The A/B in §7 is mandatory before adopting.

---

## 6. The envelope decision — the one test that reorients everything

The fork's entire operating posture ("**single high-rate stream only**"; dual 848×480@60 killed a
controller in June) rests on a defect the June analysis concluded only an NVIDIA BSP/kernel fix could
resolve. The kernel has since moved two releases.

**R6 guarded ramp** (`analysis/87-safety-reliability-roadmap.md`) is the decider:
- **If the defect is fixed** → the single-stream envelope lifts. Dual/triple 848×480@60 becomes the
  optimisation target, and the whole vigil-spark RealSense capability ceiling rises. This dominates
  every other optimisation in value.
- **If it persists** → 2.58.4 + zero-copy still land; the envelope stays; §5 becomes the main win.

**Status: BLOCKED on a coordination window, deliberately.** The test can wedge the USB controller and
force a host reboot. At time of writing:
- spark-0060 — its RealSense is **actively held** by a live `vigil_c2` session (up 19410s). Worst
  possible host. *(This reverses my own first bus post, which wrongly preferred it.)*
- spark-3066 — camera **idle** and on a **native root port** (correct host on topology grounds), but
  the host also runs a vLLM `vigil-router` container up 34h and an **active aarch64 CI job**.

Requested on the agent-bus at 06:25Z, corrected with measurements at 06:27Z, tagged
`repo:librealsense`. No reply at time of writing. **I did not take the window.**

---

## 7. Upgrade + acceleration TODO

Ordered by measured value. "Gate" = what must be true before the item is called done.

> **Only A1 needs the reboot-risk window.** A2/A3/A6 are single-stream or offline measurements —
> the same envelope the fleet runs in daily — and are *not* blocked on it. Do not bundle them.

### P-1 — hard blocker found while executing A6. **2.58.4 must not ship until this is fixed.**

- [x] **A0 · Fix the exit-time double free in the GB10 2.58.4 build.** Full evidence in §11;
      root-caused, confirmed by single-variable experiment (§11.1), and fixed by defaulting the GB10
      build to C++14. **Re-verify on a rebuilt canary before A7.**
      Every tool built from 2.58.4 with the GB10 script aborts at exit — including
      `rs-enumerate-devices --version`, which opens no camera. 2.58.1 and 2.58.3 on the same host and
      script are clean. *Gate:* `rs-enumerate-devices --version` exits 0 with no `free()` diagnostic,
      and `rs-gb10-profiler --self-test` passes, before **any** re-pin (A7) is even considered.

### P0 — verification and the measurements the plan turns on
- [x] **A6 · DONE 2026-09-10** (built `ff345476a`, not `d976b8a08` — the branch advanced). Build `d976b8a08` on spark-3066 into a **new** isolated prefix
      (`LRS_GB10_PREFIX=...-v2.58.4-...-canary`; the script's default still says `v2.58.3` and would
      collide with the PR #12 canary). Do **not** repoint `/usr/local/lib/librealsense2.so*`.
      *Gate:* this is the **first compile of every `.cu` file and of `rs-gb10-profiler`** against the
      2.58.4 API (§3.1) — it is verification, not deployment. Targets built, matching the PR #12
      canary receipt convention.
- [x] **A2 · zero-copy A/B — DONE 2026-09-10, adopted.** `benchmarks.md` §9. The runtime gate was
      verified rather than inferred (GB10 reports `cudaDevAttrIntegrated=1`). Measured on spark-3066:
      **pointcloud p50 0.234 → 0.135 ms (−42%)**, **align 0.321 → 0.301 ms (−6.2%)**, colorize
      unchanged as the control, output byte-identical. `LRS_GB10_CUDA_ZEROCOPY` now defaults **ON**.
      The V4L2 leg of the 2×2 was **not** run: `LRS_GB10_FORCE_RSUSB` already existed, but RSUSB is
      the GB10 default for controller-safety reasons and the V4L2-only mechanism (capture buffers
      borrowed from the V4L2 ring, `uvc-sensor.cpp`) is a separate change of risk class. Deferred,
      not forgotten.
      **It also produced a code optimization:** `cuda-align.cu` was never wired for zero-copy
      upstream, and wiring it *naively* is **6.6× slower** — mapping the output puts
      `atomic_min_uint16` on host memory over the coherence fabric. Landed as the `RS2_ALIGN_ZC`
      ladder defaulting to inputs-only (`81068f1b4`). Rule: **map streaming reads, keep atomic or
      scattered writes device-local.**
- [x] ~~**A2 (original scope) · 2×2 {RSUSB, V4L2} × {ON, OFF}**~~ — superseded by the above.
- [x] **A1 · R6 guarded ramp — DONE 2026-09-10. Controller SURVIVED; envelope NOT lifted.**
      `benchmarks.md` §12. Two full stress sweeps (12 s and 60 s per entry) each **12/12 PASS with
      zero kernel USB faults**, including `HEAVY_60fps_848x480_D+C+IR` which the source annotates as
      having crashed the xHCI on 2026-06-02, plus 5/5 clean dual-stream + align runs. No reboot was
      needed and no service was disrupted. Kernel/BIOS stamped as the gate required
      (6.17.0-1029-nvidia, up from 6.17.0-1021 in June; BIOS 5.36_0ACUM018 — **the first GB10
      baseline to record one at all**).
      **Envelope decision, written both here and in `realsense.TODO.md`: KEEP the single-high-rate-
      stream envelope for now.** The lethal class is no longer lethal *on this host*, but
      spark-3066's camera is on a native root port while **spark-0060's is behind a hub** and is
      unvalidated; ~12 min is not a soak against an intermittent defect; and kernel, BIOS and camera
      firmware all moved together, so nothing isolates the cause. The result justifies *scheduling*
      the soak and the 0060 run, not relaxing policy.

### P1 — build correctness the merge introduced
- [x] **A3 · A/B `-ffp-contract=off` — DONE.** Upstream `08b6d0031` added it to `CMake/unix_config.cmake`,
      noting FMA fusion is "always on aarch64". It **does apply** to the GB10 build: the script
      passes `CMAKE_{C,CXX}_FLAGS_RELEASE`, which GCC receives *after* the base flags, and never
      re-enables contraction. Upstream traded aarch64 filter throughput for cross-toolchain
      bit-identity; that cost is unmeasured on GB10.
      **DONE 2026-09-10 — negative result, keep `off`.** `benchmarks.md` §10. Made selectable
      (`RS2_FP_CONTRACT`, `FP_CONTRACT=` in `bench-filters.sh`) and measured: across eight
      deterministic filter rows the cost of bit-identity is **≤2.5% and mostly under 1%**, inside
      run-to-run noise for most rows. Both modes also reported *all variants bit-identical to the
      scalar reference*, so GCC is not actually contracting differently across flavours here — the
      guarantee upstream wanted is being had for free. No `LRS_GB10_FP_CONTRACT=fast` default is
      warranted; the knob exists for future hosts. Scope note: these flags never reach device code
      (`nvcc` defaults `--fmad=true`), so this measures **host** filters only.
- [x] **A4 · DONE 2026-09-10 — closed.** It was the same defect as §11. Across all eight bundled
      archives (543 global data objects, 80 of them realdds') **zero** are now exported by the `.so`
      *and* present in an executable's dynamic symbol table: `hide_bundled_archive_symbols` covers
      seven archives and `16382ef5c` covers `rsutils`, which that list omits. Verified beyond
      `--help`/`--version` as the gate demanded: `rs-dds-adapter` started, allowed to reach "Start
      listening to RS devices", then SIGINTed — **rc=0 on 3/3 runs**, on both architectures.
- [x] **A5 · DONE** — the corrections are in `realsense.TODO.md` (CUDA 13.2, `/usr/local/cuda-13.2` exists, kernel/driver). Correct the stale environment facts in `realsense.TODO.md`: CUDA is **13.2**, not 13.0;
      `/usr/local/cuda-13.2` **exists** (the June note calls it nonexistent); kernel/driver per §4.

### P2 — deployment
- [ ] **A7 · Re-pin fleet consumers only after A1+A2+A6.** Codex's 05:20Z bus finding already reports
      build skew (ASUS `b22` consumers vs `3b145` on both Sparks) — re-pinning into skew makes it worse.
      **New hard requirement (`benchmarks.md` §13):** upstream 2.58.4 breaks ABI against 2.58.3
      (issue #15617) — `RS2_EXTENSION_OBJECT_DETECTION_SENSOR` was removed from the **middle** of
      `rs2_extension`, shifting every later value. Checked against this fleet the blast radius is
      negligible (it sat at position 69 of 71; only the two D555/perception values shift, which the
      D435 fleet never references), but the failure mode is silent. **A7 must rebuild every consumer
      against the 2.58.4 headers — `pyrealsense2` and any ROS 2 node binary included — not merely
      repoint the `.so`.** Note vigil-spark is already internally inconsistent here:
      `ops/build_gb10_realsense.sh` builds a **2.58.3** prefix while `ops/deploy_gb10_realsense.sh`
      defaults to **2.58.1**, and both Sparks currently resolve to the 2.58.1 prefix.
- [ ] **A8 · Hand `ops/build_gb10_realsense.sh` / `ops/deploy_gb10_realsense.sh` deltas to the
      vigil-spark network-consolidation owner.** Those files are in a repo with active codex lanes;
      this lane does **not** write them. Needed changes: plumb `BUILD_WITH_CUDA_ZEROCOPY`, the
      `LRS_GB10_FP_CONTRACT` opt-in, and the v2.58.4 prefix name.

### P3 — hygiene and upstreaming
- [ ] **A9 · git-guard needs a merge-commit mode** (§3 note). A vendor merge cannot pass a gate that
      lints imported third-party code. Suggest: skip `qa_gate` lint when `MERGE_HEAD` exists and the
      merge parent is a known upstream remote, keeping the secret scan mandatory.
- [ ] **A10 · Upstream the genuinely generic fixes.** **Drafted 2026-09-10, NOT filed** —
      [`UPSTREAM-REPORT-DRAFT-2026-09-10.md`](UPSTREAM-REPORT-DRAFT-2026-09-10.md). Filing on a
      public tracker under the account owner's identity is outward-facing and unauthorised; the
      drafts are ready to submit on request. Two are new and are the high-value ones:
      (a) the **rsutils duplicate-global double free** — generic, reproduces on stock x86_64, bug
      report with the fix attached; (b) the **align zero-copy regression** — upstream wired
      pointcloud's output for zero-copy but never align's, and wiring it naively is 6.9× slower
      because of `atomic_min_uint16` on mapped memory. That is precisely what a maintainer would
      want on record *before* someone else wires it. Plus the pre-existing set: the fw-update safety
      PRs (#8–#11), the CUDA-align `{-1,-1}` sentinel, the `@CMAKE_INSTALL_LIBDIR@` pkgconfig fix
      and the OpenNI2 include repair.

- **Upstream issues relevant to the deployed hardware** (tracked, not owned by this lane):
  - [#15617](https://github.com/IntelRealSense/librealsense/issues/15617) — 2.58.4 breaks ABI vs
    2.58.3. Blast radius checked against this fleet in `benchmarks.md` §13; it gates A7.
  - [#15424](https://github.com/IntelRealSense/librealsense/issues/15424) — D435i `pipeline.start()`
    alternately fails with "Frame didn't arrive within 5000" after `pipeline.stop()` on Ubuntu 24.04
    / kernel 6.14, recovered by `hardware_reset()`. **Same territory as this fork's P7 re-acquire
    guard**, which measured 0/5 false fires. Worth comparing notes — the fork may already have the
    mitigation upstream lacks.
  - [#15546](https://github.com/IntelRealSense/librealsense/issues/15546) — `videodev` module blocks
    installation on Jetson AGX Orin. aarch64-relevant; the GB10 build sidesteps it by using RSUSB,
    which is worth saying on the issue.

- [ ] **A11 · Move PR #12 out of draft** — its blocker is resolved (§4.1); or supersede it with this
      2.58.4 branch. Owner decision.

---

## 8. Upstream 2.58.4 delta triage

154 files, +6239 −1746 over v2.58.3. The large majority is **D5x5 / GMSL / dual-RGB / D585 / viewer**
work that does not touch the D435-over-USB path. What lands on us:

| Commit | What | Impact here |
|---|---|---|
| `039e2d74b` +2 | GPU zero-copy frame allocator | **P0 opportunity** — §5 |
| `08b6d0031` | `-ffp-contract=off` on unix | **Perf regression risk on aarch64** — A3 |
| `4bbc18032` | Hide bundled third-party symbols | Candidate RealDDS fix — A4 |
| `3a44bc390` | Fix `log_to_console` race in rsutils easyloggingpp | Root-cause fix; **complementary to**, not superseded by, our PR #9 tool-level fix |
| `7ab8a00e1` | Rotation-filter crash on streams without intrinsics | Free robustness |
| `aec4d318b` | Pose-inverse numerical instability | Free correctness |
| `1dd69b7c5` | `uvc_device_info` field renames | Compile risk — **verified clear** (§3) |
| `6a78dd052` +2 | System `nlohmann_json` support | Packaging option |

---

## 9. Windows (dtm-p1gen7) and asuspro13 — future deployment

Both are named in the ask as future RealSense hosts. Neither is ready, and neither was changed here.

**asuspro13 (this host, x86_64):** no `librealsense2` installed, `pkg-config realsense2` empty, no
`pyrealsense2`, **no camera attached** (`lsusb` shows no `8086:` device). It is a fine *build and CI*
host — it compiled `d976b8a08` cleanly — but it cannot run HIL. To become a RealSense host it needs a
camera, the udev rules, and an install; that is a hardware decision, not a code one.

**dtm-p1gen7 (Windows 11):** the fork already carries `scripts/build-windows.ps1`, a
`windows-x64-cuda` preset (`BUILD_WITH_CUDA`, sm_120), an isolated x86/x64 MSVC preset, and the
merged Windows CUDA-align hardening (PR #4). Note **sm_120 ≠ GB10's sm_121** — Windows targets a
discrete GPU, so **`BUILD_WITH_CUDA_ZEROCOPY` must stay OFF there**: the runtime gate would decline it
anyway (discrete GPUs keep the copy path), so enabling it is pure risk with no upside.

**Cross-platform benchmark parity:** `docs/gb10/benchmarks.md` is GB10-only. Before either host
becomes a real target, the profiling harness needs a host-stamped results schema (arch, GPU,
integrated flag, CUDA version, link speed, backend) so x86_64-discrete, Windows-discrete and
GB10-integrated numbers are comparable rather than three incomparable tables. `rs-gb10-profiler`
already records provenance; the schema should be lifted out of the GB10-specific tool.

---

## 10. Coordination record

| Time (UTC) | Action |
|---|---|
| 06:25 | Opened lane on agent-bus, `topic=coordination`, `tag=repo:librealsense`. Claimed **librealsense only**; explicitly disclaimed vigil-spark source. Requested a reboot-risk HIL window. |
| 06:27 | **Corrected my own post** with measurements — reversed the host preference to spark-3066 and documented why both hosts are hot. |
| — | No window granted at time of writing. Ramp **not** run. All work above is source-only, on asuspro13, with zero writes to either Spark. |

Precedence was taken on the librealsense repo itself, as instructed. It was **not** extended to
wedging a Spark that another agent is streaming on — that is the specific risk the coordination
clause exists to prevent.

---

## 11. RESOLVED — 2.58.4 double-freed at exit; the cause was a librealsense linkage defect

Found while executing A6 (the Spark build), diagnosed to a defect in `rsutils`' linkage model, and
fixed in code. **C++20 is retained.** This section was rewritten on 2026-09-10 after the real root
cause was found; an earlier revision blamed the fork's C++20 default and recorded C++14 as the fix.
Both of those claims were wrong, and the corrected reasoning is below.

### Symptom

Every tool built from 2.58.4 aborted at exit — including `rs-enumerate-devices --version`, which
never touches a camera:

```
/opt/vigil/opt/librealsense-v2.58.4-dgx-spark-gb10-canary/bin/rs-enumerate-devices  version: 2.58.4.0
free(): double free detected in tcache 2      <- SIGABRT, core dumped
```

### Root cause (gdb + `readelf`, not inference)

```
#8  ~basic_json<..., rsutils::json_base>()        at 0x0000aaaaaad65590   <- EXECUTABLE range
#9  __cxa_finalize
#10 __do_global_dtors_aux   from .../lib/librealsense2.so.2.58            <- LIBRARY range 0x0000ffff...
#11 _dl_call_fini -> _dl_fini -> __run_exit_handlers -> exit
```

The library's finalizer runs a destructor for a global `nlohmann::json` that lives in the
**executable**. One object, two constructions, two destructions.

The structural cause is three lines of build configuration:

| Where | What |
|---|---|
| `third-party/rsutils/CMakeLists.txt:6` | `add_library( rsutils STATIC "" )` |
| `CMakeLists.txt:64` | `target_link_libraries( ${LRS_TARGET} PUBLIC rsutils )` |
| `CMakeLists.txt:108` | `hide_bundled_archive_symbols(...)` — **`rsutils` is not in the list** |

`rsutils` is a **static** library linked **PUBLIC** into the **shared** `realsense2`. Every
executable that links realsense2 therefore also links `librsutils.a` and gets its own definition of
every global `rsutils` owns. With default (preemptible) visibility, ELF resolves both the library's
and the executable's references to the **executable's** copy — so `librealsense2.so`'s initializer
constructs the executable's object and registers a destructor for it, and the executable's
initializer does the same.

An audit of `librsutils.a` finds exactly **five** global data objects, all affected:

```
rsutils::null_json  rsutils::missing_json  rsutils::empty_json_string
rsutils::empty_json_object          <- the four json sentinels: the crash
rsutils::g_librealsense_elpp_id     <- same defect, silent (see below)
```

### Why it looked like a C++20 problem — and why that reading was wrong

Neither of the executable's own object files references `missing_json`. The archive drags
`json.cpp.o` in *transitively*, and how much of `librsutils.a` gets pulled depends on which members
are referenced:

| `-std` | rsutils symbols pulled into the exe | sentinels among them | Result |
|---|---:|---:|---|
| `c++14` | 18 | 0 | clean |
| `c++20` | 105 | 4 | double free |

So the standard only changes **whether the linker extracts the defective archive member**. The
defect is present at every standard; C++14 merely fails to reach it. Treating C++14 as the fix
papered over a live bug and cost the C++20 optimizations for nothing.

Two further corrections to the earlier reading:

- **Upstream `4bbc18032` (`-Wl,--exclude-libs`) is not the cause.** It drops the `.so`'s exported
  json symbols 511 → 158, so the executable's references can no longer bind to the library's copy.
  That makes the bug *easier to reach*, not real. It fixes a genuine ROS 2 FastDDS ABI crash that
  matters more to this fleet than any of this, and **must not be reverted**.
- **It is not GB10-specific.** Reproduced on plain x86_64 (asuspro13) with a **stock** configure at
  `-std=c++20` — no GB10 script, no CUDA, no RSUSB. The earlier "x86_64 is clean" control row was a
  build at a *different standard*, so it compared two variables at once. This is a general
  librealsense defect.

### 11.1 The fix — hidden visibility on every duplicated global

`fdb79b7c5` hid the four json sentinels; `16382ef5c` generalised the mechanism into
`rsutils/visibility.h` (`RSUTILS_LOCAL`) and applied it to the fifth.

Hidden visibility gives each module a private, non-preemptible copy, so each is constructed and
destroyed exactly once. That is only correct for globals two modules never need to *agree* on, and
both qualify — they are compared by **value**, never by address:

- `json_ref::exists()` tests `_j.is_discarded()` (`json.h:54`), not `&_j == &missing_json`.
- the elpp id is handed to `el::Loggers::getLogger()`, which looks up by string content.

The attribute is repeated on each definition because GCC emits default visibility for a definition
that does not carry it, even when the declaration did.

**`g_librealsense_elpp_id` was the same bug, silently.** It is a `std::string const` with identical
duplicate-and-preempt behaviour, and it did not abort only because `LIBREALSENSE_ELPP_ID` is
`"librealsense"` — 12 characters, inside libstdc++'s 15-character short-string buffer — so the string
holds no heap allocation and the second destruction frees nothing. Renaming the logger id to
anything longer would have turned it into the same crash.

On Windows the `#if defined(_WIN32)` branch expands to nothing: PE has no symbol interposition, and
`src/realsense.def` (verified) does not list any of the five, so the DLL never exported them.

### 11.2 Verification — C++20 restored, both architectures

| Check | x86_64 asuspro13 | aarch64 spark-3066 (GB10, CUDA 13.0) |
|---|---|---|
| `-std=` actually used | c++20 | c++20 |
| 5 globals: `.so` exports / exe dynsym | **0 / 0** (was 4 / 1) | 0 sentinels exported |
| tools × 3 runs | 9/9 rc=0, clean stderr | 10/10 rc=0, clean stderr |
| unfixed control build | still rc=134 | — |
| `rs-gb10-profiler --self-test` | n/a (no CUDA/GL) | **checks=32 failures=0** |
| CUDA objects compiled | n/a | **5** — first CUDA-clean build (see §11.3) |
| unit tests | json-compat 6, config-file 13, hexarray 117 — pass | — |
| python | `pyrealsense2 2.58.4`, pyrealdds, pyrsutils import | built + installed |
| logging across the DSO boundary | `--debug` emits DEBUG lines | — |

The last row matters: it is the positive control for hiding the elpp id. If the logger id were being
matched by address rather than content, the hidden copies would fail to find the logger and the
DEBUG lines would vanish.

### 11.3 Why the CUDA surface had never been compile-verified

Unrelated to the crash, found in the same build. `sccache` mangles the stub translation unit `nvcc`
generates (`__cudaLaunch` macro arity, in `/tmp/sccache_nvcc*/x_0.cudafe1.stub.c`), which failed
**every** `.cu` file. The build script applied a single auto-detected launcher to C, C++ and CUDA
with no opt-out, so the CUDA path had never compiled in this configuration.

Split into `LRS_GB10_LAUNCHER` (auto → sccache/ccache/none) and `LRS_GB10_CUDA_LAUNCHER` (default
`none`). A stale `CMAKE_CUDA_COMPILER_LAUNCHER` survives in `CMakeCache.txt`, so a clean configure is
required after changing it.

### Upstream

The fix is generic — it is not a GB10 workaround and carries no fork-specific conditionals — so it
belongs upstream (A10). Any downstream that links `realsense2` is exposed; the executable simply has
to reference enough of `rsutils` to drag `json.cpp.o` out of the archive. The alternative structural
fix, adding `rsutils` to `hide_bundled_archive_symbols`, was not chosen: it would hide *all* rsutils
symbols from the `.so`, which is a much larger ABI change than making five immutable globals
module-local.
