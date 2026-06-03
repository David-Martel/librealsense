// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

//#cmake:add-file ../../src/usb-tuning.h

// Unit tests for the RSUSB tuning helpers (src/usb-tuning.h) that back the GB10
// DGX Spark multistream mitigations:
//   P2 - configurable URB pool depth (resolve_usb_request_count)
//   P3 - usbfs_memory_mb preflight advisory (usbfs_memory_advice)
//   P4 - gentler stop: watchdog reset rate-limit + inter-stop settle delay
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
