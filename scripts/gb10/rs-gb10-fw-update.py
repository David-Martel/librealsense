#!/usr/bin/env python3
"""GB10 RealSense firmware status + safety-gated update for ALL linked cameras.

WHY: the controller deaths (#1/#2/#3) were on a camera running OLD firmware 5.13.0.55; the
surviving unit runs 5.15.1.55. The latest D400 production firmware is **5.17.0.10** (Jul 2025).
Bringing every linked camera to a known-good recent firmware is a leading candidate for removing
the `-110` control-transfer trigger (see docs/gb10/FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE).

This tool REPORTS by default (no device change). Flashing is gated behind --flash AND requires a
signed image (--image) because a half-flash over a marginal link can brick the camera. Safety
pre-flights per camera: USB-3 link (refuse USB-2), controller GREEN (no recent HC-died), and it
backs up the current firmware first. Download signed images from Intel/RealSense
(https://dev.realsenseai.com/docs/firmware-releases-d400/ ; file `Signed_Image_UVC_5_17_0_10.bin`).

DOWNGRADE — VERIFIED FROM librealsense SOURCE (src/fw-update/fw-update-device.cpp):
  * The HOST does NO version check — `rs-fw-update -f`/SDK `update()` streams ANY signed image
    block-by-block (:205-247). So the host tool will *attempt* a downgrade; this wrapper's
    --allow-downgrade guard is what stops it host-side.
  * The DEVICE enforces anti-rollback in DFU mode: it reports `dfu_is_locked` + `fw_highest_version`
    ("highest ever installed"); a LOCKED unit rejects an image not higher than that and the SDK
    throws "Device is locked for update. Use firmware version higher than: <highest>" (:242).
  => Production D435 units ship DFU-LOCKED: downgrade is BLOCKED by the device (forward-only).
     Only an UNLOCKED/dev unit accepts a downgrade. A unit's lock state is only readable after it
     enters DFU mode (`rs-fw-update` detach) — not checked here (report mode does not touch DFU).

Usage:
  rs-gb10-fw-update.py                       # report all cameras' fw vs target (dry-run)
  rs-gb10-fw-update.py --flash --image <Signed_Image_UVC_5_17_0_10.bin>   # flash (gated)
  rs-gb10-fw-update.py --flash --image <bin> --serial <S>                 # one camera
"""
import argparse
import os
import subprocess
import sys

TARGET = (5, 17, 0, 10)   # latest D400 production firmware (2026-06: 5.17.0.10)
RS_FW_UPDATE = os.environ.get("LRS_RS_FW_UPDATE",
                              os.path.expanduser("~/realsense-gb10-validation/build-gb10-full/Release/rs-fw-update"))


def ver(s):
    try:
        return tuple(int(x) for x in s.split("."))
    except (ValueError, AttributeError):
        return (0,)


def image_version(path):
    """Parse the firmware version from a signed-image filename, e.g.
    Signed_Image_UVC_5_17_0_10.bin -> (5,17,0,10). Returns None if not parseable."""
    import re
    m = re.search(r"(\d+)[._](\d+)[._](\d+)[._](\d+)", os.path.basename(path))
    return tuple(int(g) for g in m.groups()) if m else None


def controller_green():
    out = subprocess.run(["bash", "-c", "journalctl -k --no-pager -n 120 2>/dev/null"],
                         capture_output=True, text=True).stdout
    return not any(("HC died" in l) or ("not responding to stop" in l) for l in out.splitlines())


