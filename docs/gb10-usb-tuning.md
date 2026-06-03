# GB10 USB Tuning — DGX Spark / NVIDIA Orin xHCI Mitigations

## Background

The NVIDIA DGX Spark (GB10 SoC) hosts its USB 3.x ports behind a single Synopsys
xHCI controller.  Running three simultaneous saturating bulk streams (D400
depth + IR + colour at high FPS) generates a dense burst of control-path
transactions (Stop Endpoint, Clear Halt, Reset Endpoint) that can starve the
controller and cause an unrecoverable xHCI controller death.

`src/usb-tuning.h` exposes four pure policy functions that let the RSUSB
back-end adopt a safer operating envelope.  They are hardware-free so they can
be unit-tested without a camera.

---

## The Four Tunables

### P2 — URB Pool Depth (`RS2_USB_REQUEST_COUNT`)

**Function**: `resolve_usb_request_count(uint8_t builtin_default, const char* env_value)`

Each bulk streaming endpoint gets a pool of pre-allocated USB Request Blocks
(URBs).  A deeper pool thickens the pipeline and reduces the frequency of
`USBDEVFS_SUBMITURB` ioctl bursts, but costs pinned `usbfs` memory.

| Parameter | Upstream default | GB10 default (`RS2_GB10_USB_TUNING=1`) |
|-----------|-----------------|----------------------------------------|
| URB count | 2               | 4 (recommended; set via builtin_default at call site) |
| Minimum   | 2               | 2 (hard floor, always) |
| Maximum   | 16              | 16 (hard ceiling, always) |

Override at runtime (experiments only — **not for production**):
```
export RS2_USB_REQUEST_COUNT=8
```

The env value is validated strictly: non-numeric or trailing-junk values fall
back to `builtin_default`; values outside [2, 16] are clamped silently.

---

### P3 — usbfs Memory Advisory (`usbfs_memory_mb`)

**Function**: `usbfs_memory_advice(long current_mb, long required_mb)`

Linux limits the total pinned memory used by `usbfs` via
`/sys/module/usbcore/parameters/usbfs_memory_mb` (kernel default: 16 MB).
With a deeper URB pool and three concurrent streams this budget is easily
exhausted, causing `USBDEVFS_SUBMITURB` to return `ENOMEM`.

The SDK **never writes** to sysfs — it only emits an advisory log message when
the current limit is below the recommended threshold.

| Parameter | Upstream default | GB10 recommended |
|-----------|-----------------|-----------------|
| usbfs_memory_mb | 16 MB      | 1000 MB         |

Raise it persistently (survives reboots):
```
echo 'options usbcore usbfs_memory_mb=1000' | \
    sudo tee /etc/modprobe.d/99-realsense-usbfs.conf
```

Raise it at runtime (no reboot required):
```
echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb
```

`usbfs_memory_advice()` returns an empty string when `current_mb >= required_mb`
and a human-readable advisory string otherwise.  The advisory always contains
the literal text `usbfs_memory_mb` so it can be grepped in logs.

---

### P4 — Watchdog Reset Rate-Limit (`RS2_USB_STOP_SETTLE_MS`)

**Function A**: `watchdog_should_reset(uint64_t now_ms, uint64_t last_reset_ms, uint64_t min_interval_ms)`

The stall-watchdog may issue a `USBDEVFS_RESETEP` / Stop Endpoint command when
a bulk endpoint stalls.  Under 3-stream load on the GB10, rapid-fire resets
pile onto the control-path storm.  `watchdog_should_reset()` suppresses resets
that arrive faster than `min_interval_ms`.

- Returns `true` (allow reset) when `last_reset_ms == 0` (first ever reset).
- Returns `true` when `now_ms - last_reset_ms >= min_interval_ms`.
- Returns `false` (suppress) otherwise.

**Function B**: `resolve_stop_settle_ms(const char* env_value, int builtin_default)`

Inserts a short delay between consecutive stream stops to space out the
cancel/clear_halt burst.

