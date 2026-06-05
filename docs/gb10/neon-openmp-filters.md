# GB10 NEON + OpenMP Feasibility Study — Depth Post-Process Filters

**Platform:** NVIDIA GB10 / DGX-Spark, ARM64 (Cortex-X925 perf + Cortex-A725
efficiency), gcc 13.3.0, OpenMP 201511 (libgomp), 20 cores (10 X925 @ 3.9 GHz,
10 A725 @ 2.8 GHz), L2 25 MiB, L3 24 MiB.
**Method:** measure-first, **no camera**. Synthetic deterministic depth
(gradient + radial + noise, 15% structured invalid/zero pixels). CPU microbench
only. Every accelerated variant is asserted **bit-identical** to the scalar
reference before timing; warmup discarded.
**Bench:** [`scripts/gb10/bench_filters_neon.cpp`](../../scripts/gb10/bench_filters_neon.cpp),
harness [`scripts/gb10/bench-filters.sh`](../../scripts/gb10/bench-filters.sh).
Builds clean under `-Wall -Wextra -Werror -O3`.

This is an **honest go/no-go**. A measured "already-SIMD / marginal / NO-GO" is a
valid result (precedent: P4 async pipelining was a measured NO-GO).

---

## 1. What actually compiles on aarch64 (source audit of `src/proc/`)

librealsense ships an SSE→NEON story only for a *narrow* set of blocks. The
`src/proc/neon/` directory contains **only** `neon-align`, `image-neon`, and
`neon-pointcloud` (align + pointcloud). `src/proc/sse/` mirrors those same two.
There is **no SSE→NEON shim header**; each accelerated block has a hand-written
NEON twin gated by `__ARM_NEON`. **None of the seven depth post-process filters
in scope has any NEON, SSE, OpenMP, TBB, or threading.** Confirmed by grep for
`sse2neon | __m128 | _mm_ | RS2_USE_CPU_EXTENSIONS | __SSSE3__ | NEON | omp |
pragma | tbb | thread` across all seven `.cpp`/`.h` — **zero hits**.

OpenMP (`BUILD_WITH_OPENMP`) is wired into `align.cpp`
(`#pragma omp parallel for schedule(dynamic)`) and pointcloud only — never the
post-process filters.

| Filter (`src/proc/`) | aarch64 today | Parallel? | Inner-loop shape | NEON-able? | OMP-able? |
|---|---|---|---|---|---|
| `threshold.cpp` | **scalar** | no | per-pixel `du*d ∈ [min,max] ? d : 0` | yes | yes (rows) |
| `disparity-transform.h` `convert<>` | **scalar** (`//TODO SSE optimize`) | no | per-pixel `isnormal ? factor/d : 0` | yes | yes (rows) |
| `temporal-filter.h` `temp_jw_smooth<>` | **scalar** | no | per-pixel IIR + history mask + persistence LUT | yes (with masked gather) | yes (rows) |
| `spatial-filter.cpp` H/V recursive | **scalar** | no | **serial recursive IIR** along scan | **no** (serial dep within line) | **yes** (lines independent) |
| `decimation-filter.cpp` (median) | **scalar** | no | median-of-N with data-dependent compaction | no (gather/compaction) | **yes** (output rows independent) |
| `hole-filling-filter.cpp` | **scalar** | no | sequential neighbor propagation | no (data-dependent fill) | no |
| `colorizer.cpp` histogram | **scalar** | no | scatter `hist[idx]++` + serial prefix sum | no (scatter conflicts + scan) | no |

### Why two filters are structural NO-GO (not benched — would be a strawman)

- **hole-filling** — each output reads neighbors that the same pass may have
  just written (fill-from-left / nearest-from-around). Inherently sequential,
  data-dependent; no lane parallelism, and the fill order makes per-row OMP
  unsafe.
- **colorizer histogram** — `hist[(int)depth]++` is a scatter with lane
  collisions (multiple pixels hit the same bin), followed by a strictly serial
  cumulative-sum `hist[i]+=hist[i-1]`. Neither vectorizes nor parallelizes
  without a conflict-detection or segmented-scan rewrite that would change
  numerics.