def main():
    ap = argparse.ArgumentParser(description="RealSense firmware status + gated update")
    ap.add_argument("--flash", action="store_true", help="actually flash (default: report only)")
    ap.add_argument("--image", help="signed firmware .bin (required to flash)")
    ap.add_argument("--serial", help="restrict to one SDK serial")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="permit flashing an image OLDER than a camera's current firmware")
    args = ap.parse_args()

    import pyrealsense2 as rs
    devs = list(rs.context().query_devices())
    if not devs:
        print("No RealSense devices.", file=sys.stderr); return 2

    target_str = ".".join(map(str, TARGET))
    print(f"Target (latest D400 production firmware): {target_str}\n")
    cams = []
    for d in devs:
        g = lambda k: d.get_info(k) if d.supports(k) else "?"
        serial = g(rs.camera_info.serial_number)
        if args.serial and serial != args.serial:
            continue
        fw = g(rs.camera_info.firmware_version)
        usb = g(rs.camera_info.usb_type_descriptor)
        need = ver(fw) < TARGET
        usb3 = str(usb).startswith("3")
        cams.append({"serial": serial, "fw": fw, "usb": usb, "usb3": usb3, "needs_update": need})
        print(f"  {g(rs.camera_info.name)}  serial={serial}  fw={fw}  usb={usb}"
              f"  -> {'UPDATE AVAILABLE (' + target_str + ')' if need else 'up to date'}"
              f"{'' if usb3 else '  [!! USB-2 link — flashing UNSAFE]'}")

    if not args.flash:
        n = sum(c["needs_update"] for c in cams)
        print(f"\n{n}/{len(cams)} camera(s) below {target_str}. Re-run with "
              f"--flash --image <Signed_Image_UVC_5_17_0_10.bin> to update (gated).")
        return 0

    # ---- flash path (gated) ----
    if not args.image or not os.path.exists(args.image):
        print("ERROR: --flash requires --image <signed .bin> that exists. Download from "
              "https://dev.realsenseai.com/docs/firmware-releases-d400/", file=sys.stderr)
        return 2
    if not controller_green():
        print("ABORT: controller shows a prior HC-died — reboot before flashing.", file=sys.stderr)
        return 2
    if not os.path.exists(RS_FW_UPDATE):
        print(f"ERROR: rs-fw-update not found at {RS_FW_UPDATE} (set LRS_RS_FW_UPDATE).", file=sys.stderr)
        return 2

    img_ver = image_version(args.image)
    if img_ver is None:
        print(f"ERROR: cannot parse a firmware version from '{os.path.basename(args.image)}' — expected a name "
              f"like Signed_Image_UVC_5_17_0_10.bin. rs-fw-update -f flashes ANY image regardless of version, so "
              f"the downgrade guard cannot run; aborting.", file=sys.stderr)
        return 2
    print(f"Image version: {'.'.join(map(str, img_ver))}")

    rc_all = 0
    for c in cams:
        # Downgrade guard: rs-fw-update flashes whatever image it is given, regardless of version.
        if img_ver < ver(c["fw"]) and not args.allow_downgrade:
            print(f"REFUSE {c['serial']}: image {'.'.join(map(str, img_ver))} is OLDER than current "
                  f"{c['fw']} — silent downgrade. Pass --allow-downgrade to override.", file=sys.stderr)
            rc_all = 1; continue
        if img_ver == ver(c["fw"]):
            print(f"skip {c['serial']} (already at image version {c['fw']})"); continue
        if not c["usb3"]:
            print(f"REFUSE {c['serial']}: USB-2 link ({c['usb']}) — a half-flash over a marginal link "
                  f"can brick the camera. Move to a native USB-3 port first.", file=sys.stderr)
            rc_all = 1; continue
        backup = os.path.expanduser(f"~/realsense-gb10-validation/fw-backups/{c['serial']}-{c['fw']}.bin")
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = os.path.dirname(RS_FW_UPDATE) + ":" + env.get("LD_LIBRARY_PATH", "")
        print(f"\n>>> {c['serial']}: backup -> {backup}, then flash {os.path.basename(args.image)}")
        subprocess.run([RS_FW_UPDATE, "-s", c["serial"], "-b", backup], env=env)
        rc = subprocess.run([RS_FW_UPDATE, "-s", c["serial"], "-f", args.image], env=env).returncode
        print(f"    rs-fw-update exit={rc}")
        rc_all = rc_all or rc
    print("\nDone. Re-run (report mode) to confirm new firmware versions.")
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
