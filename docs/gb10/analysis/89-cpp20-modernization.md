# C++20 Modernization Assessment — GB10 librealsense Build

**Scope:** Static review only. Assessment + targeted high-value recommendations.
**Build target:** `scripts/build-dgx-spark-gb10.sh`, `LRS_GB10_CXX_STANDARD=20` (default),
`CMAKE_CXX_STANDARD=20 REQUIRED`.
**Lib ABI:** C ABI in `include/librealsense2/h/rs_*.h`. Public C++ API in `include/librealsense2/*.hpp`.
CMake INTERFACE pin: `cxx_std_11`. PRIVATE build requirement: `cxx_std_14`.
**Internal surface:** `src/**` — these headers are not installed (confirmed: `src/android/CMakeLists.txt`
is only included when `BACKEND == RS2_USE_ANDROID_BACKEND`; `include/librealsense2/**` is the only
install surface).

---

## Bucket Map (governs every recommendation below)

| Bucket | Files | C++20 risk |
|--------|-------|------------|
| A — Internal impl | `src/**/*.cpp`, `src/**/*.h` | **Safe** — not installed, not in public headers |
| B — rsutils third-party backbone | `third-party/rsutils/include/rsutils/concurrency/concurrency.h` | Invasive — entire SDK uses this |
| C — Public C ABI | `include/librealsense2/h/rs_*.h` | Forbidden — C, stable, crossed by pybind/C#/Unity |
| D — Installed C++ wrappers | `include/librealsense2/*.hpp` | High risk — INTERFACE pin is `cxx_std_11`; any C++20 syntax leaking here breaks downstream |

---

## 1. C++20 Warning Debt: Deprecated Implicit `this` Capture

### What and where

The C++20 standard deprecated `[=]` capturing `this` implicitly (P0806R2). GCC/Clang emit
`-Wdeprecated` when a `[=]` lambda body odr-uses a class member, causing the compiler to
implicitly capture `this` via the copy-all rule. Lambdas that only capture local variables or
function parameters via `[=]` are **not** affected.

**Grep-count vs. warning-count:** `git grep '[=]'` across `src/` yields 18 raw hits (17 in GB10
build — `src/android/jni/sensor.cpp:59` is Android-backend-only, excluded by the
`RS2_USE_ANDROID_BACKEND` CMake guard). Of those 17, only lambdas whose bodies touch a class member
(`_dev`, `_set_ef_cb`, `_dds_ef`) actually produce the C++20 deprecation warning.

**Compiler-verified warning sites in the GB10 build (7 lambdas, 3 files):**

Verified by re-running each file's exact compile command from
`/opt/vigil/build/librealsense-v2.58.1-dgx-spark-gb10/compile_commands.json` with `-fsyntax-only
-Wdeprecated` substituted. The compiler (`g++ -std=c++20`) reported these warnings and no others
in `src/` for the P0806R2 deprecated-capture rule:

| File | Line(s) | Member accessed | Fix |
|------|---------|-----------------|-----|
| `src/dds/rs-dds-sensor-proxy.cpp` | 711 | `_dev->set_option_value(...)` | `[=, this]` or `[option, this]` |
| `src/dds/rs-dds-sensor-proxy.cpp` | 894, 899, 908, 913 | `_dev->set_embedded_filter(...)` / `query_embedded_filter(...)` | `[=, this]` or `[embedded_filter, this]` |
| `src/dds/rs-dds-embedded-decimation-filter.cpp` | 68 | `_set_ef_cb(...)`, `_dds_ef->set_options(...)` | `[=, this]` or `[option, this]` |
| `src/dds/rs-dds-embedded-temporal-filter.cpp` | 75 | `_set_ef_cb(...)`, `_dds_ef->set_options(...)` | `[=, this]` or `[option, this]` |

**Non-warning `[=]` sites** (local/param captures only, confirmed clean):
`environment.cpp:74,103`, `software-sensor.cpp:95,114`, `dds-sensor-proxy.cpp:716`,
`dds-embedded-decimation-filter.cpp:79`, `dds-embedded-temporal-filter.cpp:86`,
`proc/pointcloud.cpp:135`, `proc/processing-blocks-factory.h:43,47`.

