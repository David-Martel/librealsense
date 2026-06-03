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

}} // namespace librealsense::usb_tuning