| Parameter | Upstream default | GB10 default (`RS2_GB10_USB_TUNING=1`) |
|-----------|-----------------|----------------------------------------|
| Settle delay | 0 ms         | 50 ms (`usb_tuning::DEFAULT_STOP_SETTLE_MS`) |
| Minimum   | 0 ms            | 0 ms (hard floor) |
| Maximum   | 1000 ms         | 1000 ms (hard ceiling) |

The compile-time GB10 defaults (URB count 4, settle 50 ms, watchdog interval 250 ms)
are defined once in `src/usb-tuning.h` (`DEFAULT_USB_REQUEST_COUNT`,
`DEFAULT_STOP_SETTLE_MS`, `WATCHDOG_MIN_RESET_INTERVAL`) and selected by the
`RS2_GB10_USB_TUNING` macro. The runtime env overrides (`RS2_USB_REQUEST_COUNT`,
`RS2_USB_STOP_SETTLE_MS`) and the P3 usbfs advisory are themselves gated behind that
macro, so a build without it is byte-identical to upstream (no env reads, no sysfs read).

Override at runtime:
```
export RS2_USB_STOP_SETTLE_MS=50
```

---

## Build Flags

`RS2_GB10_USB_TUNING` is a compile-time integer flag (0 or 1).  Call sites in
`src/uvc/` and `src/libusb/` read it to select between the conservative
upstream `builtin_default` values and the raised GB10 defaults.

| Flag value | Behaviour |
|-----------|-----------|
| `RS2_GB10_USB_TUNING=0` (or undefined) | Upstream defaults |
| `RS2_GB10_USB_TUNING=1` | GB10 mitigations default-ON |

The GB10 build script passes this automatically:

```
scripts/build-dgx-spark-gb10.sh          # RS2_GB10_USB_TUNING=1 (default)
LRS_GB10_USB_TUNING=0 scripts/build-dgx-spark-gb10.sh  # vanilla build
```

---

## Running Tests

### Fast standalone gate (no SDK build required)

```bash
bash scripts/rs-gb10-test-usb-tuning.sh
```

Compiles `unit-tests/usb-tuning/standalone-check.cpp` with `g++ -std=c++14
-I <repo-root>` — no Catch2, no hardware, no full SDK build.  Exits 0 on
PASS, 1 on FAIL.  Safe to run repeatedly (idempotent, cleans temp artefacts).

Expected output:
```
Compiling standalone-check.cpp ...
Running assertions ...

PASS: all usb-tuning assertions passed
```

### Full Catch2 unit-test suite

The test lives at `unit-tests/usb-tuning/test-usb-tuning.cpp` and is
auto-discovered by `unit-test-config.py` (the script globs for `test-*.cpp`
recursively under `unit-tests/`).

Build with tests enabled:

```bash
cmake -S . -B build \
    -DBUILD_UNIT_TESTS=ON \
    -DBUILD_EASYLOGGINGPP=ON \
    -DRS2_GB10_USB_TUNING=1
cmake --build build --target test-usb-tuning-usb-tuning
```

Or via the GB10 build script:

```bash
LRS_GB10_BUILD_UNIT_TESTS=ON scripts/build-dgx-spark-gb10.sh
```

Run the binary:
```bash
./build/<cmake-build-dir>/unit-tests/usb-tuning/test-usb-tuning-usb-tuning
```

Run via the Python harness:
```bash
python3 unit-tests/run-unit-tests.py --tag usb-tuning
```

---

## Source Reference

| File | Role |
|------|------|
| `src/usb-tuning.h` | All four pure policy functions |
| `unit-tests/usb-tuning/test-usb-tuning.cpp` | Catch2 test (full suite, SDK build) |
| `unit-tests/usb-tuning/standalone-check.cpp` | `assert`-only harness (fast gate) |
| `scripts/rs-gb10-test-usb-tuning.sh` | Fast-gate runner script |
| `scripts/build-dgx-spark-gb10.sh` | Full GB10 SDK build with tuning enabled |
| `scripts/99-dgx-spark-usbcore.conf` | modprobe config for usbfs_memory_mb |
