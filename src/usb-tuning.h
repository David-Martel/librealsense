// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#pragma once

#include <string>
#include <cstdint>
#include <cstdlib>
#include <sstream>

// RSUSB tuning helpers for high-bandwidth D400 multistream, motivated by the
// NVIDIA DGX Spark / GB10 xHCI controller-death investigation (control-path
// starvation under 3 concurrent saturating bulk streams). These are pure,
// hardware-free policy functions so they can be unit tested without a camera;
// the impure parts (reading sysfs, sleeping, issuing USB resets) live at the
// call sites and delegate the *decision* here.
//
//   P2 - configurable URB pool depth   : resolve_usb_request_count()
//   P3 - usbfs_memory_mb preflight      : usbfs_memory_advice()
//   P4 - gentler stop (watchdog + delay): watchdog_should_reset(), resolve_stop_settle_ms()
//   P7 - device re-acquire guard        : resolve_reacquire_action(), reacquire_advice()
//   H3 - controller-wedge detector      : controller_wedging(), wedge_tracker
//   H3 - teardown deadline guard        : teardown_deadline_exceeded()
//
// Each tunable takes a compile-time builtin_default (raised in the GB10 build
// profile, left at the conservative upstream value otherwise) plus an optional
// environment override used only for controlled experiments.

