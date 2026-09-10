# Upstream report drafts — prepared 2026-09-10, NOT yet filed

Two findings from the GB10 fork's v2.58.4 reconcile are **generic librealsense issues**, not GB10
workarounds, and belong upstream (`realsenseai/librealsense`, formerly `IntelRealSense/librealsense`).

**Status: drafted, not submitted.** Filing on a public tracker under the account owner's identity is
an outward-facing action and has not been authorised. Nothing here is blocking the fork — both are
already fixed on `master`.

---

## Draft 1 — Bug report + fix: exit-time double free from duplicated `rsutils` globals

**Affects:** 2.58.4 (latent earlier). **Reproduces on stock x86_64**, no special hardware.

### Summary

Every tool linking `realsense2` can abort at exit with `free(): double free detected in tcache 2` —
including `rs-enumerate-devices --version`, which never opens a camera.

### Cause

`rsutils` is a **STATIC** library linked **PUBLIC** into the **shared** `realsense2`:

| File | Line |
|---|---|
| `third-party/rsutils/CMakeLists.txt` | `add_library( rsutils STATIC "" )` |
| `CMakeLists.txt` | `target_link_libraries( ${LRS_TARGET} PUBLIC rsutils )` |
| `CMakeLists.txt` | `hide_bundled_archive_symbols(...)` — **`rsutils` is not in the list** |

So every executable that links `realsense2` also links `librsutils.a` and gets a **second
definition** of each of the five globals `rsutils` owns. With default (preemptible) visibility, ELF
resolves both the library's and the executable's references to the **executable's** copy —
`librealsense2.so`'s initializer constructs that object and registers a destructor for it, and the
executable's initializer does the same. One object, two constructions, two destructions.

The five affected globals:

```
rsutils::null_json  rsutils::missing_json  rsutils::empty_json_string
rsutils::empty_json_object          <- the four json sentinels; these are the crash
rsutils::g_librealsense_elpp_id     <- same defect, currently silent
```

`g_librealsense_elpp_id` does not abort only because `LIBREALSENSE_ELPP_ID` is `"librealsense"` —
12 characters, inside libstdc++'s 15-character short-string buffer — so the string holds no heap
allocation and the second destruction frees nothing. **Renaming the logger id to anything longer
turns it into the same crash.**

### Why it looks version- and standard-dependent (it isn't)

None of an executable's own objects reference `missing_json`; the archive drags `json.cpp.o` in
transitively, and how much of `librsutils.a` is extracted depends on which members are referenced:

| `-std` | rsutils symbols pulled into the exe | sentinels among them | Result |
|---|---:|---:|---|
| `c++14` | 18 | 0 | clean |
| `c++20` | 105 | 4 | double free |

The `-std` only decides whether the linker extracts the defective archive member. **`4bbc18032`
(`-Wl,--exclude-libs`) is not the cause** — it drops the `.so`'s exported json symbols 511 → 158, so
the executable's references can no longer bind to the library's copy, which makes the latent bug
*reachable*. That change fixes a real ROS 2 FastDDS ABI crash and should not be reverted.

### gdb evidence

```
#8  ~basic_json<..., rsutils::json_base>()   at 0x0000aaaaaad65590   <- EXECUTABLE range
#9  __cxa_finalize
#10 __do_global_dtors_aux   from .../librealsense2.so.2.58           <- LIBRARY range 0x0000ffff...
#11 _dl_call_fini -> _dl_fini -> __run_exit_handlers -> exit
```

### Proposed fix

Give the five globals hidden visibility so each module's copy is private and non-preemptible, and is
constructed and destroyed exactly once. Safe because both are immutable and compared by **value**,
never by address — `json_ref::exists()` tests `_j.is_discarded()`, and the elpp id is passed to
`el::Loggers::getLogger()`, which looks up by string content. The attribute must be repeated on each
definition; GCC emits default visibility for a definition that does not carry it.

Implemented as `third-party/rsutils/include/rsutils/visibility.h` (`RSUTILS_LOCAL`). On Windows it
expands to nothing: PE has no symbol interposition and `src/realsense.def` lists none of the five.

The alternative — adding `rsutils` to `hide_bundled_archive_symbols` — was rejected as a much larger
ABI change than making five immutable globals module-local.

### Verification

`.so` exports and executable dynamic-symbol entries for all five go 4/1 → **0/0**. Nine tools × 3
runs rc=0 with clean stderr on x86_64 (unfixed control still rc=134); ten tools on aarch64. Unit
tests pass; `pyrealsense2` imports. `--debug` still emits log lines, proving the hidden logger id is
still resolved across the DSO boundary.

**Side effect:** this also resolves duplicate-symbol problems in `rs-dds-adapter` shutdown.

---

## Draft 2 — Finding: zero-copy is a large *regression* for `rs.align`, and a large win for pointcloud

**Affects:** `BUILD_WITH_CUDA_ZEROCOPY` on integrated GPUs (2.58.4+). Measured on NVIDIA GB10
(`cudaDevAttrIntegrated=1`), CUDA 13.0, D435 848×480.

### The finding

`src/cuda/cuda-pointcloud.cu` is wired for zero-copy; `src/proc/cuda/cuda-align.cu` is not. Wiring
align the same way makes it **6.6× slower**. A per-buffer ladder isolates why:

| What is mapped | align p50 |
|---|---:|
| nothing (current upstream) | 0.321 ms |
| **inputs only** | **0.301 ms (−6.2%)** |
| output only | 2.237 ms (**+6.9×**) |
| inputs and output | 2.195 ms (**+6.8×**) |

### Why

The discriminator is the **access pattern of the mapped buffer**, not zero-copy itself:

- Reads of the depth/colour planes are streaming and coalesced → serving them from mapped host
  memory costs little and saves a full-frame H2D.
- `kernel_depth_to_other` resolves occlusion with **`atomic_min_uint16`**. Atomics against host
  memory over the coherence fabric are dramatically slower than against device-local memory.
- `cuda-pointcloud.cu` writes one point per thread with **no atomics**, which is why mapping *its*
  output is a **−42%** win (0.234 → 0.135 ms) rather than a regression.

**Suggested rule for integrated targets: map streaming reads; keep atomic or scattered writes
device-local.** Worth stating in the zero-copy documentation, because the intuitive reading —
"unified memory means copies are free" — is wrong here by a factor of ~7, and the next person to
wire a kernel naively will hit it.

All configurations are byte-identical (164-frame `.db3` playback, matching SHA-256 over aligned
depth planes and over pointcloud vertex buffers).

---

## Also worth a maintainer's attention

- **`sccache` breaks every `.cu` compile.** It mangles the stub translation unit `nvcc` generates
  (`__cudaLaunch` macro arity, in `/tmp/sccache_nvcc*/x_0.cudafe1.stub.c`). Not a librealsense bug,
  but a build-doc note would save the next person the same day: do not set
  `CMAKE_CUDA_COMPILER_LAUNCHER=sccache`.
- **`rs-record` silently changed capture format expectations** — 2.58.4 requires a `.db3` (rosbag2)
  path and rejects `.bag` at `pipeline start`, not at file open. Worth a release-note line.
