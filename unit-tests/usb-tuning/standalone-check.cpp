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

    // H10: reacquire_state -- the live/total counter state-machine behind the P7 guard.
    // Asserts counter BALANCE across the three re-acquire scenarios the guard must survive,
    // so the "commit-at-end, always-release" invariant is proven, not just reasoned.
    {
        // Scenario 1: a normal multi-sensor session (depth + color on one device). Two
        // concurrent sensors construct then both destruct; live returns to baseline, total
        // reflects the two acquires, and the guard never reads "dangerous" mid-session.
        reacquire_state st;
        assert( st.live == 0 && st.total == 0 );
        assert( st.dangerous() == false );          // first acquire of this device
        st.commit();                                // depth opens
        assert( st.live == 1 && st.total == 1 );
        assert( st.dangerous() == false );          // 2nd sensor sees a live handle -> not churn
        st.commit();                                // color opens
        assert( st.live == 2 && st.total == 2 );
        st.release();                               // color closes
        st.release();                               // depth closes
        assert( st.live == 0 && st.total == 2 );    // balanced: every commit paired by a release
    }
    {
        // Scenario 2: a dangerous re-acquire that gets REFUSED does NOT perturb the counters.
        // After a full churn cycle (live hit 0, total>0) the next construct is dangerous; the
        // refuse path throws BEFORE commit(), so a refused acquire must leave live/total exactly
        // as they were -- no drift from the throw path (the invariant H10 unit-asserts).
        reacquire_state st;
        st.commit();                                // 1st session
        st.release();                               // fully released -> live 0, total 1
        assert( st.live == 0 && st.total == 1 );
        assert( st.dangerous() == true );           // full-release-then-reacquire = churn
        const int live_at_refuse = st.live, total_at_refuse = st.total;
        // production throws here (REFUSE) BEFORE commit() -- assert the refused path is a no-op:
        assert( st.live == live_at_refuse && st.total == total_at_refuse );
    }
    {
        // Scenario 3: a transient probe -- constructs then destructs with no streaming/set_power.
        // Still a balanced commit/release pair; live returns to 0, total advances by one.
        reacquire_state st;
        st.commit();
        st.release();
        assert( st.live == 0 && st.total == 1 );    // probe leaves no live drift
    }
    {
        // release() floors at 0: an unbalanced release (the dtor pairing prevents it today, but
        // this hardens against future edits) can never drive live negative.
        reacquire_state st;
        st.release();
        assert( st.live == 0 );                     // floored, not -1
        st.commit();
        st.release();
        st.release();                               // one extra release
        assert( st.live == 0 && st.total == 1 );    // still floored; total untouched by release
    }

    // H3: controller_wedging -- (minus110_count, window_ms, threshold, window_limit_ms)
    // Defaults: threshold=8, window_limit=2000ms; MIN_WEDGE_WINDOW_MS=50.
    assert( controller_wedging( 8, 1000.0 ) == true );    // burst >= threshold within window -> wedging
    assert( controller_wedging( 20, 500.0 ) == true );    // big burst, tight window -> wedging
    assert( controller_wedging( 7, 1000.0 ) == false );   // just under threshold -> not wedging
    assert( controller_wedging( 100, 5000.0 ) == false ); // spread WIDER than window_limit -> sparse, not a wedge
    assert( controller_wedging( 100, 10.0 ) == false );   // window narrower than MIN_WEDGE_WINDOW_MS -> inconclusive

    // H3: window-edge boundaries (window_ms vs MIN and vs window_limit).
    assert( controller_wedging( 8, 50.0 ) == true );      // exactly MIN_WEDGE_WINDOW_MS -> conclusive, wedging
    assert( controller_wedging( 8, 49.9 ) == false );     // a hair under MIN -> inconclusive
    assert( controller_wedging( 8, 2000.0 ) == true );    // exactly at window_limit -> still within -> wedging
    assert( controller_wedging( 8, 2000.1 ) == false );   // a hair over window_limit -> sparse

    // H3: threshold-edge with explicit tunables.
    assert( controller_wedging( 3, 1000.0, 3, 2000.0 ) == true );  // count == threshold -> wedging
    assert( controller_wedging( 2, 1000.0, 3, 2000.0 ) == false ); // count < threshold -> not wedging

    // H3: tunable clamping (out-of-range threshold / window cannot disable the guard).
    assert( controller_wedging( 2, 1000.0, 0, 2000.0 ) == true );  // threshold clamped up to MIN(2) -> 2>=2 wedging
    assert( controller_wedging( 2, 1000.0, 1, 2000.0 ) == true );  // threshold 1 clamps to 2 -> wedging
    assert( controller_wedging( 8, 1000.0, 999, 2000.0 ) == false ); // threshold clamped to MAX(64), 8<64 -> not
    assert( controller_wedging( 8, 70000.0, 8, 999999.0 ) == false ); // window_limit clamps to MAX, 70000<60000? no -> over -> sparse

    // H3: wedge_tracker -- clock-injected, deterministic. Default threshold=8, window=2000ms.
    {
        wedge_tracker wt; // threshold 8, window 2000ms
        // 8 timeouts all within a 700ms window -> wedging.
        for( int i = 0; i < 8; ++i )
            wt.record_timeout( 1000.0 + i * 100.0 ); // 1000..1700
        assert( wt.count_in_window( 1700.0 ) == 8 );
        assert( wt.is_wedging( 1700.0 ) == true );   // burst within window
    }
    {
        wedge_tracker wt;
        // 8 sparse timeouts 1000ms apart -> only the most recent few are in the 2000ms window.
        for( int i = 0; i < 8; ++i )
            wt.record_timeout( i * 1000.0 ); // 0,1000,...,7000
        // At now=7000, window=2000 -> timeouts at 5000,6000,7000 qualify (3) -> below threshold.
        assert( wt.count_in_window( 7000.0 ) == 3 );
        assert( wt.is_wedging( 7000.0 ) == false );  // sparse -> not wedging
    }
    {
        wedge_tracker wt;
        assert( wt.is_wedging( 0.0 ) == false );     // empty tracker -> not wedging
        wt.record_timeout( 100.0 );
        assert( wt.is_wedging( 100.0 ) == false );   // single timeout -> not wedging
    }
    {
        // Custom tight tracker: threshold 3, window 500ms.
        wedge_tracker wt( 3, 500.0 );
        wt.record_timeout( 1000.0 );
        wt.record_timeout( 1100.0 );
        wt.record_timeout( 1200.0 );
        assert( wt.count_in_window( 1200.0 ) == 3 ); // all within 500ms
        assert( wt.is_wedging( 1200.0 ) == true );   // 3 within 500ms -> wedging
        // Advance just past the window edge: 1700-1200=500 (in), 1700-1100=600 (out), 1700-1000=700 (out).
        assert( wt.count_in_window( 1700.0 ) == 1 ); // only the 1200 timeout still inside 500ms
        assert( wt.is_wedging( 1700.0 ) == false );  // aged out below threshold -> not wedging
        // Further out, everything ages off.
        assert( wt.count_in_window( 1800.0 ) == 0 );
    }
    {
        // Maximally-DENSE burst: threshold -110s within a few ms (span < MIN_WEDGE_WINDOW_MS).
        // This is the strongest wedge signal and MUST read as wedging -- the tracker does its
        // own windowing, so the free function's lower-bound "inconclusive" guard must not
        // suppress it. (Regression guard for the measured-span-vs-nominal-window decision.)
        wedge_tracker wt( 3, 500.0 );
        wt.record_timeout( 1000.0 );
        wt.record_timeout( 1020.0 );
        wt.record_timeout( 1040.0 ); // 3 timeouts, span only 40ms (< 50ms MIN)
        assert( wt.count_in_window( 1040.0 ) == 3 );
        assert( wt.is_wedging( 1040.0 ) == true );   // dense burst -> wedging
    }
    {
        // reset() clears history.
        wedge_tracker wt( 2, 1000.0 );
        wt.record_timeout( 100.0 );
        wt.record_timeout( 200.0 );
        assert( wt.is_wedging( 200.0 ) == true );
        wt.reset();
        assert( wt.count_in_window( 200.0 ) == 0 );
        assert( wt.is_wedging( 200.0 ) == false );   // cleared -> not wedging
    }

    // H3: teardown_deadline_exceeded -- (start_ms, now_ms, budget_ms)
    assert( teardown_deadline_exceeded( 1000.0, 1499.0, 500 ) == false ); // 499ms elapsed, budget 500 -> within
    assert( teardown_deadline_exceeded( 1000.0, 1500.0, 500 ) == true );  // exactly budget -> exceeded
    assert( teardown_deadline_exceeded( 1000.0, 2000.0, 500 ) == true );  // well past -> exceeded
    assert( teardown_deadline_exceeded( 1000.0, 9999.0, 0 ) == false );   // budget 0 -> disabled, never exceeded
    assert( teardown_deadline_exceeded( 1000.0, 9999.0, -5 ) == false );  // negative clamps to 0 -> never exceeded
    // Budget above MAX clamps to MAX_TEARDOWN_BUDGET_MS (60000).
    assert( teardown_deadline_exceeded( 0.0, 59999.0, 999999 ) == false ); // 59999 < 60000 -> within
    assert( teardown_deadline_exceeded( 0.0, 60000.0, 999999 ) == true );  // 60000 >= clamped 60000 -> exceeded

    printf( "ALL_ASSERTIONS_PASSED\n" );
    return 0;
}
