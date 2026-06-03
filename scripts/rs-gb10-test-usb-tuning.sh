#!/usr/bin/env bash
# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
#
# rs-gb10-test-usb-tuning.sh — fast standalone gate for src/usb-tuning.h
#
# Compiles unit-tests/usb-tuning/standalone-check.cpp using ONLY g++ and the
# repo source tree (no SDK build, no Catch2, no hardware). Prints PASS or FAIL
# and exits 0 or 1 respectively.  Fully idempotent — safe to run repeatedly.
# Cleans its own temp artefacts on exit.
#
# Usage:
#   scripts/rs-gb10-test-usb-tuning.sh [-h]
#
# Options:
#   -h, --help    Print this help and exit.
#
# Environment:
#   CXX           C++ compiler to use (default: g++)
#   LRS_TEST_KEEP_TEMPS  Set to 1 to keep the temp build dir on exit (debugging)

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE'
Usage:
  scripts/rs-gb10-test-usb-tuning.sh [-h]

Fast standalone gate for src/usb-tuning.h.  Compiles
unit-tests/usb-tuning/standalone-check.cpp with g++ only (no SDK build,
no Catch2, no hardware) and verifies all P2/P3/P4 assertions.

Options:
  -h, --help    Print this help and exit.

Environment:
  CXX                  C++ compiler to use (default: g++)
  LRS_TEST_KEEP_TEMPS  Set to 1 to keep the temp build dir on exit (debugging)
USAGE
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        -h|--help) usage ;;
        *) echo "error: unknown argument '$arg'" >&2; usage ;;
    esac
done

# ---------------------------------------------------------------------------
# Locate the repo root (this script lives in <root>/scripts/)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SRC_FILE="$ROOT/unit-tests/usb-tuning/standalone-check.cpp"
HEADER="$ROOT/src/usb-tuning.h"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ ! -f "$SRC_FILE" ]]; then
    echo "FAIL: source not found: $SRC_FILE" >&2
    exit 1
fi

if [[ ! -f "$HEADER" ]]; then
    echo "FAIL: header not found: $HEADER" >&2
    exit 1
fi

CXX="${CXX:-g++}"
if ! command -v "$CXX" >/dev/null 2>&1; then
    echo "FAIL: C++ compiler not found: $CXX" >&2
    echo "      Install with: sudo apt-get install -y g++" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Temp build dir — cleaned on exit unless LRS_TEST_KEEP_TEMPS=1
# ---------------------------------------------------------------------------
TMPDIR_WORK="$(mktemp -d /tmp/rs-gb10-usb-tuning-XXXXXX)"

cleanup() {
    local keep="${LRS_TEST_KEEP_TEMPS:-0}"
    if [[ "$keep" == "1" ]]; then
        echo "note: keeping temp dir $TMPDIR_WORK (LRS_TEST_KEEP_TEMPS=1)"
    else
        rm -rf "$TMPDIR_WORK"
    fi
}
trap cleanup EXIT

BIN="$TMPDIR_WORK/usb-tuning-check"

# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------
echo "Compiling standalone-check.cpp ..."
if ! "$CXX" -std=c++14 -I "$ROOT" \
        -Wall -Wextra \
        "$SRC_FILE" \
        -o "$BIN" 2>&1; then
    echo ""
    echo "FAIL: compilation failed" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
echo "Running assertions ..."
OUTPUT="$("$BIN")"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
    echo "FAIL: binary exited with code $EXIT_CODE" >&2
    exit 1
fi

if [[ "$OUTPUT" != *"ALL_ASSERTIONS_PASSED"* ]]; then
    echo "FAIL: expected ALL_ASSERTIONS_PASSED in output, got: $OUTPUT" >&2
    exit 1
fi

echo ""
echo "PASS: all usb-tuning assertions passed"
exit 0