namespace librealsense {
namespace usb_tuning {

// Per-stream RSUSB URB pool depth (P2). The upstream default is 2; deeper pools
// thicken the pipeline for concurrent bulk streams but cost pinned usbfs memory,
// so the result is clamped to a sane range.
constexpr uint8_t MIN_USB_REQUEST_COUNT = 2;
constexpr uint8_t MAX_USB_REQUEST_COUNT = 16;

// Inter-stop settle delay bounds in milliseconds (P4).
constexpr int MIN_STOP_SETTLE_MS = 0;
constexpr int MAX_STOP_SETTLE_MS = 1000;

// Controller-wedge detector bounds (H3). A "wedge" is detected from a burst of
// -110 (LIBUSB_ERROR_TIMEOUT) transfer errors arriving within a short sliding
// window: that pattern means the GB10 xHCI is no longer completing transfers and
// is sliding toward host-controller death, so the safe move is to FAIL FAST
// (abort the stream / surface an error) rather than issue yet another endpoint
// reset that could finish killing it. These bound the tunables below.
constexpr int    MIN_WEDGE_TIMEOUT_THRESHOLD = 2;      // never wedge on a single timeout
constexpr int    MAX_WEDGE_TIMEOUT_THRESHOLD = 64;
constexpr double MIN_WEDGE_WINDOW_MS         = 50.0;   // window must be a real interval
constexpr double MAX_WEDGE_WINDOW_MS         = 60000.0;

// Teardown-deadline bounds in milliseconds (H3, pairs with the F6 teardown guard).
// Bounds how long a join/drain may block before a wedged controller is declared a
// detectable failure instead of a permanent hang.
constexpr int MIN_TEARDOWN_BUDGET_MS = 0;       // 0 => disabled (never exceeded)
constexpr int MAX_TEARDOWN_BUDGET_MS = 60000;

// Single authoritative source for the compile-time defaults so the GB10-vs-upstream
// values cannot drift between translation units (uvc-device.cpp, uvc-streamer.cpp).
// Opt-in and OFF upstream: when RS2_GB10_USB_TUNING is undefined these are the stock
// values and every mitigation is a no-op (request_count=2, no settle, watchdog resets
// on every trigger). The GB10 build profile defines RS2_GB10_USB_TUNING=1.
#if defined( RS2_GB10_USB_TUNING ) && RS2_GB10_USB_TUNING
constexpr bool     GB10_TUNING_ENABLED         = true;
constexpr uint8_t  DEFAULT_USB_REQUEST_COUNT   = 4;    // deeper bulk pipeline (P2)
constexpr int      DEFAULT_STOP_SETTLE_MS      = 50;   // space out teardown bursts (P4b)
constexpr uint64_t WATCHDOG_MIN_RESET_INTERVAL = 250;  // ms; rate-limit Stop-Endpoint (P4a)
#else
constexpr bool     GB10_TUNING_ENABLED         = false;
constexpr uint8_t  DEFAULT_USB_REQUEST_COUNT   = 2;    // upstream default (unchanged)
constexpr int      DEFAULT_STOP_SETTLE_MS      = 0;    // no settle  => identical to upstream
constexpr uint64_t WATCHDOG_MIN_RESET_INTERVAL = 0;    // 0 => watchdog_should_reset() always true
#endif

// H3 defaults. Unlike P2/P4 these have no per-profile upstream-vs-GB10 split (the
// wedge signature and a teardown budget are meaningful regardless of build), so
// they live OUTSIDE the RS2_GB10_USB_TUNING #if/#else and are always visible to
// callers and to the offline unit test (which compiles with the macro undefined).
// The live wiring that consumes them is still RS2_GB10_USB_TUNING-gated at the call
// site; upstream simply never calls these functions.
constexpr int    DEFAULT_WEDGE_TIMEOUT_THRESHOLD = 8;       // -110s within the window to call it wedged
constexpr double DEFAULT_WEDGE_WINDOW_MS         = 2000.0;  // sliding window for the burst
constexpr int    DEFAULT_TEARDOWN_BUDGET_MS      = 1500;    // bound a wedged join/drain (F6 path)

// Parse an optional decimal override; returns fallback unless env_value is a
// complete, valid integer (no trailing junk).
inline long parse_int_override( const char* env_value, long fallback )
{
    if( !env_value || !*env_value )
        return fallback;
    char* end = nullptr;
    const long v = std::strtol( env_value, &end, 10 );
    if( end == env_value || *end != '\0' )
        return fallback;
    return v;
}

// P2: resolve the per-stream URB pool depth.
//  - builtin_default: compile-time default (GB10 profile raises this for D400).
//  - env_value:       RS2_USB_REQUEST_COUNT override (experiments only).
// Invalid/empty env falls back to builtin_default; result clamped to [MIN,MAX].
inline uint8_t resolve_usb_request_count( uint8_t builtin_default, const char* env_value )
{
    long chosen = parse_int_override( env_value, builtin_default );
    if( chosen < MIN_USB_REQUEST_COUNT ) chosen = MIN_USB_REQUEST_COUNT;
    if( chosen > MAX_USB_REQUEST_COUNT ) chosen = MAX_USB_REQUEST_COUNT;
    return static_cast< uint8_t >( chosen );
}

// P3: advisory message when usbfs_memory_mb is below the recommended threshold.
// Returns an empty string when current_mb >= required_mb (no warning needed).
// The SDK only *advises* — it never writes the sysfs value (config stays
// inspectable / versioned).
inline std::string usbfs_memory_advice( long current_mb, long required_mb )
{
    if( current_mb >= required_mb )
        return std::string();
    std::ostringstream os;
    os << "usbcore usbfs_memory_mb is " << current_mb << " MB (recommend >= " << required_mb
       << " MB for D400 high-bandwidth multistream over the RSUSB backend). "
       << "Raise it via a config file: "
       << "echo 'options usbcore usbfs_memory_mb=1000' | sudo tee /etc/modprobe.d/99-realsense-usbfs.conf"
       << " ; runtime (no reboot): echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb";
    return os.str();
}

// P4: rate-limit the stall-watchdog endpoint reset. The watchdog issues a USB
// reset_endpoint (-> Stop Endpoint command) on a stalled bulk stream; under
// 3-stream load that can pile onto the control-command storm the GB10 xHCI dies
// on. Suppress resets that arrive faster than min_interval_ms. last_reset_ms==0
// means "never reset yet" -> always allow the first one.
inline bool watchdog_should_reset( uint64_t now_ms, uint64_t last_reset_ms, uint64_t min_interval_ms )
{
    if( last_reset_ms == 0 )
        return true;
    return ( now_ms - last_reset_ms ) >= min_interval_ms;
}

// P4: resolve the inter-stop settle delay (ms) inserted between consecutive
// stream stops / sensor starts to space out the cancel/clear_halt burst.
//  - env_value:       RS2_USB_STOP_SETTLE_MS override.
//  - builtin_default: compile-time default (GB10 profile sets a small delay).
// Invalid/empty env falls back to builtin_default; clamped to [MIN,MAX].
inline int resolve_stop_settle_ms( const char* env_value, int builtin_default )
{
    long chosen = parse_int_override( env_value, builtin_default );
    if( chosen < MIN_STOP_SETTLE_MS ) chosen = MIN_STOP_SETTLE_MS;
    if( chosen > MAX_STOP_SETTLE_MS ) chosen = MAX_STOP_SETTLE_MS;
    return static_cast< int >( chosen );
}

// P7: mid-session device re-acquire guard.
//
// Controller-death #2 was NOT a bad profile or a bad link (the requested profile
// resolved to a native one, and the link was USB-3.2). It was *churn*: an app that
// destroyed and recreated its rs2::context/pipeline between streams released the
// device back to the kernel uvcvideo driver, which immediately re-probed the UVC
// control endpoints. On the GB10 xHCI that control-probe storm provokes a
// Stop-Endpoint the controller cannot survive (HC died, reboot required).
//
// The safe pattern is to hold ONE context/pipeline for the process lifetime and
// reconfigure via rs2::config instead of tearing it down. This guard DETECTS a
// re-acquire of the SAME physical device after a full release -- the churn signature.
// Note it fires at re-acquire (construction), which is AFTER the prior release that
// already handed the device to uvcvideo; so on a single churn it detects rather than
// prevents. Its real value is (a) an advisory that steers the developer to session-stable
// ownership, and (b) halting a repeated stop/recreate LOOP early (REFUSE stops the next
// cycle from releasing again). It is purely a decision: the call site only logs (WARN)
// or throws (REFUSE); it never touches the kernel binding (auto-detaching uvcvideo is
// the contraindicated P1/P5 that wedges the controller). RSUSB-only: the V4L2 production
// backend never constructs usb_device_libusb and does not hit this path.
enum class reacquire_action
{
    allow,   // first acquire, or guard disabled -> proceed silently
    warn,    // GB10 re-acquire -> advisory log, proceed (default policy)
    refuse,  // GB10 re-acquire with explicit opt-in -> hard fail before EP0 traffic
};

// Is this acquisition the dangerous churn re-acquire? The RSUSB backend constructs a
// fresh usb_device_libusb PER SENSOR (depth, color, ...) within one legitimate session,
// all keyed by the device's USB bus-port id and all alive concurrently -- so "has it ever
// been constructed" cries wolf on the 2nd sensor. The lethal pattern (controller-death #2)
// is a FULL release -- every live handle for the device destroyed (live_count fell to 0)
// -- followed by a re-acquire. So it is dangerous only when there are zero live handles
// AND the device was acquired at least once before.
//  - live_count_before:     live usb_device_libusb instances for this device id, just
//                           before this construction (0 means fully released).
//  - total_acquired_before: how many times this device was successfully acquired before.
inline bool is_dangerous_reacquire( int live_count_before, int total_acquired_before )
{
    return live_count_before == 0 && total_acquired_before > 0;
}

// Decide what to do for an acquisition.
//  - dangerous_reacquire: from is_dangerous_reacquire().
//  - gb10_enabled: pass GB10_TUNING_ENABLED; false upstream makes this a no-op.
//  - refuse_opt_in: escalate warn->refuse only when the operator asks for it
//    (RS2_GB10_REFUSE_REACQUIRE) -- default stays advisory so a valid app is never
//    broken by the guard.
inline reacquire_action resolve_reacquire_action( bool dangerous_reacquire, bool gb10_enabled, bool refuse_opt_in )
{
    if( !gb10_enabled || !dangerous_reacquire )
        return reacquire_action::allow;
    return refuse_opt_in ? reacquire_action::refuse : reacquire_action::warn;
}

// Human-readable remediation for a dangerous re-acquire, naming the device and the
// fix (hold a single context). device_id is whatever stable id the call site has
// (USB bus-port path / unique id).
inline std::string reacquire_advice( const std::string& device_id, int prior_acquire_count )
{
    std::ostringstream os;
    os << "RealSense device " << device_id << " re-acquired (" << ( prior_acquire_count + 1 )
       << "x) within this process. On the GB10 xHCI this releases the device to the kernel "
       << "uvcvideo driver, which re-probes UVC control endpoints and can wedge the USB host "
       << "controller (controller-death #2). Hold a single rs2::context/pipeline for the "
       << "process lifetime and reconfigure via rs2::config instead of destroying and "
       << "recreating it. Set RS2_GB10_REFUSE_REACQUIRE=1 to hard-fail the re-acquire instead "
       << "of warning.";
    return os.str();
}

// H3: controller-wedge detector.
//
// DISTINCT FROM watchdog_should_reset (P4). The P4 watchdog only RATE-LIMITS the
// in-loop Stop-Endpoint reset issued on a frame-arrival stall (uvc-streamer.cpp); it
// assumes a reset can still help. H3 detects the failure mode where reset CANNOT help:
// the GB10 xHCI has begun returning a BURST of -110 (LIBUSB_ERROR_TIMEOUT) on transfer
// submissions/completions, which is the controller sliding toward host-controller death.
// At that point the correct action is to FAIL FAST -- abort the stream and surface an
// error -- so a wedged controller becomes a detectable failure instead of a hang, and so
// we do NOT pile another reset onto the dying control path.
//
// Pure decision: caller counts the -110s it observed and the width of the window they
// landed in; this returns whether that constitutes a wedge.
//  - minus110_count: number of -110/timeout transfer errors observed in the window.
//  - window_ms:      width of the observation window the count was measured over. A
//                    window narrower than MIN_WEDGE_WINDOW_MS is treated as not-yet-
//                    conclusive (inconclusive -> not wedging), so a single fast pair of
//                    timeouts at startup does not trip it; the count must accumulate
//                    across a real interval.
//  - threshold:      -110s required to call it wedged (clamped to [MIN,MAX]).
//  - window_limit_ms: the count only counts if it accrued within this much time
//                     (clamped to [MIN,MAX] window). A burst spread wider than this is
//                     sparse, not a wedge.
inline bool controller_wedging( int minus110_count,
                                double window_ms,
                                int threshold = DEFAULT_WEDGE_TIMEOUT_THRESHOLD,
                                double window_limit_ms = DEFAULT_WEDGE_WINDOW_MS )
{
    // Clamp tunables so an out-of-range caller cannot disable or over-trigger the guard.
    if( threshold < MIN_WEDGE_TIMEOUT_THRESHOLD ) threshold = MIN_WEDGE_TIMEOUT_THRESHOLD;
    if( threshold > MAX_WEDGE_TIMEOUT_THRESHOLD ) threshold = MAX_WEDGE_TIMEOUT_THRESHOLD;
    if( window_limit_ms < MIN_WEDGE_WINDOW_MS ) window_limit_ms = MIN_WEDGE_WINDOW_MS;
    if( window_limit_ms > MAX_WEDGE_WINDOW_MS ) window_limit_ms = MAX_WEDGE_WINDOW_MS;

    // A window narrower than the minimum is not yet a meaningful sample -> inconclusive.
    if( window_ms < MIN_WEDGE_WINDOW_MS )
        return false;
    // The burst must be CONCENTRATED: spread wider than the limit it is sparse, not a wedge.
    if( window_ms > window_limit_ms )
        return false;
    return minus110_count >= threshold;
}

// H3: stateful, clock-injected helper so the detector is deterministic in tests.
// The caller feeds wall/steady time in milliseconds (record_timeout(now_ms)); the tracker
// keeps a fixed-size ring of the most recent timeout timestamps and reports whether the
// most recent ones cluster into a wedge. No real clock is read here -> fully testable.
//
// Wiring: at the uvc-streamer USB error path (uvc-streamer.cpp:162-164, where
// submit_request returns a non-SUCCESS status; secondary site start() at 193-195),
// call record_timeout(now_ms) when the status maps to -110/timeout, then if is_wedging()
// abort the stream / surface the error instead of looping into another reset.
struct wedge_tracker
{
    // Capacity caps how far back we look; >= MAX threshold so a full wedge always fits.
    static const int CAPACITY = MAX_WEDGE_TIMEOUT_THRESHOLD;

