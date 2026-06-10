// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

//#cmake:add-file ../../src/usb-tuning.h

// Unit tests for the RSUSB tuning helpers (src/usb-tuning.h) that back the GB10
// DGX Spark multistream mitigations:
//   P2 - configurable URB pool depth (resolve_usb_request_count)
//   P3 - usbfs_memory_mb preflight advisory (usbfs_memory_advice)
//   P4 - gentler stop: watchdog reset rate-limit + inter-stop settle delay
//   P7 - device re-acquire guard (resolve_reacquire_action / reacquire_advice)
// These are pure functions so the policy is verified without hardware.

#include "../catch.h"
#include "../../src/usb-tuning.h"

using namespace librealsense::usb_tuning;

TEST_CASE( "resolve_usb_request_count: env override and clamping", "[usb-tuning][P2]" )
{
    REQUIRE( resolve_usb_request_count( 2, nullptr ) == 2 );   // no override -> builtin default
    REQUIRE( resolve_usb_request_count( 4, "" ) == 4 );        // empty env -> builtin default
    REQUIRE( resolve_usb_request_count( 2, "8" ) == 8 );       // valid override
    REQUIRE( resolve_usb_request_count( 2, "1" ) == 2 );       // below floor -> clamp to MIN(2)
    REQUIRE( resolve_usb_request_count( 2, "999" ) == 16 );    // above ceiling -> clamp to MAX(16)
    REQUIRE( resolve_usb_request_count( 4, "abc" ) == 4 );     // non-numeric -> builtin default
    REQUIRE( resolve_usb_request_count( 4, "8x" ) == 4 );      // trailing junk -> builtin default
}

TEST_CASE( "usbfs_memory_advice: warns only below threshold", "[usb-tuning][P3]" )
{
    REQUIRE( usbfs_memory_advice( 1000, 256 ).empty() );       // ample headroom -> silent
    REQUIRE( usbfs_memory_advice( 256, 256 ).empty() );        // exactly at threshold -> silent
    auto msg = usbfs_memory_advice( 16, 256 );                 // default 16MB -> advise
    REQUIRE_FALSE( msg.empty() );
    REQUIRE( msg.find( "usbfs_memory_mb" ) != std::string::npos ); // names the knob to change
}

TEST_CASE( "watchdog_should_reset: rate limiting", "[usb-tuning][P4]" )
{
    REQUIRE( watchdog_should_reset( 1000, 0, 500 ) == true );    // never reset before -> allow
    REQUIRE( watchdog_should_reset( 1000, 900, 500 ) == false ); // only 100ms elapsed -> suppress
    REQUIRE( watchdog_should_reset( 1000, 400, 500 ) == true );  // 600ms elapsed -> allow
    REQUIRE( watchdog_should_reset( 1000, 500, 500 ) == true );  // exactly the interval -> allow
}

TEST_CASE( "resolve_stop_settle_ms: env override and clamping", "[usb-tuning][P4]" )
{
    REQUIRE( resolve_stop_settle_ms( nullptr, 50 ) == 50 );    // no override -> builtin default
    REQUIRE( resolve_stop_settle_ms( "100", 50 ) == 100 );     // valid override
    REQUIRE( resolve_stop_settle_ms( "-5", 50 ) == 0 );        // below floor -> clamp to MIN(0)
    REQUIRE( resolve_stop_settle_ms( "5000", 50 ) == 1000 );   // above ceiling -> clamp to MAX(1000)
    REQUIRE( resolve_stop_settle_ms( "x", 50 ) == 50 );        // non-numeric -> builtin default
}

TEST_CASE( "is_dangerous_reacquire: only a full-release-then-reacquire is dangerous", "[usb-tuning][P7]" )
{
    // (live_count_before, total_acquired_before). usb_device_libusb is constructed once
    // PER SENSOR (depth, color, ...) within a single legitimate session, all sharing the
    // device's USB bus-port id and all alive at once, so a plain "constructed before"
    // count would cry wolf on the 2nd sensor. The churn that killed the controller is a
    // FULL release (every handle destroyed -> live_count hit 0) followed by a re-acquire.
    REQUIRE( is_dangerous_reacquire( 0, 0 ) == false );  // first ever acquire of this device
    REQUIRE( is_dangerous_reacquire( 1, 1 ) == false );  // a 2nd concurrent sensor, prior one still live
    REQUIRE( is_dangerous_reacquire( 2, 3 ) == false );  // more concurrent live handles -> normal session
    REQUIRE( is_dangerous_reacquire( 0, 1 ) == true );   // fully released (0 live) then acquired again -> churn
    REQUIRE( is_dangerous_reacquire( 0, 5 ) == true );   // repeated churn cycles
}