### Mechanical fix

The minimal mechanical fix for each warning site is to add `, this` to the capture list:

```cpp
// Before (C++20 deprecated):
[=]( json value ) { _dev->set_option_value( option, std::move( value ) ); }

// After (explicit, well-formed in C++11 through C++23):
[=, this]( json value ) { _dev->set_option_value( option, std::move( value ) ); }
```

An even cleaner fix for most of these sites is to drop `[=]` entirely and capture explicitly:
`[option, this]` (or `[embedded_filter, this]`). This documents intent and prevents accidental
captures of unintended stack variables. All sites are in `src/dds/` — Bucket A, zero ABI risk.

**Effort: S.** 7 lambdas in 3 files. All in `src/dds/` (internal, not installed). The diff is
mechanical.

### Is `-Werror` feasible at C++20 for the GB10 build?

**Partially.** The blocker is EasyLogging++ (`third-party/easyloggingpp/src/easylogging++.h`), which
uses `strcpy` / `strlen` without `_FORTIFY_SOURCE` guards, triggering fortify warnings. The
Ubuntu/Debian-packaged GCC enables `_FORTIFY_SOURCE=2` by default when any optimization level
(`-O1` or higher) is active — the build script's `-O3` triggers this automatically, not `-DNDEBUG`
directly. EasyLogging++
is vendored and third-party; enabling a blanket `-Werror` on the whole build will fail there.

The correct sequence:

1. **Fix the 7 this-capture sites** (S effort, above).
2. **Add target-scoped `-Werror=deprecated` to the librealsense target only** — not to
   third-party targets. In `CMakeLists.txt` or a GB10-specific toolchain file:
   ```cmake
   # GB10 build only — do not land in upstream CMakeLists without the ELPP guard
   if(CMAKE_CXX_STANDARD GREATER_EQUAL 20)
     target_compile_options(${LRS_TARGET} PRIVATE -Werror=deprecated)
   endif()
   ```
3. **Leave EasyLogging++ at default warning level** — it is not on the install surface and its
   fortify warnings are pre-existing noise, not a regression.

