// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

// Standalone assertion harness for src/usb-tuning.h.
// Does NOT require the full librealsense SDK build — uses a relative include so it
// compiles from anywhere with no -I flag:
//   g++ -std=c++14 unit-tests/usb-tuning/standalone-check.cpp -o /tmp/usb-tuning-check
//
// This file is compiled and executed by scripts/rs-gb10-test-usb-tuning.sh as a
// fast pre-merge gate: it proves the pure policy functions behave correctly without
// hardware, without Catch2, and without building anything else.
//
// Keep the assertions here in sync with unit-tests/usb-tuning/test-usb-tuning.cpp.

#include <cassert>
#include <cstdio>
#include <string>

#include "../../src/usb-tuning.h"

using namespace librealsense::usb_tuning;

int main()
{
    // P2: resolve_usb_request_count -- env override and clamping
    assert( resolve_usb_request_count( 2, nullptr ) == 2 );   // no override -> builtin default
    assert( resolve_usb_request_count( 4, "" ) == 4 );        // empty env -> builtin default
    assert( resolve_usb_request_count( 2, "8" ) == 8 );       // valid override
    assert( resolve_usb_request_count( 2, "1" ) == 2 );       // below floor -> clamp to MIN(2)
    assert( resolve_usb_request_count( 2, "999" ) == 16 );    // above ceiling -> clamp to MAX(16)
    assert( resolve_usb_request_count( 4, "abc" ) == 4 );     // non-numeric -> builtin default
    assert( resolve_usb_request_count( 4, "8x" ) == 4 );      // trailing junk -> builtin default

    // P3: usbfs_memory_advice -- warns only below threshold
    assert( usbfs_memory_advice( 1000, 256 ).empty() );        // ample headroom -> silent
    assert( usbfs_memory_advice( 256, 256 ).empty() );         // exactly at threshold -> silent
    {
        std::string msg = usbfs_memory_advice( 16, 256 );
        assert( !msg.empty() );                                 // default 16 MB -> advise
        assert( msg.find( "usbfs_memory_mb" ) != std::string::npos ); // names the knob to change
    }

    // P4: watchdog_should_reset -- rate limiting
    assert( watchdog_should_reset( 1000, 0, 500 ) == true );    // never reset before -> allow
    assert( watchdog_should_reset( 1000, 900, 500 ) == false ); // only 100ms elapsed -> suppress
    assert( watchdog_should_reset( 1000, 400, 500 ) == true );  // 600ms elapsed -> allow
    assert( watchdog_should_reset( 1000, 500, 500 ) == true );  // exactly the interval -> allow

    // P4: resolve_stop_settle_ms -- env override and clamping
    assert( resolve_stop_settle_ms( nullptr, 50 ) == 50 );    // no override -> builtin default
    assert( resolve_stop_settle_ms( "100", 50 ) == 100 );     // valid override
    assert( resolve_stop_settle_ms( "-5", 50 ) == 0 );        // below floor -> clamp to MIN(0)
    assert( resolve_stop_settle_ms( "5000", 50 ) == 1000 );   // above ceiling -> clamp to MAX(1000)
    assert( resolve_stop_settle_ms( "x", 50 ) == 50 );        // non-numeric -> builtin default

    // P7: is_dangerous_reacquire -- (live_count_before, total_acquired_before)
    assert( is_dangerous_reacquire( 0, 0 ) == false );  // first ever acquire of this device
    assert( is_dangerous_reacquire( 1, 1 ) == false );  // 2nd concurrent sensor, prior still live
    assert( is_dangerous_reacquire( 2, 3 ) == false );  // more concurrent live handles -> normal session
    assert( is_dangerous_reacquire( 0, 1 ) == true );   // fully released then acquired again -> churn
    assert( is_dangerous_reacquire( 0, 5 ) == true );   // repeated churn cycles

    // P7: resolve_reacquire_action -- (dangerous_reacquire, gb10_enabled, refuse_opt_in)
    assert( resolve_reacquire_action( false, true,  false ) == reacquire_action::allow );  // not dangerous -> allow
    assert( resolve_reacquire_action( false, true,  true  ) == reacquire_action::allow );  // not dangerous -> allow
    assert( resolve_reacquire_action( true,  false, false ) == reacquire_action::allow );  // upstream disabled -> no-op
    assert( resolve_reacquire_action( true,  false, true  ) == reacquire_action::allow );  // upstream disabled -> no-op
    assert( resolve_reacquire_action( true,  true,  false ) == reacquire_action::warn );   // dangerous -> advisory warn
    assert( resolve_reacquire_action( true,  true,  true  ) == reacquire_action::refuse ); // refuse opt-in -> hard fail

    // P7: reacquire_advice -- names the device and the remediation
    {
        std::string msg = reacquire_advice( "2-1", 2 );
        assert( !msg.empty() );
        assert( msg.find( "2-1" ) != std::string::npos );       // names the offending device
        assert( msg.find( "context" ) != std::string::npos );   // tells the caller to hold one context
    }

    printf( "ALL_ASSERTIONS_PASSED\n" );
    return 0;
}