**decimation is NOT NO-GO** — although its median compaction (data-dependent
gather of non-zero pixels into a variable-length kernel + `opt_medK` selection
network) defeats NEON, its **output rows are independent** (each band of `scale`
input rows produces one output row), so OpenMP-across-rows is clean and
correctness-identical. It is **benched below** and is a real OMP GO (it was
wrongly hand-waved as "too cheap" in an earlier draft; measurement refuted that).

The genuine candidates are therefore **threshold, disparity, temporal** (NEON),
**spatial** and **decimation** (OpenMP, since their inner loops resist lane
vectorization but their outer loops are line/row-independent).

---

## 2. The autovec trap, and how this study avoids it

The shipping SDK builds aarch64 at **`-O3 -ftree-vectorize -mstrict-align
-ffp-contract=fast`** (no `-march`) — see `CMake/unix_config.cmake`. So the
"scalar baseline" is *whatever gcc auto-vectorizes at -O3*, not naive scalar.
Comparing hand-NEON against a `-fno-tree-vectorize` strawman would inflate every
win.

This bench reports **three** numbers per candidate and uses the **autovec-O3**
row (== shipping path) as the baseline for the verdict:

1. **true-scalar** — `-O3 -fno-tree-vectorize` (lower bound, not shipping).
2. **autovec-O3** — `-O3 -ftree-vectorize` (**== shipping flags**, except
   `-ffp-contract` forced `off` here vs `fast` shipping, for cross-variant
   bit-identity; timing impact is negligible — these loops are not FMA-bound).
3. **hand-NEON** and **NEON+OpenMP**.

**Finding:** for these branchy per-pixel loops gcc's autovectorizer yields
**negligible** benefit. true-scalar ≈ autovec within noise (threshold 848:
0.634 vs 0.648 ms; temporal 720p: 4.14 vs 4.14 ms). `objdump` of the threshold
"scalar" loop confirms it emits **single-lane `ucvtf d0` / `fmul d1,d0,d1`**, not
`.4s`/`.8h` lane ops — gcc did **not** vectorize it. So the NEON speedups below
are over the *real shipping baseline*.

**Correctness rigor:** all variants run with `-ffp-contract=off` so scalar/NEON
FMA fusion cannot diverge; disparity uses true `vdivq_f32` (IEEE divide, not
reciprocal-estimate); the spatial `*(int*)&x>0` validity test is reproduced via
a `memcpy`-based `bits_positive` (strict-aliasing-clean, bit-faithful). Temporal
& spatial mutate in place, so each timed iteration's buffer-reset cost is
measured and **subtracted** — reported numbers are **net kernel time**.

---

## 3. Measured numbers

Single-thread rows pinned to **one Cortex-X925** (`taskset -c 19`). OpenMP rows
pinned to a **homogeneous 10×X925** cpuset (`taskset -c 5-9,15-19`,
`OMP_PROC_BIND=close OMP_PLACES=threads`, 10 threads). 400 iters, 40 warmup.
Single-thread numbers are **mean**; OMP rows give **p50 and p95** (the OMP
parallel section has an OS-jitter tail — p50 is the headline, p95 shown for
honesty). decimation/spatial are OpenMP-only (no NEON column). **All cells below
come from one consistent harness run** (so they reproduce together).

### 848 × 480

| Filter | true-scalar | **autovec (ship)** | **hand-NEON** | OMP p50 | OMP p95 | NEON ×  | OMP × (p50) |
|---|---|---|---|---|---|---|---|
| threshold | 0.632 | **0.662** | **0.123** | 0.014 | 0.017 | **5.4×** | 47× |
| disparity | 0.536 | **0.547** | **0.113** | 0.013 | 0.013 | **4.8×** | 42× |
| temporal | 1.814 | **1.803** | **0.378** | 0.080 | 0.299 | **4.8×** | 23× |
| decimation | 0.841 | **0.840** | n/a (gather) | 0.035 | 0.061 | — | **24×** (scalar+OMP) |
| spatial-hv | 6.63 | **6.59** | n/a (serial) | 0.655 | 1.37 | — | **10.1×** (scalar+OMP) |

