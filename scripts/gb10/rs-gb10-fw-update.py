#!/usr/bin/env python3
"""GB10 RealSense firmware status + safety-gated update for ALL linked cameras.

WHY: the controller deaths (#1/#2/#3) were on a camera running OLD firmware
5.13.0.55; the surviving unit runs 5.15.1.55. The current **Intel D435
support-matrix floor is 5.17.3.10+** (fleet finding 2026-07-17; 5.17.0.10 is
now stale — do NOT target it). Bringing every linked camera to the signed matrix
firmware is a leading candidate for removing the `-110` control-transfer trigger
(see docs/gb10/FORK-VS-UPSTREAM-AND-CAMERA-FIRMWARE).
NOTE: this tool only reports/targets the version; flashing remains gated and
operator-led.

This tool reports by default (no device change). Flashing requires both --flash
and a signed image because a half-flash over a marginal link can brick the
camera. Per-camera preflights require a USB-3 link, a green controller, the
published image SHA-256, and a verified nonempty firmware backup. Download the
signed image from https://dev.realsenseai.com/docs/firmware-releases-d400/.

DOWNGRADE — VERIFIED FROM librealsense SOURCE
(src/fw-update/fw-update-device.cpp):
  * The host does no version check. `rs-fw-update -f`/SDK `update()` streams any
    signed image block-by-block. This wrapper stops host-side downgrades.
  * The device enforces anti-rollback in DFU mode using `dfu_is_locked` and
    `fw_highest_version`. A locked unit rejects an image below its highest
    installed version.
  * Production D435 units are DFU-locked and forward-only. Lock state is only
    readable after entering DFU mode, so report mode does not query it.

Usage:
  rs-gb10-fw-update.py                       # report all linked cameras
  rs-gb10-fw-update.py --flash --image <Signed_Image_UVC_5_17_3_10.bin>
  rs-gb10-fw-update.py --flash --image <bin> --serial <S>
"""

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

TARGET = (
    5,
    17,
    3,
    10,
)  # current Intel D435 support-matrix floor (5.17.3.10+); 5.17.0.10 is now stale
EXPECTED_IMAGE_SHA256 = (
    "4c347e3a18eac97b41a97947ef3b225bc1eaf77c68ffd87598736e3cebf3e4d2"
)
EXPECTED_BACKUP_BYTES = 2 * 1024 * 1024
RS_FW_UPDATE = os.environ.get(
    "LRS_RS_FW_UPDATE",
    os.path.expanduser(
        "~/realsense-gb10-validation/build-gb10-full/Release/rs-fw-update"
    ),
)


def ver(s):
    try:
        return tuple(int(x) for x in s.split("."))
    except (ValueError, AttributeError):
        return (0,)


def image_version(path):
    """Parse the firmware version from a signed-image filename, e.g.
    Signed_Image_UVC_5_17_3_10.bin -> (5,17,3,10). Returns None if not parseable."""
    import re

    m = re.search(r"(\d+)[._](\d+)[._](\d+)[._](\d+)", os.path.basename(path))
    return tuple(int(g) for g in m.groups()) if m else None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_flash_image(path):
    """Require the exact matrix-target image published by RealSense."""
    img_ver = image_version(path)
    if img_ver is None:
        raise ValueError(
            f"cannot parse a firmware version from '{os.path.basename(path)}'"
        )
    if img_ver != TARGET:
        expected = ".".join(map(str, TARGET))
        actual = ".".join(map(str, img_ver))
        raise ValueError(f"image version {actual} does not match target {expected}")

    digest = sha256_file(path)
    if digest != EXPECTED_IMAGE_SHA256:
        raise ValueError(
            f"image SHA-256 {digest} does not match published artifact "
            f"{EXPECTED_IMAGE_SHA256}"
        )
    return img_ver


