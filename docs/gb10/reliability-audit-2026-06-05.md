# GB10 librealsense fork — Reliability / Failure-Resistance Audit (2026-06-05)

Scope: the GB10/DGX-Spark-relevant code paths — `src/libusb/*` (RSUSB control/transfer
path on the fragile xHCI), `src/cuda/*` (cached pools), and the frame/proc/gl teardown
ordering. Method: **static analysis + offline compile checks only — no camera, no
streaming, no heavy SDK build, no git ops.** Focus is the *error / recovery / teardown*
path, where the GB10 xHCI throws `-110` timeouts and can die.

Constraints honored:
- Owned files only: `src/libusb/*`, `src/cuda/*`, `src/proc/*` (none needed), and this doc.
- `scripts/gb10/rs-gb10-nonheadless-verify.py` (other agent), `justfile`, `README`,
  `wrappers/` — **not touched**.
- Every applied change recompiled and proven warning-neutral (identical `-Wall -Wextra
  -Wreorder` warning multiset vs the pre-edit file). See "Verification" below.
- `src/cuda/` and `src/proc/` confirmed **byte-identical to HEAD** (`git diff --quiet` clean):
  the cached-pool happy path is untouched.

---

## Findings table

| # | Area | File:line | Severity | Issue | Disposition |
|---|------|-----------|----------|-------|-------------|
| F1 | USB error classify | `src/libusb/messenger-libusb.cpp:93` (`submit_request`) | **MUST-FIX** | Returned `libusb_status_to_rs(errno)` but the meaningful status is `sts` (the `LIBUSB_ERROR_*` from `libusb_submit_transfer`). libusb does not set POSIX `errno` on this path, so the lookup almost always degraded to `RS2_USB_STATUS_OTHER`, losing `NO_DEVICE`/`TIMEOUT`/`BUSY` — the exact codes that drive GB10 recovery. | **APPLIED** (`errno`→`sts`) |
| F2 | USB error classify | `src/libusb/messenger-libusb.cpp:106` (`cancel_request`) | **MUST-FIX** | Same `errno`→status bug on the cancel path (teardown). | **APPLIED** (`errno`→`sts`) |
| F3 | Event-thread null-deref | `src/libusb/request-libusb.cpp:21-22` (`internal_callback`) | **MUST-FIX (UB)** | `auto cb = response->get_callback(); cb->callback(response);` — `cb` (an `rs_usb_request_callback` `shared_ptr`) is an **empty shared_ptr until `set_callback()` runs**, and a transfer can complete/cancel on the libusb event thread before/around that. Dereferencing a null `cb` crashes the event thread on the error/teardown path. (The inner `usb_request_callback::callback` null-checks its `std::function`, but that does not protect `cb` itself being null.) | **APPLIED** (`if(cb)` guard) |
| F4 | Diagnostic loss on REFUSE | `src/libusb/enumerator-libusb.cpp:123` (`create_usb_device`) | **SHOULD-FIX** | `catch (std::exception e)` caught **by value** (slicing → `what()` lost) and logged a broken printf-style string `"...index: %d" << idx` that never substituted `idx`. On the GB10 **P7 re-acquire REFUSE** path the `std::runtime_error` advice is swallowed here, so the operator never saw *why* the device refused. | **APPLIED** (`const&` + real `what()`) |
| F5 | Teardown data race | `src/libusb/request-libusb.h:35` + `request-libusb.cpp:17,49-54`, `messenger:86,90` | **SHOULD-FIX (race)** | `_active` is a plain `bool`. The libusb event thread writes it (`set_active(false)` in `internal_callback`); the destructor polls it (`while(_active && attempts--)`) and `submit_request` writes/reads it. Cross-thread access to a non-atomic `bool` is a data race on the teardown path. | **REPORTED** — fix is `std::atomic<bool>` (textbook, behavior-preserving) but it changes a member type on the hot submit path and the dtor's poll semantics; wants HIL churn validation. See "Reported, not applied". |
| F6 | Teardown deadlock | `src/libusb/context-libusb.cpp:69` (`~usb_context`) | **SHOULD-FIX (hang)** | `assert(_handler_requests == 0)` is the *only* thing guarding the invariant that all handles closed before the context dies — and `assert` **compiles out under `-DNDEBUG`** (the Release/GB10 build). If a handle outlives the context on the churn/error path, `_kill_handler_thread` is never set, the event thread spins forever in `libusb_handle_events_completed`, and `_event_handler.join()` **hangs teardown permanently**. | **REPORTED** — a correct wake needs `libusb_interrupt_event_handler`-class care (behavior-changing, HIL-gated). Do not blind-fix. |
| F7 | CUDA zero-grid launch | `src/cuda/cuda-pointcloud.cu:144,161`; `cuda-conversion.cu:337` | **NIT** | `numBlocks = count / THREADS_PER_BLOCK` (integer div). For a sub-block frame `count < 256` → grid 0 → kernel no-ops (stale output). Unreachable for any real D400 frame (≥307200 ≫ 256); upstream; present in both paths; and on the cached path a 0-grid launch would **throw** via the existing `cuda_or_throw(cudaGetLastError(),...)`, not corrupt silently. | **REPORTED (NIT)** — do not touch `.cu` (byte-identical constraint, zero real-world gain). |
| F8 | Slow URB drain on teardown | `src/libusb/request-libusb.cpp:49-55` (`~usb_request_libusb`) | **NIT** | If a transfer never returns, the dtor sleeps 10×10 ms = 100 ms then proceeds, and the `_transfer` deleter logs `"active request didn't return on time"` and **leaks** the transfer (cannot free an in-flight libusb_transfer safely). Bounded and intentional, but on GB10 stop/start churn this 100 ms can stack per stream. | **REPORTED (NIT)** — behavior is defensible; flag for churn timing observation only. |
| F9 | CUDA cached-pool R1 re-verify | `cuda-conversion.cu:53-64`, `cuda-pointcloud.cu:37-50` | **OK (no action)** | Re-verified the R1 hardening: `ensure_*()` does **free → null the ptr → zero the cap → cudaMalloc-or-throw**, so a throwing alloc leaves the pool in a clean empty state (no leak, no double-free, no stale-cap re-read on the next call). The static singletons intentionally have **no destructor** (freeing device memory in a static dtor races CUDA's own teardown → SIGSEGV); buffers are leaked at exit and the OS reclaims them. **R1 holds.** | **VERIFIED clean** |
| F10 | gl static-dtor ordering | `src/gl/synthetic-stream-gl.cpp:106-116,149-167` + `.h:208-228`; `src/gl/rs-gl.cpp:254-258` | **SHOULD-FIX (static-dtor crash)** | `rendering_lane`/`processing_lane` are **Meyers singletons with no explicit destructor**. Explicit teardown (`rs2_gl_shutdown_processing()` → `shutdown()`) correctly makes the GL context current via `_ctx->begin_session()`, frees GPU resources, and `_ctx.reset()`s — that path is **fine**. The hazard is the *implicit* path: if a process exits **without** calling `rs2_gl_shutdown_*`, the compiler-generated singleton dtor destroys `_ctx` (`shared_ptr<context>`) at static teardown. If that drops the last ref, `~context` touches GLFW/GL **after** GLFW (often itself static/already-gone) may be destroyed — a classic static-destructor-order crash, the exact race the CUDA pools dodge by having no dtor (F9). It does **not** re-make-current before freeing, so even a surviving context frees GL objects on the wrong/destroyed context. | **REPORTED** — `src/gl/` is outside the write-zone (read-only audit). The fork already mitigates at the call site: commit `7b07e93` guards the viewer's `shutdown_processing()` with `gl_inited` so it isn't skipped/double-run, but that lives in `wrappers/`/test-bed. **Correctness depends on the caller invoking `rs2_gl_shutdown_processing()` before GLFW terminate and before static teardown.** |

> **gl path — examined, summary:** Read `src/gl/rs-gl.cpp` (292 lines) and the lane impl/decl
> in `src/gl/synthetic-stream-gl.{cpp,h}`. The *explicit* `rs2_gl_shutdown_rendering/processing`
> ordering is correct (lock → make-context-current via `begin_session()` → free GPU objs →
> `_ctx.reset()`). The only actionable item is F10 (no-explicit-shutdown → static-dtor crash on
> exit), which mirrors the F9/CUDA reasoning. No use-after-free found *within* the explicit
> shutdown path. The keep-on-GPU shutdown sequencing itself lives in `wrappers/` (out of write-zone).

---

## Fixes APPLIED (high-confidence, behavior-preserving, error-path only)

All three live in owned `src/libusb/*`, touch only the **error path** (happy path byte-identical),
and qualify as "error propagation / null guard" per the apply rules.

### F1+F2 — `messenger-libusb.cpp`: classify the libusb status, not `errno`

```
// submit_request (line ~93)
-                return libusb_status_to_rs(errno);
+                // libusb_submit_transfer returns a LIBUSB_ERROR_* code in 'sts'; classify that, not
+                // the POSIX errno (which libusb does not contractually set, so libusb_status_to_rs(errno)
+                // almost always degraded to RS2_USB_STATUS_OTHER and lost NO_DEVICE/TIMEOUT/BUSY).
+                return libusb_status_to_rs(sts);

// cancel_request (line ~106)
-                return libusb_status_to_rs(errno);
+                // Classify the LIBUSB_ERROR_* code from libusb_cancel_transfer, not the POSIX errno
+                // (see submit_request above): errno is not set by libusb on this path.
+                return libusb_status_to_rs(sts);
```

Why safe: `sts` is already in scope (captured from the libusb call). The status was meaningless
before (≈always `OTHER`), so this is strictly a first-time-meaningful classification, not a
regression of any relied-upon value. Happy path returns `RS2_USB_STATUS_SUCCESS` unchanged.
**Origin: upstream (Katz, 2019) — fixed here because it is in an owned file on the GB10 recovery path.**

### F3 — `request-libusb.cpp`: null-guard the callback shared_ptr on the event thread

```
                     auto cb = response->get_callback();
-                    cb->callback(response);
+                    // get_callback() returns an empty shared_ptr until set_callback() runs (and the
+                    // request may complete/cancel on the libusb event thread before/around that). The
+                    // inner usb_request_callback already null-checks its std::function, but cb itself
+                    // can be null here — dereferencing it would crash the event thread on the error/
+                    // teardown path. Guard it.
+                    if(cb)
+                        cb->callback(response);
```

Why safe: pure null guard; when `cb` is non-null behavior is identical.

### F4 — `enumerator-libusb.cpp`: catch by const-ref + surface the real reason

```
-                    catch (std::exception e)
-                    {
-                        LOG_WARNING("failed to create usb device at index: %d" << idx);
-                    }
+                    catch (const std::exception& e)
+                    {
+                        // Catch by const-ref (catch-by-value sliced the exception and dropped what()).
+                        // Surface the real reason — e.g. the GB10 P7 re-acquire REFUSE message — instead
+                        // of the previous broken printf-style format string that never substituted idx.
+                        LOG_WARNING("failed to create usb device at index: " << (int)idx << ", error: " << e.what());
+                    }
```

Why safe: diagnostics-only; control flow (swallow + return nullptr at loop end) is unchanged.
The P7 REFUSE advice is *also* already `LOG_ERROR`'d at `device-libusb.cpp:90`, so this is
belt-and-suspenders, not the sole surface.

---

## Verification (warning-clean proof)

Build flags used (repo-root relative includes are required: headers use `<src/...>`):
```
g++ -std=c++17 -fsyntax-only -Wall -Wextra -Wreorder \
    -I. -Isrc -Iinclude -Ithird-party/rsutils/include -Ithird-party \
    -I/usr/include/libusb-1.0 -Icommon -DRS2_GB10_USB_TUNING=1 <file>.cpp
```
(No build tree on disk during the audit — the user's tree is reserved for camera HIL — so
each touched translation unit was compiled standalone against the real project + system
headers, `nvcc`/CUDA 12 and g++ 13.3 present.)

Method: compiled the **pre-edit (`git show HEAD:`)** and **post-edit** version of each file
under identical flags and path, then diffed the full warning multiset.

| Touched TU | Result |
|------------|--------|
| `messenger-libusb.cpp` | **Warning multiset IDENTICAL** to HEAD — 0 net change |
| `enumerator-libusb.cpp` | **Warning multiset IDENTICAL** to HEAD — 0 net change |
| `request-libusb.cpp` | **Warning multiset IDENTICAL** to HEAD — 0 net change |

Pre-existing warnings that remain (NOT mine, NOT on my changed lines): `timeout_ms` unused
in `reset_endpoint`, `r` unused + two `-Wsign-compare` in `get_device_path`. None of my edits
added or removed a warning.

Happy-path / byte-identical confirmation: `git diff --quiet HEAD -- src/cuda/ src/proc/` is
clean — **the CUDA cached-pool path was not modified.** The three libusb edits change only the
already-failed (`sts < 0`) branch, the null-`cb` branch, and a `catch` block; the success return
in each function is unchanged.

---

## REPORTED — not applied (need behavior change and/or camera HIL)

- **F5 `_active` data race** — make `_active` `std::atomic<bool>` and review the dtor poll loop
  (`request-libusb.h:35`, `.cpp:49-55`). Textbook fix, but it touches the hot submit path's
  member and the teardown poll; validate under stop/start churn before shipping.
- **F6 `~usb_context` teardown deadlock under `-DNDEBUG`** — the `assert(_handler_requests==0)`
  guard evaporates in Release. If a handle can outlive the context, the event thread never exits
  and `join()` hangs forever. Needs a real wake/timeout (e.g. set `_kill_handler_thread` + an
  event interrupt) — behavior-changing, HIL-gated. **Highest-value teardown item to investigate.**
- **F7 / F8 (NITs)** — zero-grid CUDA launch (unreachable for real frames, and would throw on the
  cached path) and the 100 ms URB drain on dtor (bounded; may stack per stream under churn).
  Observe only; do not edit `.cu` (byte-identical constraint).
- **F10 gl static-dtor crash on exit** — `src/gl/` is read-only here. The durable fix is to add an
  explicit lane `shutdown()` (or a no-op-if-already-shutdown teardown that does **not** touch GL in
  the static dtor) and ensure every keep-on-GPU entrypoint calls `rs2_gl_shutdown_processing()`
  before GLFW terminate. The viewer/wrappers already gate this with `gl_inited` (commit `7b07e93`);
  audit the other GB10 keep-on-GPU callers for the same guard. Behavior-changing; HIL-gated.

---

## Prioritized camera-HIL reliability tests for the operator

Run these against a live D400 on the GB10 (the static audit cannot exercise them):

1. **USB error-classification (validates F1/F2).** Induce a transfer failure (yank/replug or a
   `-110` timeout under 3-stream load) and confirm the SDK now surfaces
   `RS2_USB_STATUS_NO_DEVICE`/`TIMEOUT`/`BUSY` (was `OTHER`), and that recovery logic keyed on
   those codes now triggers. This is the top behavioral change to confirm.
2. **Re-acquire / P7 REFUSE diagnostics (validates F4).** With `RS2_GB10_USB_TUNING=1` and
   `RS2_GB10_REFUSE_REACQUIRE=1`, drive the destroy-context-then-recreate churn and confirm the
   `create_usb_device` log now shows the **full re-acquire remediation message** (was a broken
   format string). Also confirm WARN-only mode (no env) still proceeds.
3. **Event-thread robustness (validates F3).** Submit/cancel transfers around start/stop so a
   completion can land before `set_callback`; confirm no event-thread crash (previously a null
   `cb` deref). Soak under stop/start churn.
4. **Teardown deadlock probe (F6).** Construct a scenario where a handle/messenger outlives its
   `usb_context` on the error path (e.g. context drop during an in-flight stream) in a
   `-DNDEBUG` Release build; watch for a hung `~usb_context` (`join()` never returns). If
   reproducible, prioritize the F6 fix.
5. **Stop/start churn timing (F5/F8).** Repeated stream stop→start cycles; watch for the
   100 ms URB-drain stacking and any `_active` race symptoms (use TSan if a debug build is
   available off the HIL rig).
6. **gl exit-without-shutdown crash (validates F10).** Run a keep-on-GPU (`rs2::gl`) tool and exit
   the process **without** calling `rs2_gl_shutdown_processing()` (e.g. SIGINT / early return /
   exception escaping `init`). Watch for a static-destructor-order crash at exit. Then confirm the
   clean path (explicit shutdown before GLFW terminate) exits cleanly. Compare against the
   `gl_inited`-guarded viewer path.
