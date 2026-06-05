# GB10 librealsense fork — Hardening & Optimization Proposals (2026-06-05)

**Status (updated 2026-06-05, master `f38fd3471`):** several P0/P1 items have since LANDED and were
**live-validated controller-GREEN**; the rest remain proposed next steps.

| item | what | status | commit / validation |
|------|------|--------|---------------------|
| **single-opener lock** | flock-per-device, refcounted — prevents the 2-PROCESS open (the #1 controller-death cause) | ✅ **DONE** | `08c0360cb` — Test A: 2nd process queues, never simultaneous, no leak/spin; Test B: same-process depth+color OK |
| **H1** | SAFE-STOP: relocate the read-endpoint `clear_halt` out of the post-stop teardown window into the settled start window | ✅ **DONE** | `f38fd3471` — 3× single-stream stop→start, frames resume each cycle, GREEN |
| **H2 (F6)** | NDEBUG-safe `~usb_context` (interrupt + wake before join — no teardown hang on vigil's restart) | ✅ **DONE** | `8a08572e0` — compile-verified |
| **H5 (F5)** | `_active` → `std::atomic<bool>` (event-thread/dtor data race) | ✅ **DONE** | `8a08572e0` |
| (audit) | libusb error path: `errno`→`sts` classification, null-guard event-thread cb, catch-by-ref P7 reason | ✅ **DONE** | `ed0903a1a` |
| **H3** | Teardown/health watchdog: detect a wedging controller, fail fast not hang | ⬜ **next** | OFFLINE unit + 2-STREAM HIL |
| **H4** | Reconfigure-WITHOUT-`stop()` recovery (re-`config`, not destroy/recreate) — directly targets vigil's `restart_pipeline` | ⬜ **next (P1, high value)** | OFFLINE design + 2-STREAM HIL |
| **H6 (F8)** | Bound the 100 ms URB-drain stacking across N streams on close | ⬜ next | OFFLINE + 2-STREAM HIL |
| **H7 / H8** | Per-frame align-alloc reduction / USB-thread↔CUDA affinity pinning | ⬜ next (perf — **likely Finding-A-marginal; bench before building**) | OFFLINE bench (needs a number) |
| **H9 (F10)** | gl-lane dtor / `atexit` shutdown (static-teardown GL UAF) | ⬜ next (P2 — test-bed/posebench only, not vigil) | OFFLINE + 1-STREAM HIL |
| **H10** | Re-acquire state-machine hardening (P7 counter robustness) | ⬜ next | OFFLINE unit |
| **lock fail-fast** | a variant that *aborts* the 2nd opener instead of queueing (clearer footgun UX) | ⬜ next (refinement) | needs defeating the enumerator swallow+retry |

**Original framing (still valid):** static reading of the GB10 code paths + the reliability audit, ranked by
consumer relevance. The two highest-value items (H1 + the single-opener lock) are now landed + HIL-proven.

**Framing:** the primary consumer (`vigil-spark`, see `vigil-spark-integration.md`) is
**unavoidably 2-stream** (color 640×480 bgr8@30 + depth 640×480 z16@30, one camera, one
process) and **its failure-recovery does `pipeline.stop()→sleep→start()`** on a `wait_for_frames`
error (`realsensenode.py:90–144`). That restart is the *exact* `-110`/Stop-Endpoint death
trigger the fork's safe envelope warns about. So the highest-value work here is **2-stream
survivability**, not raw throughput. This doc leads with that.

**The killer (unchanged, not preventable in userspace):** the NVIDIA GB10 `xhci_plat_hcd`
(ACPI `NVDA8000`) cannot complete a **Stop-Endpoint** command after any control-path `-110`
timeout, and has no recovery path → `HC died` → reboot. Reproduced on BOTH RSUSB and native
V4L2 (`FINDINGS-2026-06-03.md`). Everything below **shrinks the trigger surface or fails fast/
clean** — none of it can fix the silicon. Claim "safer," never "safe."

**Scope note on "byte-identical upstream":** every proposal carries an `#if`-guard /
upstream-safety note. The prior audit's "src/gl is read-only" / "don't touch .cu" were *that
audit's apply-constraints* — this is a proposal doc, so it proposes freely against `src/libusb`,
`src/uvc`, `src/cuda`, `src/gl`. Where a fix is a defensible *unconditional* upstream
improvement (not GB10-specific), that is called out as an alternative to the `#if`-guarded form.

**Validation tags used throughout:**
- **[OFFLINE]** — provable with static analysis, a standalone compile, a unit test, or TSan/ASan
  on a non-HIL build. No camera, no reboot risk.
- **[1-STREAM HIL]** — needs a live D400 but only single high-rate stream (the shipped-safe
  envelope; low controller-death risk).
- **[2-STREAM HIL]** — needs the eyes-open color+depth config at vigil's profile. Carries
  real controller-death risk; run with the `HC died` tripwire and a reboot budget.

---

## Ranking summary

| ID | Proposal | Class | Priority | Validation | vigil-relevant? |
|----|----------|-------|----------|------------|-----------------|
| **H1** | SAFE-STOP teardown ordering (drain→cancel→settle→close), bounded | 2-stream survivability | **P0** | OFFLINE + 2-STREAM HIL | **YES (direct)** |
| **H2** | F6: NDEBUG-safe `~usb_context` (interrupt + bounded join + leak-not-hang) | Deferred audit / teardown | **P0** | OFFLINE + 1-STREAM HIL | **YES (restart path)** |
| **H3** | Teardown/health watchdog: detect a wedging controller, fail fast not hang | 2-stream survivability | **P0** | OFFLINE + 2-STREAM HIL | **YES (direct)** |
| **H4** | Reconfigure-without-stop() recovery (re-`config` not destroy/recreate) | 2-stream survivability | **P1** | OFFLINE design + 2-STREAM HIL | **YES (direct)** |
| **H5** | F5: `_active` → `std::atomic<bool>` (event-thread/dtor data race) | Deferred audit | **P1** | OFFLINE (TSan) | YES (event thread) |
| **H6** | F8: bound the 100 ms URB-drain stacking across N streams on close | Edge/failure-mode | **P1** | OFFLINE + 2-STREAM HIL | YES (restart latency) |
| **H7** | Per-frame alloc reduction on the 2-stream align hot path | Perf | **P2** | OFFLINE bench (needs number) | YES (if measured) |
| **H8** | USB-event-thread vs CUDA CPU affinity / pinning | Perf | **P2** | OFFLINE bench (needs number) | maybe (unmeasured) |
| **H9** | F10: gl-lane dtor / `atexit` shutdown (static-teardown GL UAF) | Deferred audit | **P2** | OFFLINE + 1-STREAM HIL | **NO (test-bed only)** |
| **H10** | Re-acquire state-machine hardening (P7 counter robustness) | Edge/race | **P2** | OFFLINE (unit) | YES (REFUSE-loop) |

> **Why H9 (F10) is P2 here, not P0:** vigil never loads `librealsense2-gl.so` — keep-on-GPU is
> architecturally blocked for vigil by the cross-process CPU-JPEG hop (`vigil-spark-integration.md`
> §2 #5). F10 is a real fix for the **keep-on-GPU test-bed / posebench render**, not vigil's
> path. Ranked by consumer relevance it drops below the libusb survivability items.

---

# CLASS 1 — 2-stream survivability (lead; the real consumer need)

## H1 — SAFE-STOP: ordered, bounded stream teardown that minimizes the Stop-Endpoint storm  · P0

**Problem + evidence.** `rs_uvc_device::close()` (`src/uvc/uvc-device.cpp:170–208`) already has the
P4b inter-stop settle (50 ms GB10 / 0 upstream) between `_streamers[i]->stop()` calls. But the
*ordering within a single streamer stop* is what arms the lethal Stop-Endpoint: the streamer
poll loop cancels in-flight URBs and the close path issues `reset_endpoint` →
`libusb_clear_halt` → Stop-Endpoint (`uvc-streamer.cpp:121,132,184,225`, `uvc-device.cpp:492`).
Under 2-stream load a `-110` on a control transfer (`messenger-libusb.cpp:36–47`) immediately
precedes the clear-halt, and that is the sequence the controller cannot survive
(`FINDINGS-2026-06-03.md` TL;DR §1; death #3 was a *setup* control timeout on the dual config).
vigil's `restart_pipeline()` hits this every recovery cycle.

**Concrete change sketch.** Introduce a SAFE-STOP path in `rs_uvc_device::close()` that, under
GB10 tuning, **drains then quiesces before any Stop-Endpoint-issuing call**:

```cpp
// src/uvc/uvc-device.cpp, rs_uvc_device::close(), GB10 branch
#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
    // Phase 1: stop submitting NEW transfers and let in-flight URBs DRAIN naturally
    //          (bounded) before we cancel — a natural completion does not arm a
    //          Stop-Endpoint, a cancel does. Quiescing first shrinks the cancel set.
    for (auto&& s : _streamers) s->request_quiesce();      // new: set a "no-resubmit" flag
    drain_inflight_urbs(usb_tuning::DEFAULT_DRAIN_MS);     // new: bounded wait, default 30ms

    // Phase 2: settle-spaced stop() as today (P4b), but the cancel set is now smaller
    int settle = usb_tuning::resolve_stop_settle_ms(getenv("RS2_USB_STOP_SETTLE_MS"),
                                                    usb_tuning::DEFAULT_STOP_SETTLE_MS);
    for (std::size_t i = 0; i < _streamers.size(); ++i) {
        const bool was_running = _streamers[i]->running();
        _streamers[i]->stop();
        if (settle > 0 && was_running && i + 1 < _streamers.size())
            std::this_thread::sleep_for(std::chrono::milliseconds(settle));
    }

    // Phase 3: do NOT clear_halt on the close path under GB10 unless the endpoint is
    //          actually halted — an unconditional clear_halt is a gratuitous Stop-Endpoint.
    //          (Gate the uvc-streamer.cpp:184/225 reset_endpoint behind a "was it stalled?"
    //          check, same spirit as the watchdog_should_reset rate-limit at :117.)
#else
    for (auto&& s : _streamers) s->stop();   // upstream: byte-identical
#endif
```

The new pieces are: `request_quiesce()` (a `std::atomic<bool> _no_resubmit` the poll loop checks
before re-arming a URB — pairs with H5), a bounded `drain_inflight_urbs()` helper, and a new
`DEFAULT_DRAIN_MS` constant in `usb-tuning.h` (a pure tunable, unit-testable like the others).
Phase 3 is the highest-leverage: **skip the unconditional `clear_halt`/`reset_endpoint` on a
clean stop** — only issue it when the endpoint is genuinely halted — removing one armed
Stop-Endpoint per stream per teardown.

**Expected benefit.** Fewer armed Stop-Endpoint commands per teardown, and they are issued only
after in-flight traffic has drained — so a teardown that races a `-110` is far less likely to
stack a cancel-storm onto the control-timeout. This is the single change most directly aimed at
vigil's `stop()→start()` recovery loop.

**Risk / effort.** Medium. Phases 1–2 are low-risk (additive, bounded). Phase 3 (conditional
clear_halt) changes teardown behavior and must be proven not to leave an endpoint halted across
a restart — that is the one part that needs careful 2-stream HIL. `drain_inflight_urbs` must be
strictly bounded (reuse the F8 budget, H6) so it can never hang teardown.

**Validation.** [OFFLINE] unit-test the new `usb-tuning.h` tunables + the "is endpoint halted?"
decision helper; compile-prove byte-identical with the define OFF. [2-STREAM HIL] vigil-profile
stop→start churn (≥50 cycles) with the `HC died` tripwire; compare armed-Stop-Endpoint count and
`-110` incidence vs the current P4b-only path. **Upstream-safety:** entire SAFE-STOP body is
`#if RS2_GB10_USB_TUNING` — define-off path is the existing byte-identical tight loop.

---

## H3 — Teardown/health watchdog: detect a wedging controller and fail fast instead of hanging  · P0

**Problem + evidence.** Two *distinct* failure modes today have no fast-fail:
1. A teardown that hangs (F6 join-forever, F8 100 ms-per-stream drain stacking).
2. A controller already entering the death spiral (repeated `-110` on control transfers) where
   continuing to issue transfers only feeds the storm.

Note this is a **different mechanism** from the existing `watchdog_should_reset`
(`uvc-streamer.cpp:117`), which merely *rate-limits* `reset_endpoint` **inside the streaming
poll loop**. That one throttles resets during steady-state streaming; this proposal is a
**teardown/health watchdog** that aborts a hung join or trips on a `-110` burst. Keep them
separate.

**Concrete change sketch.** A small, header-only policy helper in `usb-tuning.h` plus two call
sites:

```cpp
// usb-tuning.h — pure decision (unit-testable, no camera)
//  trip when N control-path -110s land inside a window: the controller is wedging.
inline bool controller_wedging(int recent_minus110_count, int threshold /*e.g. 3*/)
{ return recent_minus110_count >= threshold; }

// usb-tuning.h — bounded teardown deadline policy
inline bool teardown_deadline_exceeded(uint64_t waited_ms, uint64_t budget_ms)
{ return waited_ms >= budget_ms; }
```

Call site A (messenger): keep a small ring/count of recent `-110` (`LIBUSB_ERROR_TIMEOUT`)
classifications from `control_transfer`/`bulk_transfer` (now that F1/F2 classify `sts`
correctly, this count is *meaningful* — previously degraded to `OTHER`). When
`controller_wedging()` trips, surface a distinct `RS2_USB_STATUS` / log a fatal-class
diagnostic and **stop re-arming transfers** rather than spinning the storm.

Call site B (`~usb_context` join, H2): wrap the `_event_handler.join()` in the bounded-deadline
policy so a wedged controller cannot hang process teardown forever (degrade per H2 — leak, don't
detach-into-UAF).

**Expected benefit.** Converts two "hang the process / spin the storm" paths into "log loudly,
return a meaningful error, exit fast." For vigil this means a wedged controller surfaces as a
detectable node failure (it can `destroy_node()` deliberately) instead of a hung pipeline that a
watchdog has to SIGKILL.

**Risk / effort.** Low-medium. The policy helpers are pure and offline-testable. The risk is a
false trip aborting a recoverable transient — keep the threshold conservative (≥3 `-110` in a
short window) and make the bulk path advisory-first.

**Validation.** [OFFLINE] unit-test `controller_wedging` / `teardown_deadline_exceeded` boundary
cases; the `-110` ring under TSan. [2-STREAM HIL] induce a control timeout under dual-stream
load and confirm fast-fail + no hung teardown (the reboot-risk run — tripwire + budget). [1-STREAM
HIL] the teardown-deadline (H2 pairing) is exercisable single-stream. **Upstream-safety:** the
helpers are inert unless `WATCHDOG_*`/GB10 constants are the tuned values; gate the call sites
under `RS2_GB10_USB_TUNING`. Upstream sees the helpers compiled but unreferenced (byte-identical
behavior).

---

## H4 — Reconfigure-without-stop(): a recovery that re-`config`s instead of destroy/recreate  · P1

**Problem + evidence.** Death #2 was root-caused to **churn**: destroying and recreating the
whole `rs2::context`+pipeline between streams handed the device back to kernel uvcvideo, which
re-probed UVC control endpoints → `-110` storm → death (`FINDINGS-2026-06-03.md` correction;
`usb-tuning.h:124–143`). The P7 guard (`device-libusb.cpp:64–119`) *detects* this churn but fires
*after* the release already handed the device to uvcvideo. vigil's `restart_pipeline()` is a
softer version of the same anti-pattern (`stop()` then `start()` on the same pipeline — it does
not destroy the context, but it does a full stop).

**Concrete change sketch.** This is primarily a **documented recovery recipe + an SDK affordance**,
not a deep code change:
- Provide/strengthen a "session-stable" recovery helper so a consumer can recover from a
  transient frame error **without** a full `pipeline.stop()`: prefer `rs2::pipeline` reconfigure
  via a held `rs2::config`, or a sensor-level `stop()/start()` that does **not** release the
  device handle (no `set_power(D3)`), keeping `usb_device_libusb` alive (so the re-acquire
  counter never falls to 0 — `device-libusb.cpp:106–112`).
- Where the SDK currently forces a device release on `pipeline.stop()`, add a GB10-guarded path
  that keeps the handle warm across a soft restart.

**Expected benefit.** Removes the highest-severity churn trigger (full release → uvcvideo
re-probe) from the recovery path entirely. This is the death-#2 prevention the P7 guard can only
*advise* toward.

**Risk / effort.** Medium-high if it touches pipeline lifecycle; low if delivered as a
recovery-recipe + a `keep_handle_warm` flag on the existing stop path. Needs care that a soft
restart actually recovers a stalled stream (vs. a hard one).

**Validation.** [OFFLINE] design review against `device-libusb.cpp` live-counter semantics
(prove the warm-handle path keeps live ≥ 1). [2-STREAM HIL] vigil-profile: induce a frame
timeout, recover via the soft path, confirm zero device release / zero P7-trip / controller
GREEN, vs. the current `stop()→start()` baseline. **Upstream-safety:** `#if RS2_GB10_USB_TUNING`
for any lifecycle change; the recovery recipe itself is doc-only (no upstream impact).

---

# CLASS 2 — close the deferred audit items (mostly offline-validatable)

## H2 — F6: NDEBUG-safe `~usb_context` (interrupt + bounded join + leak-not-hang)  · P0

**Problem + evidence.** `~usb_context()` (`context-libusb.cpp:65–74`) guards the
"all handles closed before the context dies" invariant with **only** `assert(_handler_requests
== 0)` — which **compiles out under `-DNDEBUG`** (the Release/GB10 build). If a handle outlives
the context on the churn/error path, `_kill_handler_thread` is never set, the event thread spins
forever in `libusb_handle_events_completed` (`:92–95`), and `_event_handler.join()` (`:70–71`)
**hangs teardown permanently.** This is on vigil's restart/error path.

**Concrete change sketch — all three parts (the subtle ones):**

```cpp
usb_context::~usb_context()
{
    if (_list)
        libusb_free_device_list(_list, true);

#if defined(RS2_GB10_USB_TUNING) && RS2_GB10_USB_TUNING
    // (1) Force the event loop to exit regardless of _handler_requests balance, and
    //     INTERRUPT the blocked libusb_handle_events_completed so join() can proceed.
    //     Setting _kill_handler_thread ALONE does not wake a thread already blocked in
    //     libusb_handle_events_completed — libusb_interrupt_event_handler (libusb>=1.0.21)
    //     is required to unblock it.
    if (_ctx && _event_handler.joinable())
    {
        _kill_handler_thread = 1;
        libusb_interrupt_event_handler(_ctx);

        // (2) Bounded join. On timeout we must NOT detach-then-exit: a detached thread is
        //     still inside libusb_handle_events_completed(_ctx,…), so libusb_exit(_ctx)
        //     would be a use-after-free — WORSE than the hang. The correct degradation is
        //     to LEAK the context (skip libusb_exit) so the live thread's _ctx stays valid.
        if (join_with_timeout(_event_handler, GB10_TEARDOWN_JOIN_BUDGET_MS))
        {
            if (_ctx) { libusb_exit(_ctx); _ctx = nullptr; }
        }
        else
        {
            LOG_ERROR("usb_context teardown: event thread did not exit within budget; "
                      "leaking libusb_context to avoid a use-after-free (GB10 xHCI wedge).");
            _event_handler.detach();   // leak: do NOT libusb_exit(_ctx)
            _ctx = nullptr;            // drop our pointer without freeing
        }
    }
    else
#endif
    {
        assert(_handler_requests == 0);
        if (_event_handler.joinable()) _event_handler.join();
        if (_ctx) libusb_exit(_ctx);
    }
}
```

Three correctness points (each independently load-bearing):
1. **Interrupt, not just flag.** `_kill_handler_thread = 1` is the loop's exit condition but does
   not wake a thread *already blocked* inside `libusb_handle_events_completed`.
   `libusb_interrupt_event_handler(_ctx)` (libusb ≥ 1.0.21) unblocks it.
2. **Leak on timeout, never UAF.** A bounded join that times out must **skip `libusb_exit`** and
   leak the context — the detached thread still references `_ctx`. The documented failure mode is
   a **bounded leak**, not a hang and not a UAF.
3. **Cause-agnostic.** The fix is robust to a `_handler_requests` imbalance *regardless of who
   caused it* — it does not depend on tracing every `start/stop_event_handler` caller
   (`:81–107`). The dtor fix stands either way.

**Expected benefit.** Eliminates the permanent teardown hang in Release builds — the worst
failure mode on vigil's restart path (a hung process a supervisor must SIGKILL, masking the real
controller state).

**Risk / effort.** Medium. `libusb_interrupt_event_handler` requires libusb ≥ 1.0.21 (verify the
GB10 system libusb-1.0 version offline — trivial). `join_with_timeout` is a small standard helper
(`std::async`/`future::wait_for` or a `condition_variable` flag). The leak-on-timeout is
deliberate and bounded.

**Validation.** [OFFLINE] construct the imbalance in a unit harness (increment
`_handler_requests`, drop the context) under a Release `-DNDEBUG` build; confirm bounded teardown
+ the leak log instead of a hang. ASan to confirm no UAF on the timeout branch. [1-STREAM HIL]
the F6 probe from the audit (context drop during an in-flight stream). **Upstream-safety:**
`#if RS2_GB10_USB_TUNING` keeps upstream byte-identical (it retains the assert-only version).
**Alternative:** the interrupt+bounded-join is a *defensible unconditional upstream improvement*
(the assert-only teardown is a latent Release hang for everyone, not just GB10) — offer it
unguarded if upstream-acceptance is wanted; the GB10 guard is the conservative default.

---

## H5 — F5: `_active` → `std::atomic<bool>` (event-thread / dtor data race)  · P1

**Problem + evidence.** `_active` is a plain `bool` (`request-libusb.h:35`). The libusb event
thread writes it (`set_active(false)` in `internal_callback`, `request-libusb.cpp:17`), the
destructor polls it (`while(_active && attempts--)`, `:55–60`), and `submit_request`
writes/reads it (`messenger-libusb.cpp:86,90`). Cross-thread access to a non-atomic `bool` is a
**data race (UB)** on the teardown path.

**Concrete change sketch.** `std::atomic<bool> _active{false};` and the deleter check
(`request-libusb.cpp:38`) + dtor poll read it via `.load()`. Behavior-preserving.

```cpp
// request-libusb.h
-            bool _active = false;
+            std::atomic<bool> _active{false};
```

**Scope honesty (the advisor's point):** `atomic<bool>` fixes the **data race (UB)** — it does
**not** fix the *logical* completion-during-teardown race. That logical race is already handled
by the dtor's `libusb_cancel_transfer` + the bounded poll loop (`request-libusb.cpp:53–61`). So
this is strictly "remove UB," not "change teardown logic."

**Expected benefit.** Removes a real data-race UB on the hot submit + teardown path; lets a TSan
build run clean.

**Risk / effort.** Low. It changes a member type on the hot submit path; the `.load()/.store()`
are relaxed-sufficient (no inter-variable ordering relied upon). Validate it doesn't perturb the
submit hot path under churn.

**Validation.** [OFFLINE] **TSan** on a non-HIL build exercising submit/cancel around teardown
(the cleanest offline win in this doc). Compile-prove warning-neutral. **Upstream-safety:** this
is a pure correctness fix with no GB10 specificity — propose it **unconditional** (a defensible
upstream improvement, not `#if`-guarded). The audit already flagged the exact same fix for
`uvc_streamer::_frame_arrived` (`FINDINGS-2026-06-03.md` C++20 item) — do both together.

---

## H9 — F10: gl-lane dtor / `atexit` shutdown (static-teardown GL UAF)  · P2 (test-bed, not vigil)

**Problem + evidence.** `rendering_lane`/`processing_lane` are Meyers singletons with **no
explicit destructor** (`synthetic-stream-gl.cpp:106–116`). The *explicit*
`rs2_gl_shutdown_processing()`→`shutdown()` path is correct (lock → make-context-current via
`_ctx->begin_session()` → free GPU objs → `_ctx.reset()`, `:149–167`). The hazard is the
*implicit* path: a process that exits **without** calling `rs2_gl_shutdown_*` has the
compiler-generated singleton dtor drop `_ctx` (`shared_ptr<context>`) at static teardown, and
`~context` (`synthetic-stream-gl.cpp:381`) touches GLFW/GL **after** GLFW may already be
destroyed — a static-destructor-order crash / use-after-destroy.

**Concrete change sketch.** Register an `atexit`/idempotent guarded shutdown so the GL teardown
runs **before** static destruction, and make the implicit dtor a no-op w.r.t. GL:

```cpp
// On first lane init, register a one-time, idempotent, GL-safe shutdown:
std::atomic_flag s_shutdown_done = ATOMIC_FLAG_INIT;
void lane_atexit_shutdown() {
    if (!s_shutdown_done.test_and_set()) {
        // only touch GL if a context+GLFW are still alive; otherwise no-op (mirror F9/CUDA:
        // do NOT free GL objects during static teardown on a destroyed context).
        if (glfw_still_valid()) processing_lane::instance().shutdown();
    }
}
// in init(): std::call_once(s_atexit, []{ std::atexit(lane_atexit_shutdown); });
// explicit rs2_gl_shutdown_processing() also sets s_shutdown_done so atexit becomes a no-op.
```

This mirrors the CUDA-pool reasoning (F9): the safe move is **not to touch GL during static
teardown**; the `atexit` hook runs *before* static dtors while GLFW is still valid, and the
implicit singleton dtor then has nothing GL-ish to do.

**Expected benefit.** Eliminates the exit-without-shutdown SIGSEGV for keep-on-GPU tools
(posebench render, the GL test-bed) on the abnormal-exit path (SIGINT, exception escaping
`init`).

**Risk / effort.** Medium — GL/GLFW lifetime is fiddly; `glfw_still_valid()` must be conservative.

**Validation.** [OFFLINE] static reasoning + an ASan run of a minimal `rs2::gl` tool that exits
without explicit shutdown. [1-STREAM HIL] a keep-on-GPU tool, SIGINT mid-stream, confirm clean
exit. **Upstream-safety:** `src/gl` only builds under `-DBUILD_GLSL_EXTENSIONS=ON`; gate the
atexit registration so it is inert unless the GL lane is actually initialized. **vigil
relevance: NONE** — vigil does not load `librealsense2-gl.so` (cross-process CPU-JPEG hop blocks
keep-on-GPU, `vigil-spark-integration.md` §2 #5). This is a test-bed/posebench fix; ranked P2
accordingly.

---

# CLASS 3 — perf optimizations (honest: survivability > perf for vigil)

> **Lead verdict.** For vigil's actual 2-stream path the high-value work is **survivability, not
> perf.** The big measured win — **CUDA `rs.align` 15–19×** (`benchmarks.md:191–197`) — is **real
> but already auto-dispatched** when `BUILD_WITH_CUDA=ON`; the "win" is the *SDK swap*, not any
> new code change here. Keep-on-GPU render is **architecturally blocked** for vigil (IPC
> boundary). Pointcloud-cache, NVENC, and zero-copy ROS publish **don't apply** (vigil uses
> pyrealsense2-direct + JPEG transport, no pointcloud, no recording). Everything *net-new* below
> is **unmeasured — flagged needs-bench, likely Finding-A-marginal**. Per the project's own rule,
> no speedup is claimed without a number.

## H7 — Reduce per-frame allocations on the 2-stream align hot path  · P2 (needs-bench)

**Problem + evidence.** vigil calls `rs.align(rs.stream.color).process(frames)` **every frame**
(`realsensenode.py:84`). The CUDA align path is already 15–19× over CPU and uses cached pools
(R1, `cuda-conversion.cu`), so the GPU compute is not the bottleneck. The *candidate* remaining
cost is per-frame host-side allocation/copy around the aligned frame (frame-pool churn,
intrinsics marshaling). **This is a hypothesis, not a measured finding.**

**Concrete change sketch.** Profile the align `process()` call path for per-frame heap
allocations (frame allocator hits, temporary vectors); where a per-call temporary recurs, hoist
to a reused member buffer (same pattern as the cached CUDA pools). **Do not write code before the
profile.**

**Expected benefit.** Unknown until measured. If allocation is in the noise (likely, given the
cached pools already dominate), this is **Finding-A-marginal** — flag and drop.

**Risk / effort.** Low risk, low-medium effort; gated entirely on the bench showing a real cost.

**Validation.** [OFFLINE] perf/heaptrack on the align path at vigil's 640×480 profile; ship only
if there is a measured before/after. **Upstream-safety:** any buffer-hoist must stay
behavior-identical (same output bytes — the cached paths already prove max-abs-diff 0).

## H8 — USB-event-thread vs CUDA CPU affinity / pinning  · P2 (needs-bench, unmeasured)

**Problem + evidence.** The libusb event thread (`context-libusb.cpp:92–95`) and the CUDA align
work may contend for the same CPU clusters on the GB10 (Cortex-X925 + others; note
`FINDINGS-2026-06-03.md` flags `-mcpu=native` silently degrading to `armv8-a` baseline). Pinning
the USB event thread off the CUDA/compute cluster *might* reduce event-handling jitter under
2-stream load. **Purely a hypothesis — no measurement exists.**

**Concrete change sketch.** Optional, env-gated affinity hint for the event thread
(`pthread_setaffinity_np`) behind `RS2_GB10_USB_TUNING` + an explicit opt-in env, defaulting
OFF. **No default behavior change.**

**Expected benefit.** Unknown; plausibly marginal. Could *help* survivability indirectly if event
jitter contributes to control-path timeouts — but that link is unproven.

**Risk / effort.** Low (opt-in, off by default). Effort is mostly the benchmark to justify it.

**Validation.** [OFFLINE] microbench event-loop latency with/without pinning; [2-STREAM HIL] check
`-110` incidence with/without. **Do not ship without a number.** **Upstream-safety:** opt-in env +
`#if`-guard; default OFF = byte-identical.

---

# CLASS 4 — edge / race / failure-mode hardening (from reading the GB10 paths)

## H6 — Bound the 100 ms URB-drain stacking across N streams on close  · P1

**Problem + evidence.** F8: `~usb_request_libusb` (`request-libusb.cpp:53–61`) sleeps up to
10×10 ms = 100 ms per request if a transfer never returns, then leaks the `libusb_transfer`. On a
2-stream teardown with a deep URB pool (P2 raises the pool to 4/stream on GB10), this 100 ms can
**stack per request per stream** — turning vigil's `restart_pipeline()` 0.5 s sleep into a
multi-second teardown, widening the window the controller is in a fragile half-stopped state.

**Concrete change sketch.** Replace the per-request independent 100 ms poll with a **shared
deadline** across all in-flight requests of a streamer (one 100 ms budget for the whole drain,
not 100 ms × N), driven by the same `teardown_deadline_exceeded` helper as H3. A single
`condition_variable` woken by `internal_callback` (which already runs `set_active(false)`,
`request-libusb.cpp:17`) lets the drain return as soon as the *last* URB completes, with one
shared ceiling.

**Expected benefit.** Caps total teardown drain at one budget regardless of pool depth — keeps
vigil's restart fast and shortens the fragile half-stopped window. Pairs with H1 (the drain is
SAFE-STOP phase 1) and H3 (shared deadline).

**Risk / effort.** Medium — touches the request dtor + needs a shared signaling primitive across
a streamer's requests. The leak-on-timeout behavior is preserved (a never-returning URB is still
leaked, just not waited-on N×).

**Validation.** [OFFLINE] unit-test the shared-deadline helper; TSan the cv signaling.
[2-STREAM HIL] measure teardown wall-time at pool depth 4 vs the current per-request stacking.
**Upstream-safety:** `#if RS2_GB10_USB_TUNING` (upstream keeps the per-request 100 ms loop);
define-off byte-identical.

## H10 — Re-acquire state-machine robustness (P7 counter)  · P2

**Problem + evidence.** The P7 live/total counters (`device-libusb.cpp:57–119`) are guarded by a
single `reacquire_mutex()` and keyed by `unique_id` (USB bus-port) with a serial fallback
(`:114–118`). Two edge cases worth hardening:
1. A **never-powered transient probe object** that constructs then destructs without a `set_power`
   could perturb `total`/`live` — the audit's own "not yet iron-clad" caveat
   (`FINDINGS-2026-06-03.md` P7 note). A ctor that `commit`s only at the very end
   (`:196`) and a dtor that always `release`s (`:203`) can drift if construction throws *after*
   commit (it can't today, but it's fragile to future edits).
2. Under `RS2_GB10_REFUSE_REACQUIRE=1`, a refused construction throws (`:91`) and is swallowed by
   `create_usb_device` (`enumerator-libusb.cpp`, now logging via the F4 fix) — confirm the
   counters stay balanced on the throw path (they do: commit is after the throw point, so a
   refused acquire never increments — but this invariant should be unit-asserted, not just
   reasoned).

**Concrete change sketch.** Add a focused unit test (`unit-tests/usb-tuning/`) that drives
`is_dangerous_reacquire` / `resolve_reacquire_action` through the throw-path commit/release
ordering and asserts counter balance under: normal multi-sensor session, refused re-acquire,
and a transient probe. Optionally make commit/release RAII (a scope guard) so the
"commit-at-end, always-release" invariant is structurally enforced rather than convention.

**Expected benefit.** Hardens the P7 guard against counter drift under future edits and proves the
REFUSE-loop halts cleanly (its stated prevention value, `usb-tuning.h:135–143`).

**Risk / effort.** Low. Pure unit-test + optional RAII wrapper; no hot-path change.

**Validation.** [OFFLINE] the new unit test (Catch2 + standalone gate, same as the existing P7
tests). **Upstream-safety:** all inside `#if RS2_GB10_USB_TUNING`; tests only.

---

## Cross-cutting offline-first sequencing (recommended order)

1. **H5 (TSan, unconditional)** + **H2 (F6, NDEBUG hang)** — both heavily offline-validatable,
   both on vigil's restart path, both close deferred audit items. H2 is the single worst failure
   mode (permanent hang) and is the prerequisite for H3's bounded join.
2. **H3 helpers + H6** — pure policy + shared-deadline drain, offline-testable, prerequisites for
   H1's SAFE-STOP phase 1.
3. **H1 SAFE-STOP** — the headline survivability change; phases 1–2 offline-provable, phase 3
   (conditional clear_halt) gated on the 2-stream HIL.
4. **H4 reconfigure-without-stop** — design + recovery recipe offline; the warm-handle path on
   2-stream HIL.
5. **H10 (P7 unit tests)**, **H9 (gl atexit, test-bed)** — independent, offline-first.
6. **H7 / H8 (perf)** — only after a profile/bench produces a number. Likely Finding-A-marginal;
   do not ship on speculation.

**The honest bottom line:** vigil needs the fork to be **survivable under `stop()→start()`**, not
faster. H1/H2/H3 (the SAFE-STOP + NDEBUG-safe teardown + fail-fast watchdog) are the P0 trio that
directly harden the 2-stream restart path vigil cannot avoid. The perf items are real only as the
already-shipping CUDA-align SDK swap; net-new perf work here is unmeasured and flagged as such.
