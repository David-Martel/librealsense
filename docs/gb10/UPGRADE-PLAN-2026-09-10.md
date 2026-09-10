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
-mtune=neoverse-v2`, NEON, OpenMP, sccache, DDS on, IPO off.

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
| D435 serial | `404543020690` | `344223022564` | `346522072418` |
| Link speed | **5000 Mbps**, but **behind a hub** (`2-1.1`) | **5000 Mbps, native root port `6-1`** | USB 2.1 (PR #12) |
| Camera state | **HELD** by PID 1139440 in live `vigil_c2` | **idle** (`Driver=[none]`) | — |
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

### P0 — verification and the measurements the plan turns on
- [ ] **A6 · Build `d976b8a08` on spark-3066** into a **new** isolated prefix
      (`LRS_GB10_PREFIX=...-v2.58.4-...-canary`; the script's default still says `v2.58.3` and would
      collide with the PR #12 canary). Do **not** repoint `/usr/local/lib/librealsense2.so*`.
      *Gate:* this is the **first compile of every `.cu` file and of `rs-gb10-profiler`** against the
      2.58.4 API (§3.1) — it is verification, not deployment. Targets built, matching the PR #12
      canary receipt convention.
- [ ] **A2 · A/B zero-copy as a 2×2: {RSUSB, V4L2} × {`BUILD_WITH_CUDA_ZEROCOPY` ON, OFF}** (§5.1).
      *Gate:* pointcloud 848×480 and align depth→color, p50/p95 over ≥3 runs, against the
      `benchmarks.md` baselines (pointcloud CUDA 0.57× NEON; align 15–19× NEON), plus output
      byte-identity. Report concurrent host load — spark-3066 carries vLLM and CI, so numbers are
      noisy and must be stated as such. Adopt only on a measured win.
- [ ] **A1 · Run the R6 guarded ramp on spark-3066.** **Blocked on a coordination window (§6)** —
      this is the multistream provocation, the only item that can wedge the controller.
      *Gate:* ramp result recorded with kernel/driver/BIOS stamped; envelope decision written into
      `realsense.TODO.md` either way.

### P1 — build correctness the merge introduced
- [ ] **A3 · A/B `-ffp-contract=off`.** Upstream `08b6d0031` added it to `CMake/unix_config.cmake`,
      noting FMA fusion is "always on aarch64". It **does apply** to the GB10 build: the script
      passes `CMAKE_{C,CXX}_FLAGS_RELEASE`, which GCC receives *after* the base flags, and never
      re-enables contraction. Upstream traded aarch64 filter throughput for cross-toolchain
      bit-identity; that cost is unmeasured on GB10.
      *Gate:* filter/convert p50 with and without. If the cost is real, add an opt-in
      `LRS_GB10_FP_CONTRACT=fast` and **document the 1-LSB output delta**. Note the CUDA side is
      unaffected (`nvcc -fmad=true` is untouched), so enabling contraction on CPU actually *narrows*
      CPU↔GPU divergence.
- [ ] **A4 · Test whether `4bbc18032` (`--exclude-libs` symbol hiding) closes the open RealDDS
      duplicate static/shared symbol item** still listed in `realsense.TODO.md`.
      *Gate:* `rs-dds-adapter` shuts down cleanly, or the item is re-scoped with evidence.
- [ ] **A5 · Correct the stale environment facts** in `realsense.TODO.md`: CUDA is **13.2**, not 13.0;
      `/usr/local/cuda-13.2` **exists** (the June note calls it nonexistent); kernel/driver per §4.

### P2 — deployment
- [ ] **A7 · Re-pin fleet consumers only after A1+A2+A6.** Codex's 05:20Z bus finding already reports
      build skew (ASUS `b22` consumers vs `3b145` on both Sparks) — re-pinning into skew makes it worse.
- [ ] **A8 · Hand `ops/build_gb10_realsense.sh` / `ops/deploy_gb10_realsense.sh` deltas to the
      vigil-spark network-consolidation owner.** Those files are in a repo with active codex lanes;
      this lane does **not** write them. Needed changes: plumb `BUILD_WITH_CUDA_ZEROCOPY`, the
      `LRS_GB10_FP_CONTRACT` opt-in, and the v2.58.4 prefix name.

### P3 — hygiene and upstreaming
- [ ] **A9 · git-guard needs a merge-commit mode** (§3 note). A vendor merge cannot pass a gate that
      lints imported third-party code. Suggest: skip `qa_gate` lint when `MERGE_HEAD` exists and the
      merge parent is a known upstream remote, keeping the secret scan mandatory.
- [ ] **A10 · Upstream the genuinely generic fixes** — the four fw-update safety PRs (#8–#11), the
      CUDA-align `{-1,-1}` sentinel, the `@CMAKE_INSTALL_LIBDIR@` pkgconfig fix, and the OpenNI2
      Linux include repair. Each is an upstream bug, not GB10-specific; carrying them forever is
      merge debt.
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