    double  _ts[CAPACITY];   // ring of timeout timestamps (ms)
    int     _head;           // next write index
    int     _size;           // valid entries (<= CAPACITY)
    int     _threshold;
    double  _window_ms;

    explicit wedge_tracker( int threshold = DEFAULT_WEDGE_TIMEOUT_THRESHOLD,
                            double window_ms = DEFAULT_WEDGE_WINDOW_MS )
        : _head( 0 ), _size( 0 ), _threshold( threshold ), _window_ms( window_ms )
    {
        for( int i = 0; i < CAPACITY; ++i )
            _ts[i] = 0.0;
    }

    // Record a -110/timeout observed at now_ms (must be non-decreasing across calls).
    void record_timeout( double now_ms )
    {
        _ts[_head] = now_ms;
        _head = ( _head + 1 ) % CAPACITY;
        if( _size < CAPACITY )
            ++_size;
    }

    // Drop all recorded timeouts (call after a successful recovery / clean transfer).
    void reset()
    {
        _head = 0;
        _size = 0;
    }

    // How many recorded timeouts fall within _window_ms of now_ms.
    int count_in_window( double now_ms ) const
    {
        int n = 0;
        for( int i = 0; i < _size; ++i )
        {
            if( ( now_ms - _ts[i] ) <= _window_ms )
                ++n;
        }
        return n;
    }

