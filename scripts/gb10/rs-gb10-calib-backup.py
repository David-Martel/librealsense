#!/usr/bin/env python3
"""Dump a D400's depth (table 25) and RGB (table 32) calibration tables. READ ONLY.

WHY THIS EXISTS. `run_on_chip_calibration` / `run_tare_calibration` /
`run_uv_map_calibration` are two calls away from permanent:
`set_calibration_table()` loads a candidate into VOLATILE RAM, and
`write_calibration()` BURNS FLASH. `reset_to_factory_calibration()` exists, but
it restores the factory tables for depth AND colour together -- so it also
discards any good calibration you had. A saved table is the only route back to a
SPECIFIC prior state, and the only moment to take it is before anyone
experiments, not after.

Nothing here writes to the device. Both reads are GETINTCAL (0x15), which has no
write path -- but they do claim a device handle, so run it while the camera is
idle rather than mid-capture.

TWO THINGS THE SUPPORTED API WILL NOT DO FOR YOU:

1. `get_calibration_table()` takes no table id and never will -- it is hardcoded
   to `coefficients_table_id` (d400-auto-calibration.cpp). Depth only. The RGB
   table needs the raw `debug_protocol` path.
2. `hw_monitor::send()` strips the leading opcode/return word; the raw path does
   NOT. Hence the `[4:]` on the RGB read. Get that wrong and you parse the
   return code as a table header.

`table_header` is **16** bytes: uint16 version (big-endian) + uint16 table_type
+ uint32 table_size + uint32 param + uint32 crc32, all naturally aligned so no
padding. `table_size` counts the PAYLOAD only, excluding this header.

The CRC is standard CRC-32 (IEEE 802.3): rsutils::number::calc_crc32 uses init
0xFFFFFFFF, the 0xedb88320 table, and a final complement -- byte-for-byte
equivalent to zlib.crc32, which is why validating here is meaningful rather than
decorative. A dump that fails CRC is not a backup; it is a file someone will
trust later.

WHAT THIS DOES NOT COVER. Seven calibration tables exist on a D400; this saves
the two that OCC, tare and UV-map write. It is not a full EEPROM image, and
anything that touched another table is not recoverable from it.
"""

import datetime
import hashlib
import os
import struct
import sys
import zlib

import pyrealsense2 as rs

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"

devs = list(rs.context().query_devices())
if not devs:
    sys.exit("REFUSING: no RealSense device present")
dev = devs[0]
sn = dev.get_info(rs.camera_info.serial_number)
fw = dev.get_info(rs.camera_info.firmware_version)
host = os.uname().nodename
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
print(f"host={host} serial={sn} fw={fw}")


def split_and_check(raw, expect_type, label):
    """Validate header + CRC. librealsense's calc_crc32 is standard CRC-32
    (init 0xFFFFFFFF, poly table 0xedb88320, final complement) == zlib.crc32."""
    if len(raw) < 16:
        raise SystemExit(
            f"REFUSING: {label} shorter than a table_header ({len(raw)} B)"
        )
    _ver, ttype, tsize, _param, crc = struct.unpack("<HHIII", raw[:16])
    if ttype != expect_type:
        raise SystemExit(
            f"REFUSING: {label} table_type={ttype}, expected {expect_type}"
        )
    payload = raw[16 : 16 + tsize]
    if len(payload) != tsize:
        raise SystemExit(f"REFUSING: {label} truncated: {len(payload)} of {tsize} B")
    got = zlib.crc32(payload) & 0xFFFFFFFF
    if got != crc:
        raise SystemExit(
            f"REFUSING: {label} CRC MISMATCH stored={crc:#010x} computed={got:#010x}"
        )
    print(f"  {label}: type={ttype} payload={tsize}B crc={crc:#010x} OK")
    return raw[:16] + payload


results = {}

cal = rs.auto_calibrated_device(dev)
results["depth_t25"] = split_and_check(
    bytes(cal.get_calibration_table()), 25, "depth (t25)"
)

# RGB has no public getter; GETINTCAL param1=32 via the raw path, which unlike
# hw_monitor::send does NOT strip the leading opcode/return word -- hence [4:].
try:
    dp = rs.debug_protocol(dev)
    raw = bytes(dp.send_and_receive_raw_data(dp.build_command(0x15, 32)))
    rc = struct.unpack("<i", raw[:4])[0]
    if rc < 0:
        print(f"  rgb (t32): firmware returned {rc} - NOT dumped")
    else:
        results["rgb_t32"] = split_and_check(raw[4:], 32, "rgb (t32)")
except Exception as exc:  # noqa: BLE001 - deliberate; see below
    # Deliberately broad. The RGB read is best-effort: it goes through the raw
    # debug_protocol path, which can fail in ways the typed API cannot (an
    # unsupported opcode on some firmware, a short reply, a malformed header).
    # None of that should cost us the DEPTH backup, which is the one that
    # matters and has already succeeded by this point. Report and continue --
    # but report loudly, and let RESULT say 1 of 2 rather than claiming success.
    print(f"  rgb (t32): FAILED {type(exc).__name__}: {exc} - NOT dumped")

os.makedirs(OUT, exist_ok=True)
for name, blob in results.items():
    p = os.path.join(OUT, f"d435_{sn}_fw{fw}_{host}_{ts}_{name}.bin")
    with open(p, "wb") as fh:
        fh.write(blob)
    print(f"  wrote {p} ({len(blob)} B) sha256={hashlib.sha256(blob).hexdigest()[:16]}")
print(f"RESULT: {len(results)} of 2 tables backed up")
