#!/usr/bin/env python3
"""P7 re-acquire guard confirm (single-context, safe). Opens the D435 once, enumerates
both sensors, streams depth briefly. Intended to be run under RS2_GB10_REFUSE_REACQUIRE=1:
a P7 false-positive would throw in a sensor's usb_device_libusb ctor and drop a sensor or
the device. A complete depth+color enumeration + clean depth stream => guard does NOT
false-fire on a legitimate single-context session. Uses the build pointed to by
LD_LIBRARY_PATH/PYTHONPATH (see `just hil-p7`)."""
import sys
import pyrealsense2 as rs


def main():
    ctx = rs.context()
    devs = ctx.query_devices()
    if len(devs) == 0:
        print("NO_DEVICE", file=sys.stderr)
        return 2
    d = devs[0]
    sensors = d.query_sensors()
    names = [s.get_info(rs.camera_info.name) for s in sensors]
    print(f"DEVICE={d.get_info(rs.camera_info.name)} "
          f"USB={d.get_info(rs.camera_info.usb_type_descriptor)} SENSORS={len(sensors)} {names}")
    ok_both = any("Stereo" in n for n in names) and any("RGB" in n for n in names)

    pipe = rs.pipeline(ctx)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.depth, 848, 480, rs.format.z16, 60)
    pipe.start(cfg)
    n = 0
    for _ in range(60):
        pipe.wait_for_frames(3000)
        n += 1
    pipe.stop()
    print(f"FRAMES_OK={n} BOTH_SENSORS={'yes' if ok_both else 'NO(!)'}")
    # If REFUSE had false-fired, a sensor would be missing -> ok_both False.
    return 0 if (n == 60 and ok_both) else 1


if __name__ == "__main__":
    sys.exit(main())