    // Is the controller wedging as of now_ms? Single source of truth: delegates the
    // decision to the free controller_wedging() so the stateful and pure paths agree.
    //
    // The tracker does its OWN windowing in count_in_window(), so it passes the nominal
    // _window_ms (not the measured span of the cluster) to the free function. This is
    // deliberate: a maximally-DENSE burst -- threshold -110s all completing within a few
    // ms (exactly what a deep URB pool produces when the xHCI stalls) -- is the STRONGEST
    // wedge signal and must read as wedging. If we passed the measured span, that span
    // could fall below MIN_WEDGE_WINDOW_MS and controller_wedging()'s "inconclusive"
    // lower-bound guard would misclassify the worst case as benign. Passing _window_ms
    // (>= MIN by construction, == window_limit so never "sparse") neutralizes both window
    // guards for the tracker, reducing its decision to ">= threshold timeouts within
    // _window_ms" -- which is precisely what the tracker is for.
    bool is_wedging( double now_ms ) const
    {
        const int n = count_in_window( now_ms );
        if( n == 0 )
            return false;
        return controller_wedging( n, _window_ms, _threshold, _window_ms );
    }
};

// H3: teardown-deadline helper. Pairs with the F6 ~usb_context join guard
// (context-libusb.cpp:83): wrap the bounded wait around the event-thread join so a
// wedged controller surfaces as a detectable timeout instead of a permanent hang.
// Pure: the caller samples start_ms before the join and now_ms while polling; this
// only decides whether the budget has elapsed.
//  - start_ms:  timestamp captured when the teardown/join began.
//  - now_ms:    current timestamp while waiting.
//  - budget_ms: how long the join may take (clamped to [MIN,MAX]). budget_ms <= 0
//               means "no deadline" -> never exceeded (disabled / safe default), so an
//               upstream/0 build keeps the original unbounded-join behavior.
inline bool teardown_deadline_exceeded( double start_ms, double now_ms, int budget_ms )
{
    if( budget_ms < MIN_TEARDOWN_BUDGET_MS ) budget_ms = MIN_TEARDOWN_BUDGET_MS;
    if( budget_ms > MAX_TEARDOWN_BUDGET_MS ) budget_ms = MAX_TEARDOWN_BUDGET_MS;
    if( budget_ms <= 0 )
        return false; // disabled -> never declare a deadline breach
    return ( now_ms - start_ms ) >= static_cast< double >( budget_ms );
}

}} // namespace librealsense::usb_tuning