@contextmanager
def verified_flash_image(path):
    """Yield a validated private copy insulated from later source changes."""
    with tempfile.TemporaryDirectory(prefix="rs-fw-image-") as temp_dir:
        private_image = os.path.join(temp_dir, os.path.basename(path))
        with open(path, "rb") as source, open(private_image, "xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(private_image, 0o400)
        image_version_tuple = validate_flash_image(private_image)
        yield image_version_tuple, private_image


def backup_artifact_is_valid(path):
    """Return whether path is a regular, complete firmware backup."""
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except (OSError, TypeError):
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_size == EXPECTED_BACKUP_BYTES


def backup_firmware(rs_fw_update, serial, current_fw, env, backup_root=None):
    """Back up firmware atomically and return its path, or None on failure."""
    if backup_root is None:
        backup_root = os.path.expanduser("~/realsense-gb10-validation/fw-backups")
    os.makedirs(backup_root, exist_ok=True)
    backup = os.path.join(backup_root, f"{serial}-{current_fw}.bin")
    partial = f"{backup}.partial-{os.getpid()}"
    try:
        os.unlink(partial)
    except FileNotFoundError:
        pass

    try:
        result = subprocess.run(
            [rs_fw_update, "-s", serial, "-b", partial],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
        return None
    diagnostic = f"{result.stdout}\n{result.stderr}"
    if (
        result.returncode != 0
        or "Creating backup file failed" in diagnostic
        or not backup_artifact_is_valid(partial)
    ):
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
        return None

    os.replace(partial, backup)
    return backup if backup_artifact_is_valid(backup) else None


def controller_green():
    result = subprocess.run(
        ["journalctl", "-k", "-b", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    fatal_markers = ("HC died", "not responding to stop", "Host halt failed")
    return not any(
        marker in line
        for line in result.stdout.splitlines()
        for marker in fatal_markers
    )


def flash_cameras(cams, img_ver, image_path, allow_downgrade):
    """Back up and flash each selected camera through the verified image copy."""
    rc_all = 0
    for camera in cams:
        # rs-fw-update itself flashes any supplied image regardless of version.
        if img_ver < ver(camera["fw"]) and not allow_downgrade:
            print(
                f"REFUSE {camera['serial']}: image "
                f"{'.'.join(map(str, img_ver))} is OLDER than current "
                f"{camera['fw']} — silent downgrade. "
                "Pass --allow-downgrade to override.",
                file=sys.stderr,
            )
            rc_all = 1
            continue
        if img_ver == ver(camera["fw"]):
            print(f"skip {camera['serial']} (already at image version {camera['fw']})")
            continue
        if not camera["usb3"]:
            print(
                f"REFUSE {camera['serial']}: USB-2 link ({camera['usb']}) — "
                "a half-flash over a marginal link can brick the camera. "
                "Move to a native USB-3 port first.",
                file=sys.stderr,
            )
            rc_all = 1
            continue
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = (
            os.path.dirname(RS_FW_UPDATE) + ":" + env.get("LD_LIBRARY_PATH", "")
        )
        print(f"\n>>> {camera['serial']}: back up current firmware before flashing")
        backup = backup_firmware(RS_FW_UPDATE, camera["serial"], camera["fw"], env)
        if not backup_artifact_is_valid(backup):
            print(
                f"ABORT {camera['serial']}: firmware backup failed or was invalid; "
                "camera was not flashed.",
                file=sys.stderr,
            )
            rc_all = 1
            continue
        print(f"    backup verified: {backup} ({os.path.getsize(backup)} bytes)")
        print(f"    flashing {os.path.basename(image_path)}")
        rc = subprocess.run(
            [RS_FW_UPDATE, "-s", camera["serial"], "-f", image_path], env=env
        ).returncode
        print(f"    rs-fw-update exit={rc}")
        rc_all = rc_all or rc
    return rc_all


def main():
    ap = argparse.ArgumentParser(description="RealSense firmware status + gated update")
    ap.add_argument(
        "--flash", action="store_true", help="actually flash (default: report only)"
    )
    ap.add_argument("--image", help="signed firmware .bin (required to flash)")
    ap.add_argument("--serial", help="restrict to one SDK serial")
    ap.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="permit flashing an image OLDER than a camera's current firmware",
    )
    args = ap.parse_args()

    import pyrealsense2 as rs

    devs = list(rs.context().query_devices())
    if not devs:
        print("No RealSense devices.", file=sys.stderr)
        return 2

    target_str = ".".join(map(str, TARGET))
    print(f"Target (latest D400 production firmware): {target_str}\n")
    cams = []
    for d in devs:

        def get_info(key):
            return d.get_info(key) if d.supports(key) else "?"

        serial = get_info(rs.camera_info.serial_number)
        if args.serial and serial != args.serial:
            continue
        fw = get_info(rs.camera_info.firmware_version)
        usb = get_info(rs.camera_info.usb_type_descriptor)
        need = ver(fw) < TARGET
        usb3 = str(usb).startswith("3")
        cams.append(
            {"serial": serial, "fw": fw, "usb": usb, "usb3": usb3, "needs_update": need}
        )
        print(
            f"  {get_info(rs.camera_info.name)}  serial={serial}  fw={fw}  usb={usb}"
            f"  -> {'UPDATE AVAILABLE (' + target_str + ')' if need else 'up to date'}"
            f"{'' if usb3 else '  [!! USB-2 link — flashing UNSAFE]'}"
        )

    if not args.flash:
        n = sum(c["needs_update"] for c in cams)
        print(
            f"\n{n}/{len(cams)} camera(s) below {target_str}. Re-run with "
            f"--flash --image <Signed_Image_UVC_5_17_3_10.bin> to update (gated)."
        )
        return 0

    # ---- flash path (gated) ----
    if not args.image or not os.path.exists(args.image):
        print(
            "ERROR: --flash requires --image <signed .bin> that exists. Download from "
            "https://dev.realsenseai.com/docs/firmware-releases-d400/",
            file=sys.stderr,
        )
        return 2
    if not controller_green():
        print(
            "ABORT: controller shows a prior HC-died — reboot before flashing.",
            file=sys.stderr,
        )
        return 2
    if not os.path.exists(RS_FW_UPDATE):
        print(
            f"ERROR: rs-fw-update not found at {RS_FW_UPDATE} (set LRS_RS_FW_UPDATE).",
            file=sys.stderr,
        )
        return 2

    try:
        with verified_flash_image(args.image) as (img_ver, private_image):
            print(f"Image version: {'.'.join(map(str, img_ver))}")
            print(f"Image SHA-256: {EXPECTED_IMAGE_SHA256}")
            rc_all = flash_cameras(cams, img_ver, private_image, args.allow_downgrade)
    except (OSError, ValueError) as exc:
        print(f"ERROR: unsafe firmware image: {exc}", file=sys.stderr)
        return 2
    print("\nDone. Re-run (report mode) to confirm new firmware versions.")
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
