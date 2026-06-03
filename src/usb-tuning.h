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

}} // namespace librealsense::usb_tuning