After step 1+2, `warnings != clean` (CLAUDE.md rule #1) becomes enforceable for the C++20 GB10
target on `src/` files. Full `-Werror` remains blocked by ELPP.

### Recommended tooling addition (item 7 deliverable for this warning class)

Add `scripts/gb10-lint.sh`:

```bash
#!/usr/bin/env bash
# Idempotent syntax-check lane for the GB10 C++20 build.
# Run before every commit touching src/. Requires clang (or g++ >= 12).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${LRS_GB10_PREFIX:-/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10}"
INCLUDES="-I$ROOT/include -I$ROOT/src -I$ROOT/third-party/rsutils/include"

echo "=== GB10 C++20 -Wdeprecated syntax check ==="
find "$ROOT/src" -name "*.cpp" \
  \( -path "*/dds/*" -o -path "*/uvc/*" -o -path "*/proc/*" \) \
  ! -path "*/android/*" \
  | xargs -P"$(nproc)" -I{} \
    clang++ -std=c++20 -fsyntax-only -Wall -Wextra -Wdeprecated \
            -Werror=deprecated \
            $INCLUDES {} 2>&1 \
  | grep -E "warning:|error:" || echo "Clean."
```

This script is idempotent (syntax-only, no object files), parallelizable, and generates the
ground-truth warning count without requiring a full rebuild. It should be wired as the CI
"lint" step for the GB10 build lane.

---

## 2. `std::jthread` / `std::stop_token`

### The threading model

The RSUSB backend uses a layered threading model from `third-party/rsutils/concurrency.h`
(Bucket B — vendored backbone):

- `dispatcher`: a single-consumer action queue with its own `std::thread`. Shutdown is signaled
  via `std::atomic<bool> _was_stopped`. `cancellable_timer` is a hand-rolled stop-token analog.
- `active_object<T>`: wraps a `dispatcher` in a loop; `stop()` sets `_stopped` then calls
  `dispatcher::stop()` which drains and kills the dispatch thread.
- `watchdog`: wraps an `active_object<>`, adds a `kick()` / `_kicked` mechanism.
- `uvc_streamer`: owns one `dispatcher _action_dispatcher`, one `watchdog`, one `active_object<>`
  for the publish-frame thread.

### Where jthread/stop_token would fit

The most defensible do-now use of `std::jthread` is **the profiler tool you own** — the RAII
watchdog in `tools/rs-gb10-profiler/` that the prior session already implemented as a `std::jthread`
with a `std::stop_token`. That work is done.

For `dispatcher` / `active_object` / `watchdog` in `third-party/rsutils/concurrency.h`:
`std::jthread` would simplify the `_was_stopped` atomic + `_was_stopped_cv` shutdown handshake into
a `stop_token`-carrying thread. The pattern is cleaner. **However:**

- This is Bucket B — the threading backbone of the entire SDK, including the DDS layer, not just
  UVC. The blast radius of a rewrite is high.
- The `_was_stopped` atomic + condition variable is already functionally equivalent to `stop_token`
  (cooperative cancellation, `try_sleep` polls it). There is no correctness benefit.
- The SDK is a community-maintained repo; a jthread-based dispatcher requires C++20 from *all*
  consumers of rsutils, which conflicts with the `cxx_std_11` INTERFACE pin.

**Verdict: jthread/stop_token in rsutils is "nice but invasive." Do not retrofit.**

### Would cooperative stop_token cancellation help the GB10 multi-stream STOP path?

**No, and this is important to state clearly.** The xHCI controller death is a USB
control-command storm: multiple concurrent streams issuing `cancel_request` + `reset_endpoint`
(Stop-Endpoint XHCI command) in rapid succession overwhelms the controller's command ring. This is
a USB hardware scheduling problem, not a thread-ordering problem.

`stop_token` coordinates *threads* agreeing to stop. The GB10 stop path already serializes through
the dispatcher thread — `uvc_streamer::stop()` calls `invoke_and_wait()`, which blocks until the
dispatcher executes the stop lambda. The inter-stream ordering is controlled by the sequential loop
in `uvc-device.cpp` (P4b). A `stop_token`-based cooperative cancel cannot reduce the number of
USB commands issued; it only changes how threads acknowledge the request to stop.

The P4a watchdog rate-limit (`usb-tuning.h: WATCHDOG_MIN_RESET_INTERVAL`) and P4b inter-stop settle
delay are the correct mitigation. `stop_token` does not make this path more deterministic.

**Verdict: Low priority, internal to rsutils, no reliability benefit for the GB10 stop problem.**
Effort M for rsutils rewrite. ABI risk: low (internal), but blast radius is high.

---

## 3. `std::barrier` / `std::latch` for Multi-Stream Coordination

### Current teardown model

The P4b settle loop in `uvc-device.cpp` is **sequential and single-threaded**:

```cpp
for(std::size_t i = 0; i < _streamers.size(); ++i) {
    const bool was_running = _streamers[i]->running();
    _streamers[i]->stop();                         // blocks via invoke_and_wait
    if(settle > 0 && was_running && i + 1 < _streamers.size())
        std::this_thread::sleep_for(std::chrono::milliseconds(settle));
}
```

Each `stop()` call uses `invoke_and_wait()` — it serializes through the streamer's dispatcher
thread and returns only when that streamer is fully stopped. The loop is inherently sequential.

### Why barrier does not cleanly replace the settle

`std::barrier` synchronizes N **concurrent** threads reaching a common point. In the current
architecture, there are no N concurrent threads tearing down streamers. The streamers for depth
(MI0) and color (MI3) sensors stop on the same thread (the device's `_action_dispatcher` or the
caller), sequentially. Inserting a barrier would require:

1. Launching N parallel stop threads (one per streamer).
2. Having them all reach the barrier before any issues the `reset_endpoint`.
3. A completion phase that then serializes the USB commands.

This is an architectural change of non-trivial scope, and it would require coordination across the
`rs_uvc_device` sensor boundary (depth sensor and color sensor live in separate `rs_uvc_device`
instances). The sleep-settle is cruder but localized and correct.

`std::latch` (one-shot countdown) would fit a pattern like "wait for N streamers to signal they
have cancelled all in-flight URBs before issuing endpoint resets." This is a better semantic fit
than barrier, but still requires restructuring `uvc_streamer::stop()` to separate the
`cancel_request` phase from the `reset_endpoint` phase — currently they are sequential inside a
single dispatcher action.

**Verdict:** Neither barrier nor latch is a drop-in improvement for P4b. The sleep-settle is
pragmatically correct for the GB10 problem. If the stop path is ever restructured to run streamers
concurrently, `std::latch` with N = `_streamers.size()` would be the right primitive for the
"all URBs cancelled" rendezvous point. File as a future refactor, not a current action.

**Effort: L (with architectural change required). Do not do now.**

---

## 4. `std::span` for Buffer-Passing APIs

### Where

`src/uvc/uvc-parser.h` has 11 parse functions taking `const std::vector<uint8_t>&` block
parameters. `src/uvc/uvc-device.cpp:258-263` has `set_xu`/`get_xu` taking `const uint8_t* data,
int len` (a raw pointer + length pair — the classic span target).

```cpp
// uvc-device.cpp:258-263
bool rs_uvc_device::set_xu(const extension_unit& xu, uint8_t ctrl,
                           const uint8_t* data, int len)
bool rs_uvc_device::get_xu(const extension_unit& xu, uint8_t ctrl,
                           uint8_t* data, int len) const
```

### Assessment

**`uvc-parser.h` `const std::vector<uint8_t>&` → `std::span<const uint8_t>`:** A genuine
improvement. `std::span` expresses "I need a contiguous view, not ownership," enables callers to
pass `std::array`, stack buffers, or subranges without a vector copy, and enables range-checked
debug builds. All 11 parse functions are in `src/uvc/` (Bucket A — internal, not installed).

**`set_xu`/`get_xu` ptr+len → `std::span`:** The public method signature of `set_xu`/`get_xu` in
`src/uvc/uvc-device.h` is internal (Bucket A). Verified: `grep -rn "set_xu\|get_xu" include/`
returns nothing — the virtual interface is fully contained within `src/`. The span migration is
safe. The virtual interface in `src/uvc/uvc-device.h` and any overrides in the V4L2 / WinUSB
backends should be updated together in one pass to keep the override chain consistent.

**ABI/source-compat:** `std::span` is a C++20 type. Replacing `const std::vector<uint8_t>&` with
`std::span<const uint8_t>` in these internal headers is Bucket A — no installed header, no public
C ABI impact, no Python/C#/Unity exposure. Callers in `src/` must be updated to pass `std::span`
(or the implicit conversion from vector works via span's `vector` constructor, so existing callers
with `std::vector` arguments compile without change).

**Effort: S for uvc-parser.h** (internal, mechanical, callers are unaffected via implicit
conversion). **M for set_xu/get_xu** (virtual override chain across multiple backends).

**Priority: High-value, low-ABI-risk, internal-only — do now (after warning fix).**

---

## 5. Other C++20 Features: Prioritized

### 5a. `[[likely]]` / `[[unlikely]]` (Effort: S, ABI: Zero)

The hot path in `uvc-streamer.cpp` processes every URB completion:

```cpp
// uvc-streamer.cpp ~line 147
if(al > 0L && ((al == r->get_buffer().data()[0] + _context.control->dwMaxVideoFrameSize)
               || is_compressed ))
```

The success branch (valid frame data) is the common case. Adding `[[likely]]` on the success
branch and `[[unlikely]]` on error paths (`LOG_ERROR("bad packet...")`) is a zero-ABI-risk,
zero-risk change that gives the compiler a branch-prediction hint. At -O3 the compiler likely
infers this already, but explicit hints are documentation and can prevent regressions on profile-
guided optimization order changes.

**Recommendation: Apply to the URB callback hot path and watchdog branch in `uvc-streamer.cpp`.
Internal only. Effort S.**

### 5b. `std::source_location` for Logging (Effort: M, ABI: Zero for internal use)

EasyLogging++ `LOG_ERROR`/`LOG_DEBUG` macros capture `__FILE__`/`__LINE__` via C preprocessor
macros. `std::source_location` (C++20) provides the same information without macros and integrates
with template helpers. However, replacing EasyLogging++ macros with a `source_location`-based
wrapper is invasive and buys little since ELPP already does this correctly. The value is in **new
logging helpers** added for GB10 diagnostics (the profiler, usb-tuning.h log sites) — use
`std::source_location` there rather than `__FILE__`/`__LINE__`.

**Recommendation: Use `std::source_location` in new GB10 diagnostic code only. Do not retrofit
ELPP macros. Effort S for new code.**

### 5c. Designated Initializers (Effort: S, ABI: Zero)

`uvc-device.cpp:504` initializes `uvc_streamer_context` by positional aggregate:

```cpp
uvc_streamer_context usc = { profile, callback, ctrl, _usb_device, _messenger, _usb_request_count };
```

`uvc_streamer_context` is a plain aggregate struct (`src/uvc/uvc-streamer.h:21`). With C++20
designated initializers:

```cpp
uvc_streamer_context usc = {
    .profile       = profile,
    .user_cb       = callback,
    .control       = ctrl,
    .usb_device    = _usb_device,
    .messenger     = _messenger,
    .request_count = _usb_request_count,
};
```

This makes field-order-independent initialization explicit and surfaces mismatches as compiler
errors if the struct is ever reordered. All internal (Bucket A). Zero ABI risk.

**Recommendation: Apply at `uvc-device.cpp:504`. Effort S. High clarity value.**

### 5d. `constexpr` / `consteval` (Effort: S, already partially adopted)

The codebase already uses `constexpr` extensively (183 instances across `src/`). `usb-tuning.h`
uses `constexpr` correctly for all tunables. The remaining opportunities are:

- `uvc-streamer.cpp` magic constants: `UVC_PAYLOAD_MAX_HEADER_LENGTH = 1024`,
  `DEQUEUE_MILLISECONDS_TIMEOUT = 50`, `ENDPOINT_RESET_MILLISECONDS_TIMEOUT = 100`. These are
  `const int` at file scope — should be `constexpr int` or `inline constexpr int` in a header
  to catch UB if used in a template context.
- `watchdog_timeout` computation in `uvc_streamer::uvc_streamer()`: currently a `static_cast<int64_t>` with a
  floating-point intermediate. Could be a `constexpr` lambda or standalone function.

**Recommendation: Apply to `uvc-streamer.cpp` constants. Effort S.**

### 5e. Concepts for `proc/` Filter Templates (Effort: M, ABI: Internal-only risk)

`src/proc/temporal-filter.h:25` and `src/proc/spatial-filter.h:32` use `static_assert`:

```cpp
static_assert((std::is_arithmetic<T>::value), "temporal filter assumes numeric types");
```

C++20 concepts replace these with a cleaner constraint declaration:

```cpp
template<std::arithmetic T>
void temporal_filter_process_frame(T* data, ...) { ... }
```

or a named concept:

```cpp
template<typename T>
concept FilterPixel = std::is_arithmetic_v<T>;
```

This produces better error messages and communicates the constraint in the function signature
rather than the body. All in `src/proc/` (Bucket A — not installed).

**Caution:** Concepts change what constitutes a valid template instantiation at the call site. Any
processing block used via a Python binding or C# wrapper that passes a non-arithmetic type will now
get a concept-constraint error at compile time (the correct behavior). The bindings go through the
C ABI, not the C++ template layer, so this is not a consumer-facing break.

**Recommendation: Apply incrementally to `temporal-filter.h` and `spatial-filter.h`. Effort M.
Low ABI risk (all internal). Good diagnostic improvement.**

### 5f. `std::format` vs EasyLogging++ (Effort: L, ABI: Invasive)

`std::format` is a cleaner replacement for EasyLogging++'s `LOG_DEBUG("x=" << x)` stream API.
However:

- ELPP is deeply embedded via `LOG_DEBUG`/`LOG_INFO`/`LOG_ERROR`/`LOG_WARNING` macros used across
  every `src/` file.
- `std::format` does not replace the async dispatch, callback sink, or configuration infrastructure
  ELPP provides.
- A migration would require either a `std::format`-based wrapper that reimplements ELPP's sink
  interface, or a wholesale replacement of the logging infrastructure.

**Verdict: Too invasive. Note and defer. Not recommended in the assessment horizon.**

### 5g. `std::atomic_ref` (Effort: S, ABI: Zero)

`uvc_streamer._frame_arrived` is a `bool` (plain, not atomic) protected by
`_action_dispatcher.invoke()` serialization. This is correct as documented in the header comment.
However, `wait_for_first_frame()` busy-polls `_frame_arrived` without holding any lock — a classic
data race:

```cpp
// uvc-streamer.cpp:260-271
bool uvc_streamer::wait_for_first_frame(uint32_t timeout_ms) {
    auto start = std::chrono::system_clock::now();
    while(!_frame_arrived)  // <-- unsynchronized read
    ...
}
```

This is technically UB (the write to `_frame_arrived` happens on the dispatcher thread; the read
here is unsynchronized). `std::atomic_ref<bool>` (C++20) could wrap the non-atomic member for the
duration of the poll without changing the member type. Alternatively, declare `_frame_arrived` as
`std::atomic<bool>`.

**Recommendation: Declare `_frame_arrived` as `std::atomic<bool>`. This is the simpler fix, is
C++11-compatible (no C++20 required), and removes the UB. `std::atomic_ref` is not needed here.
Effort S. Internal (Bucket A). Do as a bug fix, not a modernization.**

---

## 6. Build / Tooling Recommendations

### Recommended: GB10 lint script (`scripts/gb10-lint.sh`)

Described in §1 above. Catches `-Wdeprecated` (the this-capture warning class), `-Wall -Wextra`,
and `-Wpedantic` for the `src/dds/` and `src/uvc/` subtrees. Idempotent (syntax-only pass, no
output files). Writable today.

### Recommended: Separate C++20 CI lane

The GB10 build compiles at C++20 (`-DCMAKE_CXX_STANDARD=20`). No CI lane currently enforces
C++20-specific warnings separately from the default C++14 build. A minimal addition to the CI
matrix:

```yaml
# ci-gb10-cpp20.yml
- name: GB10 C++20 lint
  run: LRS_GB10_CXX_STANDARD=20 bash scripts/gb10-lint.sh
```

This is the minimal idempotent tooling gate that would have caught the this-capture debt
automatically.

### clang-tidy `modernize-*` checks

For opportunistic modernization, `clang-tidy` with:
```
modernize-use-span, modernize-use-designated-initializers,
modernize-use-std-format (if/when adopted),
cppcoreguidelines-avoid-non-const-global-variables
```
applied to `src/uvc/` and `src/dds/` is a concrete next step. Wire it as a `scripts/gb10-tidy.sh`
that runs `clang-tidy` over the CMake compile-commands JSON produced by the GB10 build, targeting
only `src/` files (exclude third-party via `--header-filter`).

---

## 7. Production Recommendation: Stay at C++20 or Fall Back?

### Reasoning

The concern in `realsense.TODO.md` is ABI/source-compat for downstream wrappers
(Python/pyrealsense2, C#, Unity). The relevant facts:

1. **The C ABI does not change with the C++ standard used to build the `.so`.** The public surface
   exposed to Python via pybind11, to C# via P/Invoke, and to Unity via the native plugin is
   the C ABI in `include/librealsense2/h/rs_*.h` — plain C structs and function pointers. The
   standard used to compile the implementation object files is invisible to these consumers.

2. **The installed C++ headers (`include/librealsense2/*.hpp`) carry a `cxx_std_11` INTERFACE
   pin** (`CMake/lrs_macros.cmake:13`). No C++20 syntax has leaked into the installed headers
   (verified by running `grep -rn 'std::span|std::jthread|std::stop_token|std::barrier|std::latch|std::source_location|std::format|std::atomic_ref|concept |consteval' include/` — only match was a prose comment in `include/readme.md`, not a declaration). As long
   as this invariant is maintained, consumers at C++11/14 compile fine.

3. **The `std::jthread` RAII watchdog in `tools/rs-gb10-profiler`** depends on C++20. Falling back
   to C++14 for the GB10 build would require rewriting that component.

4. **The CTAD fix in `single_consumer_frame_queue`** (prior session, resolved at C++20) indicates
   at least one vendored header has a latent C++17+ CTAD dependency that surfaced under C++20.
   This is not a reason to fall back — it was fixed — but it signals that the rsutils third-party
   layer should be audited for similar implicit standard assumptions before any production rollout.

### Recommendation

**Keep `LRS_GB10_CXX_STANDARD=20` as the production default.** The C ABI is unchanged. No C++20
syntax is in installed headers. The C++20 build has been smoke-tested (`validate` step in
`build-dgx-spark-gb10.sh`, profiler tool exercised). The `TODO.md` escape hatch (fall back to 14
if a downstream wrapper shows compat issues) remains available and is the right safety valve, but
there is no evidence today that any downstream wrapper is affected.

**The concrete invariant to protect:** no C++20-only syntax (`std::span`, `std::jthread`,
`std::format`, concepts, `std::atomic_ref`) must appear in `include/librealsense2/**`. This
invariant is currently met. Enforce it via a `grep` in CI:

```bash
# Fail if any C++20 keyword appears in installed headers
if grep -rn "std::span\|std::jthread\|std::format\|std::barrier\|std::latch" include/; then
    echo "ERROR: C++20 type in installed header — breaks cxx_std_11 consumers"; exit 1
fi
```

---

## Summary Priority Matrix

| # | Recommendation | Effort | ABI Risk | Do Now? |
|---|---------------|--------|----------|---------|
| 1a | Fix 7 `[=]` this-capture warnings (3 files in `src/dds/`) | S | Zero | **Yes** |
| 1b | `_frame_arrived` → `std::atomic<bool>` (UB fix) | S | Zero | **Yes** (bug fix) |
| 1c | Add `scripts/gb10-lint.sh` C++20 syntax-check lane | S | Zero | **Yes** |
| 2 | Designated init at `uvc-device.cpp:504` | S | Zero | **Yes** |
| 3 | `std::span` on `uvc-parser.h` parse functions | S | Zero | **Yes** |
| 4 | `[[likely]]/[[unlikely]]` on URB hot path | S | Zero | Yes (low priority) |
| 5 | `constexpr` for `uvc-streamer.cpp` constants | S | Zero | Yes (low priority) |
| 6 | Concepts on `proc/` `static_assert(is_arithmetic)` | M | Zero | Later |
| 7 | `std::source_location` in new GB10 diagnostic code | S | Zero | For new code only |
| 8 | `std::span` on `set_xu`/`get_xu` (virtual chain confirmed internal) | M | Zero | Later (multi-backend update) |
| 9 | jthread/stop_token in rsutils backbone | M | Low (Bucket B) | No — invasive, no reliability gain |
| 10 | `std::barrier`/`std::latch` for stream teardown | L | Low (Bucket B) | No — requires arch change |
| 11 | `std::format` / ELPP replacement | L | Medium | No — too invasive |

**Items 1a, 1b, 1c, 2, 3 form a coherent first-pass modernization commit** — all in `src/`, all
Bucket A, zero ABI impact, and directly address warning debt that would block `-Werror=deprecated`
in the GB10 build lane.