### 1280 × 720

| Filter | true-scalar | **autovec (ship)** | **hand-NEON** | OMP p50 | OMP p95 | NEON ×  | OMP × (p50) |
|---|---|---|---|---|---|---|---|
| threshold | 1.520 | **1.526** | **0.277** | 0.030 | 0.030 | **5.5×** | 51× |
| disparity | 1.307 | **1.307** | **0.253** | 0.027 | 0.028 | **5.2×** | 48× |
| temporal | 4.146 | **4.144** | **0.861** | 0.214 | 0.322 | **4.8×** | 19× |
| decimation | 2.021 | **2.017** | n/a (gather) | 0.162 | 0.508 | — | **12×** (scalar+OMP) |
| spatial-hv | 15.38 | **15.35** | n/a (serial) | 1.610 | 2.36 | — | **9.5×** (scalar+OMP) |

Throughput peaks (NEON, single X925): threshold/disparity ~3.3–3.6 Gpix/s, ~13
GB/s (threshold) / ~21 GB/s (disparity). Single-core NEON was **compute/overhead
bound, not bandwidth bound** (scalar threshold 2.6 GB/s → NEON 13 GB/s, 5×).
NEON+OpenMP shows 50–200 GB/s aggregate; note these working sets (≤5.5 MB) are
**L3-resident** (L3 = 24 MB), so this is cache bandwidth — cold-DMA frames
arriving from the camera will see less DRAM headroom. The OMP p95 tails (notably
temporal/decimation/spatial at 848×480, where each thread gets only ~10K px) are
**fork/join + OS-scheduler jitter**, not kernel cost — the p50 is the
representative figure and even the p95 stays far inside budget.

**Scope notes on the numbers:**
- `spatial-hv` = one full separable pass (horizontal rows **+** vertical
  columns). The real filter runs this **1–5× per frame** (default magnitude = 2),
  so true cost is **≈2× the table** (≈13 ms / ≈31 ms at 720p with default
  settings). The vertical pass is column-strided (worse cache) yet still
  line-independent, so OMP scales similarly. This makes the spatial OMP case
  **stronger**, not weaker.
- disparity benched **depth→disparity** only (uint16 input, where
  `isnormal == nonzero`). The reverse (float input) has real subnormal/inf
  semantics the NEON `vtst` path would not replicate bit-for-bit and is out of
  scope for this study.

---

## 4. Frame-budget reality check + per-filter verdict

Budget: **33.3 ms @ 30 fps**, **11.1 ms @ 90 fps**. Verdicts are driven by
**absolute ms vs budget on the shipping (autovec) path**, not raw speedup. Floor:
a filter that is <2% of budget or already SIMD = NO-GO.

Verified from `spatial-filter.h`: default magnitude `filter_iter_def = 2`, each
iteration runs H **and** V, so the real spatial cost is **≈2× the single-pass
bench number**. decimation default `scale = 2`.

| Filter | autovec cost @720p | % of 90 fps (11.1 ms) | % of 30 fps (33.3 ms) | Verdict |
|---|---|---|---|---|
| **spatial** (×2 iters) | ~30.8 ms | **>100%** (caps fps) | ~92% | **GO — OpenMP** (→~3.2 ms, ~9.6×) |
| **temporal** | 4.14 ms | **37%** | 12% | **GO — NEON** (→0.86 ms, 4.8×) |
| **decimation** | 2.02 ms | **18%** | 6% | **GO — OpenMP** (→0.16 ms, 12×) |
| **disparity** | 1.31 ms | 12% | 4% | **GO — NEON** (→0.25 ms, 5.2×) |
| **threshold** | 1.54 ms | 14% | 5% | **marginal-GO — NEON** (→0.28 ms, 5.5×) |
| hole-filling | small, data-dependent | — | — | **NO-GO** (sequential, not vectorizable/parallelizable) |
| colorizer histogram | small | — | — | **NO-GO** (scatter + serial scan) |

