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

The wrapper holds a cooperative host-wide maintenance lock and scans visible
Linux process descriptors for holders of the camera's usbfs, V4L2, media, and
hidraw nodes before backup and again before flash. A non-cooperating process can
still open the device after either scan; stop camera services for the entire
maintenance window. The holder gate fails closed when a live non-kernel process
has an inaccessible descriptor table; run firmware maintenance with sufficient
privilege to inspect every userspace process.

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

Exact spark-0060 fleet invocation (all runtime paths are explicit):
  /usr/bin/sudo /usr/bin/env -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib/\
python3.12/site-packages \
    LD_LIBRARY_PATH=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib \
    LRS_RS_FW_UPDATE=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/bin/\
rs-fw-update \
    LRS_FW_BACKUP_ROOT=/home/damartel/realsense-gb10-validation/fw-backups \
    /usr/bin/python3.12 \
    /home/damartel/dev/repos/librealsense/scripts/gb10/rs-gb10-fw-update.py \
    --flash --serial 327122076391 \
    --image /home/damartel/realsense-gb10-validation/firmware/\
Signed_Image_UVC_5_17_3_10.bin
"""

import argparse
import fcntl
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

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
    "/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/bin/rs-fw-update",
)
BACKUP_ROOT = os.environ.get(
    "LRS_FW_BACKUP_ROOT", "/home/damartel/realsense-gb10-validation/fw-backups"
)
LOCK_PATH = "/run/lock/realsense-gb10-firmware.lock"
JOURNALCTL = "/usr/bin/journalctl"
PF_KTHREAD = 0x00200000
FLEET_SUDO_0060 = (
    "/usr/bin/sudo",
    "/usr/bin/env",
    "-i",
    "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONPATH=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib/python3.12/site-packages",
    "LD_LIBRARY_PATH=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/lib",
    "LRS_RS_FW_UPDATE=/opt/vigil/opt/librealsense-v2.58.1-dgx-spark-gb10/bin/rs-fw-update",
    "LRS_FW_BACKUP_ROOT=/home/damartel/realsense-gb10-validation/fw-backups",
    "/usr/bin/python3.12",
    "/home/damartel/dev/repos/librealsense/scripts/gb10/rs-gb10-fw-update.py",
    "--flash",
    "--serial",
    "327122076391",
    "--image",
    "/home/damartel/realsense-gb10-validation/firmware/Signed_Image_UVC_5_17_3_10.bin",
)


class SafetyGateError(RuntimeError):
    """A required firmware-maintenance safety condition was not met."""


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


def validate_serial_selection(cameras, requested_serial):
    """Require --serial to resolve to exactly one SDK device."""
    if requested_serial and len(cameras) != 1:
        raise SafetyGateError(
            f"--serial {requested_serial!r} matched {len(cameras)} devices; "
            "exactly one is required"
        )


def require_firmware_privileges():
    """Require root before creating the host lock or inspecting process holders."""
    if os.geteuid() != 0:
        raise SafetyGateError(
            "firmware flashing requires effective uid 0 before any maintenance "
            "lock is created or holder scan runs. Exact spark-0060 invocation:\n  "
            + shlex.join(FLEET_SUDO_0060)
        )


@contextmanager
def firmware_maintenance_lock(lock_path=None):
    """Hold one nonblocking host lock across every selected camera operation."""
    require_firmware_privileges()
    path = Path(lock_path or LOCK_PATH)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SafetyGateError(
            f"cannot establish host firmware lock at {path}: {exc}; use the same "
            "account that created the 0600 lock or remove it only after proving "
            "no maintenance operation is active"
        ) from exc
    locked = False
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise SafetyGateError(
                f"unsafe host firmware lock metadata at {path}; require a regular, "
                "single-link, root-owned 0600 file"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SafetyGateError(
                "another host firmware maintenance operation already holds the lock"
            ) from exc
        locked = True
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.fsync(fd)
        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _is_within(path, root):
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _usb_device_root(camera, sys_root):
    """Resolve one SDK camera to exactly one USB device in sysfs."""
    sys_root = Path(sys_root)
    usb_devices = sys_root / "bus" / "usb" / "devices"
    physical_port = str(camera.get("physical_port", "")).strip()
    firmware_update_id = str(camera.get("firmware_update_id", "")).strip()
    if firmware_update_id == "?":
        firmware_update_id = ""
    candidates = []
    if physical_port and physical_port != "?":
        port_path = Path(physical_port)
        candidates.extend((port_path, usb_devices / physical_port))
        if "/" not in physical_port and "-" in physical_port:
            compact_parent, compact_suffix = physical_port.rsplit("-", 1)
            if compact_suffix.isdigit():
                candidates.append(usb_devices / compact_parent)
        if port_path.is_absolute():
            try:
                candidates.append(sys_root / port_path.relative_to("/sys"))
            except ValueError:
                pass

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        for parent in (resolved, *resolved.parents):
            if (parent / "busnum").is_file() and (parent / "devnum").is_file():
                device_serial = (
                    (parent / "serial").read_text(encoding="utf-8").strip()
                    if (parent / "serial").is_file()
                    else ""
                )
                if firmware_update_id and device_serial != firmware_update_id:
                    break
                return parent

    if not firmware_update_id:
        raise SafetyGateError(
            f"camera {camera['serial']} has neither a resolvable physical port nor "
            "a firmware update ID; cannot prove the target USB device"
        )
    matches = []
    try:
        entries = list(usb_devices.iterdir())
    except OSError as exc:
        raise SafetyGateError(
            f"cannot inspect USB sysfs at {usb_devices}: {exc}"
        ) from exc
    for entry in entries:
        serial_path = entry / "serial"
        try:
            if serial_path.read_text(encoding="utf-8").strip() == firmware_update_id:
                matches.append(entry.resolve(strict=True))
        except OSError:
            continue
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        raise SafetyGateError(
            f"camera {camera['serial']} mapped to {len(matches)} USB sysfs devices; "
            f"firmware update ID {firmware_update_id!r} is not unique"
        )
    return matches[0]


def camera_device_nodes(camera, sys_root="/sys", dev_root="/dev"):
    """Return usbfs and class device nodes belonging to one camera."""
    sys_root = Path(sys_root)
    dev_root = Path(dev_root)
    usb_root = _usb_device_root(camera, sys_root)
    try:
        bus = int((usb_root / "busnum").read_text(encoding="utf-8"))
        device = int((usb_root / "devnum").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyGateError(f"invalid USB bus/device metadata at {usb_root}") from exc

    nodes = {dev_root / "bus" / "usb" / f"{bus:03d}" / f"{device:03d}"}
    for class_name in ("video4linux", "media", "hidraw"):
        class_root = sys_root / "class" / class_name
        try:
            entries = list(class_root.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SafetyGateError(f"cannot inspect {class_root}: {exc}") from exc
        for entry in entries:
            try:
                target = (entry / "device").resolve(strict=True)
            except OSError:
                continue
            if _is_within(target, usb_root):
                nodes.add(dev_root / entry.name)

    missing = sorted(str(node) for node in nodes if not node.exists())
    if missing:
        raise SafetyGateError(
            f"camera {camera['serial']} has missing device nodes: {', '.join(missing)}"
        )
    return nodes


def _file_identity(path):
    metadata = os.stat(path)
    if stat.S_ISCHR(metadata.st_mode) or stat.S_ISBLK(metadata.st_mode):
        return ("device", metadata.st_rdev)
    return ("inode", metadata.st_dev, metadata.st_ino)


def _is_kernel_thread(process):
    """Return True for a kernel thread, False for userspace, or None if gone."""
    try:
        raw = (process / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False if process.exists() else None
    except OSError:
        return False
    end_name = raw.rfind(")")
    fields = raw[end_name + 2 :].split() if end_name >= 0 else []
    try:
        flags = int(fields[6])
    except (IndexError, ValueError):
        return False
    return bool(flags & PF_KTHREAD)


def camera_holders(camera, proc_root="/proc", sys_root="/sys", dev_root="/dev"):
    """Find visible processes with an open descriptor for the target camera.

    Linux may hide another user's descriptors. Any inaccessible live userspace
    descriptor table makes the result incomplete and aborts the operation. This
    still cannot close the race where a process opens a node after the scan.
    """
    nodes = camera_device_nodes(camera, sys_root=sys_root, dev_root=dev_root)
    try:
        identities = {_file_identity(node) for node in nodes}
    except OSError as exc:
        raise SafetyGateError(
            f"cannot identify all camera device nodes: {exc}"
        ) from exc

    proc_root = Path(proc_root)
    try:
        processes = list(proc_root.iterdir())
    except OSError as exc:
        raise SafetyGateError(
            f"cannot inspect process table at {proc_root}: {exc}"
        ) from exc
    holders = {}
    incomplete = set()
    for process in processes:
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            kernel_thread = _is_kernel_thread(process)
            if kernel_thread is False:
                incomplete.add(int(process.name))
            continue
        for descriptor in descriptors:
            try:
                identity = _file_identity(descriptor)
            except FileNotFoundError:
                continue
            except OSError:
                incomplete.add(int(process.name))
                continue
            if identity not in identities:
                continue
            try:
                command = (process / "cmdline").read_bytes().replace(b"\0", b" ")
                command_text = command.decode(errors="replace").strip()
            except OSError:
                command_text = ""
            holders[int(process.name)] = command_text or "unknown"
            break
    if incomplete:
        sample = ", ".join(str(pid) for pid in sorted(incomplete)[:8])
        raise SafetyGateError(
            f"process descriptor visibility is incomplete for {len(incomplete)} "
            f"live userspace process(es), including pid(s) {sample}; rerun with "
            "sufficient privilege to inspect all /proc file descriptors"
        )
    return nodes, holders


def require_camera_idle(camera):
    """Abort unless the target camera has no visible live holders."""
    nodes, holders = camera_holders(camera)
    if holders:
        details = ", ".join(
            f"pid {pid} ({command})" for pid, command in holders.items()
        )
        raise SafetyGateError(f"camera {camera['serial']} is in use by {details}")
    print(
        f"    holder scan clear across {len(nodes)} device node(s) "
        "(visible /proc descriptors)"
    )


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
        backup_root = BACKUP_ROOT
    os.makedirs(backup_root, exist_ok=True)
    backup = os.path.join(backup_root, f"{serial}-{current_fw}.bin")
    partial = f"{backup}.partial-{os.getpid()}"
    if os.path.lexists(backup):
        raise SafetyGateError(
            f"backup already exists and will not be overwritten: {backup}"
        )
    if os.path.lexists(partial):
        raise SafetyGateError(
            f"stale partial backup requires operator inspection: {partial}"
        )

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

    try:
        os.link(partial, backup, follow_symlinks=False)
    except FileExistsError as exc:
        os.unlink(partial)
        raise SafetyGateError(
            f"backup appeared concurrently and was not overwritten: {backup}"
        ) from exc
    os.unlink(partial)
    return backup if backup_artifact_is_valid(backup) else None


def controller_green():
    result = subprocess.run(
        [JOURNALCTL, "-k", "-b", "--no-pager"],
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


def flash_cameras(cams, img_ver, image_path, allow_downgrade, lock_path=None):
    """Back up and flash selected cameras under one host maintenance lock."""
    require_firmware_privileges()
    rc_all = 0
    with firmware_maintenance_lock(lock_path=lock_path):
        for camera in cams:
            rc_all = _flash_camera(camera, img_ver, image_path, allow_downgrade, rc_all)
    return rc_all


def _flash_camera(camera, img_ver, image_path, allow_downgrade, rc_all):
    """Run every safety gate and optionally flash one selected camera."""
    # rs-fw-update itself flashes any supplied image regardless of version.
    if img_ver < ver(camera["fw"]) and not allow_downgrade:
        print(
            f"REFUSE {camera['serial']}: image "
            f"{'.'.join(map(str, img_ver))} is OLDER than current "
            f"{camera['fw']} — silent downgrade. "
            "Pass --allow-downgrade to override.",
            file=sys.stderr,
        )
        return 1
    if img_ver == ver(camera["fw"]):
        print(f"skip {camera['serial']} (already at image version {camera['fw']})")
        return rc_all
    if not camera["usb3"]:
        print(
            f"REFUSE {camera['serial']}: USB-2 link ({camera['usb']}) — "
            "a half-flash over a marginal link can brick the camera. "
            "Move to a native USB-3 port first.",
            file=sys.stderr,
        )
        return 1

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = (
        os.path.dirname(RS_FW_UPDATE) + ":" + env.get("LD_LIBRARY_PATH", "")
    )
    require_camera_idle(camera)
    print(f"\n>>> {camera['serial']}: back up current firmware before flashing")
    backup = backup_firmware(RS_FW_UPDATE, camera["serial"], camera["fw"], env)
    if not backup_artifact_is_valid(backup):
        print(
            f"ABORT {camera['serial']}: firmware backup failed or was invalid; "
            "camera was not flashed.",
            file=sys.stderr,
        )
        return 1
    print(f"    backup verified: {backup} ({os.path.getsize(backup)} bytes)")
    require_camera_idle(camera)
    print(f"    flashing {os.path.basename(image_path)}")
    rc = subprocess.run(
        [RS_FW_UPDATE, "-s", camera["serial"], "-f", image_path], env=env
    ).returncode
    print(f"    rs-fw-update exit={rc}")
    return rc_all or rc


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

    if args.flash:
        try:
            require_firmware_privileges()
        except SafetyGateError as exc:
            print(f"ABORT: {exc}", file=sys.stderr)
            return 2

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
        physical_port = get_info(rs.camera_info.physical_port)
        firmware_update_id = get_info(rs.camera_info.firmware_update_id)
        need = ver(fw) < TARGET
        usb3 = str(usb).startswith("3")
        cams.append(
            {
                "serial": serial,
                "fw": fw,
                "usb": usb,
                "usb3": usb3,
                "needs_update": need,
                "physical_port": physical_port,
                "firmware_update_id": firmware_update_id,
            }
        )
        print(
            f"  {get_info(rs.camera_info.name)}  serial={serial}  fw={fw}  usb={usb}"
            f"  -> {'UPDATE AVAILABLE (' + target_str + ')' if need else 'up to date'}"
            f"{'' if usb3 else '  [!! USB-2 link — flashing UNSAFE]'}"
        )

    try:
        validate_serial_selection(cams, args.serial)
    except SafetyGateError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2

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
    except SafetyGateError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"ERROR: unsafe firmware image: {exc}", file=sys.stderr)
        return 2
    print("\nDone. Re-run (report mode) to confirm new firmware versions.")
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