TEST_CASE( "resolve_reacquire_action: non-dangerous acquisitions are always allowed", "[usb-tuning][P7]" )
{
    // First acquire and concurrent-sensor acquires (dangerous_reacquire == false) never fire.
    REQUIRE( resolve_reacquire_action( false, true,  false ) == reacquire_action::allow );
    REQUIRE( resolve_reacquire_action( false, true,  true  ) == reacquire_action::allow );
    REQUIRE( resolve_reacquire_action( false, false, false ) == reacquire_action::allow );
}

TEST_CASE( "resolve_reacquire_action: disabled upstream is a no-op", "[usb-tuning][P7]" )
{
    // With GB10 tuning off (stock upstream) the guard must never fire, even on a real
    // dangerous re-acquire and even if the refuse flag is somehow set.
    REQUIRE( resolve_reacquire_action( true, false, false ) == reacquire_action::allow );
    REQUIRE( resolve_reacquire_action( true, false, true  ) == reacquire_action::allow );
}

TEST_CASE( "resolve_reacquire_action: dangerous GB10 re-acquire warns by default", "[usb-tuning][P7]" )
{
    // A dangerous re-acquire on GB10 means the device was fully released back to kernel
    // uvcvideo (which re-probes control endpoints -> controller-death #2 churn). Default
    // policy is advisory WARN (never breaks a valid app).
    REQUIRE( resolve_reacquire_action( true, true, false ) == reacquire_action::warn );
}

TEST_CASE( "resolve_reacquire_action: refuse opt-in hard-fails the re-acquire", "[usb-tuning][P7]" )
{
    // Only when the operator explicitly opts in (RS2_GB10_REFUSE_REACQUIRE) does a
    // dangerous GB10 re-acquire escalate from WARN to REFUSE.
    REQUIRE( resolve_reacquire_action( true, true, true ) == reacquire_action::refuse );
}

TEST_CASE( "reacquire_advice: names the device and the remediation", "[usb-tuning][P7]" )
{
    auto msg = reacquire_advice( "2-1", 2 );
    REQUIRE_FALSE( msg.empty() );
    REQUIRE( msg.find( "2-1" ) != std::string::npos );          // names the offending device
    REQUIRE( msg.find( "context" ) != std::string::npos );      // tells the caller to hold one context
}

TEST_CASE( "reacquire_state: counters stay balanced across a normal multi-sensor session", "[usb-tuning][P7][H10]" )
{
    // depth + color on one device: two concurrent sensors construct then both destruct.
    // live returns to baseline, total reflects the two acquires, never dangerous mid-session.
    reacquire_state st;
    REQUIRE( st.live == 0 );
    REQUIRE( st.total == 0 );
    REQUIRE( st.dangerous() == false );          // first acquire of this device
    st.commit();                                 // depth opens
    REQUIRE( st.live == 1 );
    REQUIRE( st.total == 1 );
    REQUIRE( st.dangerous() == false );          // 2nd sensor sees a live handle -> not churn
    st.commit();                                 // color opens
    REQUIRE( st.live == 2 );
    REQUIRE( st.total == 2 );
    st.release();                                // color closes
    st.release();                                // depth closes
    REQUIRE( st.live == 0 );                     // balanced: every commit paired by a release
    REQUIRE( st.total == 2 );
}

TEST_CASE( "reacquire_state: a refused dangerous re-acquire does not perturb the counters", "[usb-tuning][P7][H10]" )
{
    // After a full churn cycle (live hit 0, total>0) the next construct is dangerous; the refuse
    // path throws BEFORE commit(), so a refused acquire must leave live/total exactly as they were.
    reacquire_state st;
    st.commit();
    st.release();                                // fully released -> live 0, total 1
    REQUIRE( st.live == 0 );
    REQUIRE( st.total == 1 );
    REQUIRE( st.dangerous() == true );           // full-release-then-reacquire = churn
    const int live_at_refuse = st.live, total_at_refuse = st.total;
    // production throws here (REFUSE) BEFORE commit() -- the refused path is a no-op on the counters:
    REQUIRE( st.live == live_at_refuse );
    REQUIRE( st.total == total_at_refuse );
}

TEST_CASE( "reacquire_state: a transient probe leaves no live drift", "[usb-tuning][P7][H10]" )
{
    // Constructs then destructs with no streaming/set_power -- still a balanced commit/release pair.
    reacquire_state st;
    st.commit();
    st.release();
    REQUIRE( st.live == 0 );                     // probe leaves no live drift
    REQUIRE( st.total == 1 );                    // but the acquire is counted (future churn detection)
}

TEST_CASE( "reacquire_state: release() floors live at zero", "[usb-tuning][P7][H10]" )
{
    // An unbalanced release (the ctor/dtor pairing prevents it today, but this hardens against
    // future edits) can never drive live negative -- which would defeat dangerous() detection.
    reacquire_state st;
    st.release();
    REQUIRE( st.live == 0 );                     // floored, not -1
    st.commit();
    st.release();
    st.release();                                // one extra release
    REQUIRE( st.live == 0 );                     // still floored
    REQUIRE( st.total == 1 );                    // total untouched by release
}