**Priority order if implemented:** spatial (OpenMP) ≫ temporal (NEON) >
decimation (OpenMP) > disparity (NEON) ≥ threshold (NEON). Spatial alone is the
single biggest win — at default magnitude it can dominate / cap the achievable
frame rate, and OpenMP across rows/columns reduces it ~10× with **zero numeric
change**. decimation is a low-effort OpenMP GO (one `#pragma` on the output-row
loop, no intrinsics, bit-identical) and is typically *first* in the post-process
chain, so it sits squarely in the hot path.

---

## 5. Minimal, upstream-safe integration sketch (follow-up only — no `src/` edits this phase)

All changes stay `#if`-guarded and correctness-identical. NEON behind
`__ARM_NEON` (+ optionally `RS2_USE_CPU_EXTENSIONS`), OpenMP behind the existing
`BUILD_WITH_OPENMP` / `_OPENMP`. Pattern mirrors `align.cpp`'s existing
`#pragma omp parallel for`.

**Spatial + decimation (highest value, simplest, OpenMP-only):**
```cpp
// spatial recursive_filter_horizontal_fp / _vertical_fp: the row (resp. column)
// loop bodies are already independent. Just parallelize the outer loop.
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static)
#endif
for (v = 0; v < _height; ++v) { /* unchanged recursive body */ }

// decimation decimate_depth: each output row j reads a disjoint band of `scale`
// input rows and writes one output row. The loop currently uses TWO running
// accumulators that must BOTH become j-indexed before parallelizing:
//   block_start    -> in  + (size_t)j * scale * width_in   (input band)
//   frame_data_out -> out + (size_t)j * _padded_width      (output row)
// (the inner `*frame_data_out++` cursor and the per-row zero-pad then write
//  into that row only). With both fixed:
#if defined(_OPENMP)
#  pragma omp parallel for schedule(static)
#endif
for (int j = 0; j < _real_height; ++j) { /* unchanged median body */ }
```
Both are bit-identical (each line / output-row is a self-contained computation
with no shared state). Spatial needs the same one-line guard on the vertical
column loop. **decimation requires converting *both* the input-band offset and
the output-row pointer to `j`-indexed expressions first** — leaving
`frame_data_out++` as a shared accumulator would race; the bench already does
both correctly and can serve as the regression oracle.

**Temporal / disparity / threshold (NEON):** add a `#if defined(__ARM_NEON)`
fast path that vectorizes the common arithmetic and falls through to the existing
scalar tail for the remainder, e.g.:
```cpp
#if defined(__ARM_NEON)
    // 8-px/iter: du*d, range compare, masked store  (threshold)
    // 4-px/iter: vdivq_f32(factor, d) w/ nonzero select  (disparity)
    // 8-px/iter: masked alpha-blend + per-lane persistence-LUT gather (temporal)
#endif
    for (; i < n; ++i) { /* existing scalar kernel, unchanged */ }
```
Optionally wrap the row loop in `#pragma omp parallel for` under
`BUILD_WITH_OPENMP` for the NEON+OMP tier. The bench in this repo is the
correctness oracle: every variant already asserts bit-equality to the current
scalar code, so an upstream PR can reuse it as a regression gate.

**Risk / upstream posture:** spatial-OMP and decimation-OMP are the safest (no
intrinsics, pure loop-parallelism, identical output) and should land first. NEON
paths need the FP-contract and true-divide discipline documented above to keep
bit-identity across the scalar/NEON boundary; the bench enforces it.

---

## 6. Reproduce

```bash
scripts/gb10/bench-filters.sh           # builds 3 flavors, runs all, asserts bit-identity
ITERS=800 WARMUP=80 scripts/gb10/bench-filters.sh   # tighter stats
```
The harness builds into `scripts/gb10/.bench-build/` and **deletes it on exit**
(disposable; never committed).
Suggested `just` recipe line (add manually to the justfile — not edited here):
```
gb10-bench-filters:
    scripts/gb10/bench-filters.sh
```
