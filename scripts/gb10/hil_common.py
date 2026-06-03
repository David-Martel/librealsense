#!/usr/bin/env python3
"""Standardized GB10 HIL test infrastructure — shared by every test in this dir so runs are
IDEMPOTENT (unique timestamped artifact dir, clean teardown) and STABLE (warmup, repeats,
percentile stats, pre-flight gate, continuous controller tripwire). No hardware side effects
beyond the camera; never leaves a stream open. Import this; don't run it.

Env it honors: LRS_BUILD (default build-gb10-full/Release for LD_LIBRARY_PATH/PYTHONPATH),
LRS_HIL_OUT (artifact root, default ~/realsense-gb10-validation/hil-runs), DISPLAY (for --display).
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

HIL_OUT = os.environ.get("LRS_HIL_OUT", os.path.expanduser("~/realsense-gb10-validation/hil-runs"))


def now_ts():
    return subprocess.run("date '+%Y-%m-%d %H:%M:%S'", shell=True, capture_output=True, text=True).stdout.strip()


def now_slug():
    return subprocess.run("date '+%Y%m%d-%H%M%S'", shell=True, capture_output=True, text=True).stdout.strip()


def stats(xs):
    if not xs:
        return None
    a = np.asarray(xs, dtype=float)
    return {"n": int(a.size), "mean": round(float(a.mean()), 3), "p50": round(float(np.percentile(a, 50)), 3),
            "p95": round(float(np.percentile(a, 95)), 3), "min": round(float(a.min()), 3), "max": round(float(a.max()), 3)}


class Tripwire(Exception):
    """Raised the instant a controller death is seen in the kernel journal."""


class HIL:
    """One idempotent HIL run: artifact dir, pre-flight, tripwire, result emission."""

    def __init__(self, name, display=False):
        self.name = name
        self.start_ts = now_ts()
        self.display = display and bool(os.environ.get("DISPLAY"))
        self.dir = os.path.join(HIL_OUT, f"{now_slug()}-{name}")
        os.makedirs(self.dir, exist_ok=True)
        self.report = {"test": name, "start": self.start_ts, "artifact_dir": self.dir,
                       "display": self.display, "controller_death": False, "minus110": 0}
        self._win = None

    # ---- controller health -------------------------------------------------
    def _journal(self):
        return subprocess.run(["bash", "-c", f"journalctl -k --since '{self.start_ts}' --no-pager 2>/dev/null"],
                              capture_output=True, text=True).stdout

    def preflight(self):
        """Refuse to start if the controller already shows a death or the camera is gone."""
        import pyrealsense2 as rs
        j = subprocess.run(["bash", "-c", "journalctl -k --no-pager -n 80 2>/dev/null"],
                           capture_output=True, text=True).stdout
        if any(("HC died" in l) or ("not responding to stop" in l) for l in j.splitlines()):
            raise Tripwire("pre-flight: controller already dead (reboot required)")
        if len(rs.context().query_devices()) == 0:
            raise Tripwire("pre-flight: no RealSense device")
        # USB-3 link gate
        try:
            d = rs.context().query_devices()[0]
            usb = d.get_info(rs.camera_info.usb_type_descriptor)
            self.report["usb_type"] = usb
            if usb and not usb.startswith("3"):
                self.report["usb_warning"] = f"link is USB {usb} (<3.x) — degraded/death-prone"
        except Exception:
            pass

    def check_tripwire(self, minus110_too=True):
        out = self._journal()
        if minus110_too:
            self.report["minus110"] = sum(1 for l in out.splitlines()
                                          if "-110" in l and "control" in l.lower())
        if any(("HC died" in l) or ("not responding to stop" in l) or ("assume dead" in l)
               for l in out.splitlines()):
            self.report["controller_death"] = True
            self._dump_forensics()
            raise Tripwire(f"HC died since {self.start_ts}")

    def _dump_forensics(self):
        with open(os.path.join(self.dir, "kernel-journal.txt"), "w") as f:
            f.write(self._journal())
        subprocess.run(["bash", "-c", f"lsusb -t > {self.dir}/lsusb-t.txt 2>&1"])

    # ---- non-headless display ---------------------------------------------
    def show(self, img, label=""):
        """Paint a frame to an on-screen window (only if --display + DISPLAY set)."""
        if not self.display:
            return
        import cv2
        if self._win is None:
            self._win = f"gb10-hil-{self.name}"
            cv2.namedWindow(self._win, cv2.WINDOW_NORMAL)
            cv2.moveWindow(self._win, 40, 40)
        if label:
            cv2.putText(img, label, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
        cv2.imshow(self._win, img)
        cv2.waitKey(1)

    def grab_proof(self, tag="proof"):
        """x11grab the screen as visible proof the render happened; returns path or None."""
        if not self.display:
            return None
        path = os.path.join(self.dir, f"{tag}.png")
        ffmpeg = os.environ.get("LRS_FFMPEG", "/opt/gb10-cuda/install/ffmpeg/bin/ffmpeg")
        rc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab",
                             "-video_size", "1920x1080", "-i", os.environ["DISPLAY"], "-frames:v", "1", path],
                            capture_output=True, text=True).returncode
        return path if rc == 0 and os.path.exists(path) else None

    def close_display(self):
        if self._win is not None:
            import cv2
            cv2.destroyAllWindows()

    # ---- result emission ---------------------------------------------------
    def finish(self, ok=True):
        out = self._journal()
        self.report["final_minus110"] = sum(1 for l in out.splitlines() if "-110" in l and "control" in l.lower())
        self.report["controller_green"] = not self.report["controller_death"]
        self.report["result"] = self.report.get("result", "PASS" if ok and not self.report["controller_death"] else "FAIL")
        with open(os.path.join(self.dir, "result.json"), "w") as f:
            json.dump(self.report, f, indent=2)
        print(json.dumps(self.report, indent=2))
        return 0 if self.report["controller_green"] and ok else 1
