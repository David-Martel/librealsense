# GB10 RealSense Debugging Notes

<!-- Hand-written debugging brief (and session appendix) for the Intel RealSense D435
     on NVIDIA DGX Spark / GB10. Originally saved as ~/Desktop/realsense_debugging.txt.
     Preserved verbatim below the separator. 623 lines / 25632 bytes (original).
     Source: ~/Desktop/realsense_debugging.txt as of 2026-06-05.
     Do NOT edit the content below -- treat it as a timestamped log artifact. -->

---

You are an expert Linux USB / kernel / librealsense / ARM64 debugging agent.

I need you to diagnose why my Intel RealSense camera on an NVIDIA DGX Spark appears to "stop" in a bad way when my program finishes, instead of cleanly pausing or shutting down. After that happens, the camera becomes unusable and is no longer recognized over USB until I physically reconnect it or otherwise recover it.

Environment and problem summary:
- Host: NVIDIA DGX Spark
- OS: DGX OS / Ubuntu-like Linux on ARM64
- Device: Intel RealSense camera
- Symptom:
  - Camera works initially
  - When my program finishes, the camera does not remain healthy
  - It seems to "stop" in a way that causes the USB device to disappear or become non-functional
  - Afterward, the camera may no longer show up in lsusb / rs-enumerate-devices / application code
- I want to know whether this is:
  1) an application shutdown bug
  2) a librealsense pipeline lifecycle issue
  3) a kernel/UVC/xHCI/USB controller problem
  4) USB power management / autosuspend / runtime PM
  5) cabling / power / hub / bandwidth instability
  6) a firmware or device fault
  7) a DGX Spark-specific compatibility or USB topology issue

Your goals:
1. Build a disciplined, evidence-driven differential diagnosis.
2. Reproduce the failure with the smallest possible test harness.
3. Determine exactly what changes at the moment the program exits:
   - process exits cleanly?
   - pipeline.stop() returns?
   - file descriptors remain open?
   - kernel logs a disconnect/reset/error?
   - USB device disappears from enumeration?
   - the device remains present but cannot stream?
4. Distinguish between:
   - “camera still enumerates but streaming fails”
   - “camera vanishes from USB enumeration entirely”
   - “camera remains on USB but RealSense APIs no longer see it”
5. Recommend the safest fix and recovery strategy.

Very important guardrails:
- Do NOT make destructive or irreversible changes first.
- Do NOT upgrade kernel/firmware/packages unless the evidence strongly justifies it and you clearly explain why.
- Prefer observation and minimally invasive tests first.
- If you need elevated privileges, explicitly say so.
- Keep a timestamped log of every command, output snippet, and inference.
- If you propose a reset/rebind/reload step, do it only after gathering pre-failure and post-failure evidence.
- Assume I may have sudo access but want to minimize reboots and physical reconnection.

Please work in this order:

PHASE 1 — Baseline Inventory
Collect and summarize:
- uname -a
- lsb_release -a or /etc/os-release
- architecture (uname -m)
- current kernel version
- installed RealSense packages / SDK version
- connected USB topology and negotiated speeds
- camera model, firmware version, serial, USB type descriptor if visible

Run commands like:
- uname -a
- uname -m
- cat /etc/os-release
- lsusb
- lsusb -t
- lsusb -nn | grep -i 8086 || true
- dmesg | tail -n 200
- journalctl -k -b --no-pager | tail -n 300
- rs-enumerate-devices || true
- realsense-viewer --version || true
- dpkg -l | grep -i realsense || true
- python3 -c "import pyrealsense2 as rs; print(rs.__version__)" || true

If available, capture:
- exact RealSense model
- firmware version
- whether the link is USB 3.x / SuperSpeed or falling back
- whether the camera is directly attached vs through a hub

PHASE 2 — Live Monitoring Before Reproduction
Set up watchers before reproducing the bug:
- a live kernel log monitor
- USB/udev event monitoring
- repeated device enumeration snapshots

Examples:
- sudo dmesg -wT
- sudo journalctl -kf
- sudo udevadm monitor --kernel --udev --property
- while true; do date; lsusb; echo; sleep 1; done
- while true; do date; rs-enumerate-devices || true; echo; sleep 2; done

Also enable verbose librealsense logging if possible:
- export LRS_LOG_LEVEL=DEBUG

PHASE 3 — Minimal Reproduction Harness
Create the smallest possible repro that:
1. opens the RealSense device
2. starts streaming
3. captures a few frames
4. calls the clean shutdown path
5. exits
6. immediately checks whether the camera still enumerates and can reopen

I want you to build BOTH:
A. a minimal Python repro using pyrealsense2
B. if needed, a minimal C++ repro using librealsense

The repro should:
- print timestamps for each stage
- print success/failure for:
  - context creation
  - device enumeration
  - pipeline.start()
  - frame acquisition
  - pipeline.stop()
  - destruction / exit
- after stop(), wait briefly and re-enumerate devices
- repeat this for multiple cycles (e.g., 10–50 iterations) to see whether the failure accumulates

Please also test variants:
- explicit pipeline.stop()
- object destruction without explicit stop (if safe and clearly labeled)
- closing all references before exit
- sleeping 1–3 seconds after stop
- using only depth stream
- using only color stream
- using depth+color
- low resolution / low FPS
- high resolution / higher FPS

For each variant, report:
- does USB enumeration survive?
- does rs-enumerate-devices still see the camera?
- can a second open/start succeed immediately?

PHASE 4 — Determine Whether This Is a USB-Level Disappearance
After reproducing the failure, explicitly determine which of the following happened:

Case A: Camera disappears from lsusb entirely
Case B: Camera remains in lsusb but rs-enumerate-devices fails
Case C: Camera remains visible and enumerable but streaming fails
Case D: Camera only returns after physical replug or port reset

For each case, gather evidence from:
- dmesg / journalctl
- lsusb / lsusb -t
- udev monitor output
- rs-enumerate-devices
- application logs
- lsof/fuser if a lingering process still holds the device

Please check for:
- xhci_hcd errors
- usb disconnect/reset/fail messages
- uvcvideo errors
- bandwidth/power negotiation issues
- runtime suspend/autosuspend state changes
- device authorization toggles
- endpoint stalls / transfer failures

PHASE 5 — Check Power Management and Runtime PM
Inspect likely runtime power-management contributors:
- /sys/bus/usb/devices/*/power/control
- autosuspend settings
- whether the device or hub is being suspended after stream stop
- whether the host controller/runtime PM changes state at process exit

If you find a likely PM issue, test the least invasive mitigation first and document the before/after result.

PHASE 6 — DGX Spark / ARM64 / Host-Specific Factors
Because this is a DGX Spark on ARM64, specifically consider:
- host USB controller behavior
- USB4/USB topology issues
- hub routing or port compatibility
- kernel module compatibility on ARM64
- whether the camera behaves differently on another host
- whether the same DGX Spark port behaves differently with another USB 3 device

If possible, compare:
- direct attach vs powered hub
- alternate cable
- alternate port
- another host machine

PHASE 7 — Safe Recovery Tests (Only After Evidence Is Collected)
If the device disappears or becomes unusable, attempt the safest recovery steps in escalating order, documenting which one restores function:
1. wait and re-enumerate
2. terminate any lingering process and re-enumerate
3. software re-open
4. USB device authorization toggle if available
5. USB unbind/bind for the affected device or controller (only if clearly identified)
6. module reload only if justified
7. physical replug
8. reboot as last resort

Do NOT apply risky recovery steps unless you clearly explain:
- what you are changing
- why it is likely relevant
- what evidence supports doing it

PHASE 8 — Code-Level Root Cause Analysis
From the repro and logs, determine whether my app is likely doing one of these:
- tearing down the pipeline incorrectly
- exiting before callbacks/threads are drained
- holding references to frames/sensors too long
- leaving background threads running
- double-stopping or double-destroying objects
- mixing recorder/pause semantics with stream-stop semantics
- failing to release contexts/sensors cleanly
- racing process exit against device shutdown

If you suspect application issues, provide:
- a minimal corrected shutdown sequence
- clear lifecycle rules
- exact code changes with explanation

PHASE 9 — Final Deliverable
Your final answer must include:

1. Executive summary
- one-paragraph explanation of the most likely root cause

2. Evidence table
- hypothesis
- evidence for
- evidence against
- confidence level

3. Reproduction result
- exact minimal repro
- whether the bug reproduced consistently
- what signal appears at failure time

4. Root cause classification
Choose one:
- application shutdown bug
- librealsense lifecycle bug
- USB power-management issue
- kernel/driver/controller issue
- cable/power/hub issue
- firmware/device issue
- unknown but narrowed to top 2 causes

5. Safest fix
- code-level fix if software
- host config mitigation if USB/runtime PM
- hardware/cable/powered hub mitigation if signal integrity/power
- firmware/package update only if strongly justified

6. Recovery playbook
- exact commands/steps to recover the camera when it becomes non-enumerable

7. Prevention playbook
- how to prevent this from happening again in a production workflow

Formatting requirements:
- Be explicit and methodical.
- Show commands before asking me to run them.
- Explain what each command is meant to prove.
- Prefer high-signal diagnostics over generic advice.
- Call out uncertainty honestly.
- Do not stop at “try another cable” unless the evidence points there.
- If a step depends on output from a previous step, say exactly what output pattern you are looking for.

Start by collecting the baseline inventory and setting up monitoring, then build the minimal repro harness.You are an expert Linux USB / kernel / librealsense / ARM64 debugging agent.

I need you to diagnose why my Intel RealSense camera on an NVIDIA DGX Spark appears to "stop" in a bad way when my program finishes, instead of cleanly pausing or shutting down. After that happens, the camera becomes unusable and is no longer recognized over USB until I physically reconnect it or otherwise recover it.

Environment and problem summary:
- Host: NVIDIA DGX Spark
- OS: DGX OS / Ubuntu-like Linux on ARM64
- Device: Intel RealSense camera
- Symptom:
  - Camera works initially
  - When my program finishes, the camera does not remain healthy
  - It seems to "stop" in a way that causes the USB device to disappear or become non-functional
  - Afterward, the camera may no longer show up in lsusb / rs-enumerate-devices / application code
- I want to know whether this is:
  1) an application shutdown bug
  2) a librealsense pipeline lifecycle issue
  3) a kernel/UVC/xHCI/USB controller problem
  4) USB power management / autosuspend / runtime PM
  5) cabling / power / hub / bandwidth instability
  6) a firmware or device fault
  7) a DGX Spark-specific compatibility or USB topology issue

Your goals:
1. Build a disciplined, evidence-driven differential diagnosis.
2. Reproduce the failure with the smallest possible test harness.
3. Determine exactly what changes at the moment the program exits:
   - process exits cleanly?
   - pipeline.stop() returns?
   - file descriptors remain open?
   - kernel logs a disconnect/reset/error?
   - USB device disappears from enumeration?
   - the device remains present but cannot stream?
4. Distinguish between:
   - “camera still enumerates but streaming fails”
   - “camera vanishes from USB enumeration entirely”
   - “camera remains on USB but RealSense APIs no longer see it”
5. Recommend the safest fix and recovery strategy.

Very important guardrails:
- Do NOT make destructive or irreversible changes first.
- Do NOT upgrade kernel/firmware/packages unless the evidence strongly justifies it and you clearly explain why.
- Prefer observation and minimally invasive tests first.
- If you need elevated privileges, explicitly say so.
- Keep a timestamped log of every command, output snippet, and inference.
- If you propose a reset/rebind/reload step, do it only after gathering pre-failure and post-failure evidence.
- Assume I may have sudo access but want to minimize reboots and physical reconnection.

Please work in this order:

PHASE 1 — Baseline Inventory
Collect and summarize:
- uname -a
- lsb_release -a or /etc/os-release
- architecture (uname -m)
- current kernel version
- installed RealSense packages / SDK version
- connected USB topology and negotiated speeds
- camera model, firmware version, serial, USB type descriptor if visible

Run commands like:
- uname -a
- uname -m
- cat /etc/os-release
- lsusb
- lsusb -t
- lsusb -nn | grep -i 8086 || true
- dmesg | tail -n 200
- journalctl -k -b --no-pager | tail -n 300
- rs-enumerate-devices || true
- realsense-viewer --version || true
- dpkg -l | grep -i realsense || true
- python3 -c "import pyrealsense2 as rs; print(rs.__version__)" || true

If available, capture:
- exact RealSense model
- firmware version
- whether the link is USB 3.x / SuperSpeed or falling back
- whether the camera is directly attached vs through a hub

PHASE 2 — Live Monitoring Before Reproduction
Set up watchers before reproducing the bug:
- a live kernel log monitor
- USB/udev event monitoring
- repeated device enumeration snapshots

Examples:
- sudo dmesg -wT
- sudo journalctl -kf
- sudo udevadm monitor --kernel --udev --property
- while true; do date; lsusb; echo; sleep 1; done
- while true; do date; rs-enumerate-devices || true; echo; sleep 2; done

Also enable verbose librealsense logging if possible:
- export LRS_LOG_LEVEL=DEBUG

PHASE 3 — Minimal Reproduction Harness
Create the smallest possible repro that:
1. opens the RealSense device
2. starts streaming
3. captures a few frames
4. calls the clean shutdown path
5. exits
6. immediately checks whether the camera still enumerates and can reopen

I want you to build BOTH:
A. a minimal Python repro using pyrealsense2
B. if needed, a minimal C++ repro using librealsense

The repro should:
- print timestamps for each stage
- print success/failure for:
  - context creation
  - device enumeration
  - pipeline.start()
  - frame acquisition
  - pipeline.stop()
  - destruction / exit
- after stop(), wait briefly and re-enumerate devices
- repeat this for multiple cycles (e.g., 10–50 iterations) to see whether the failure accumulates

Please also test variants:
- explicit pipeline.stop()
- object destruction without explicit stop (if safe and clearly labeled)
- closing all references before exit
- sleeping 1–3 seconds after stop
- using only depth stream
- using only color stream
- using depth+color
- low resolution / low FPS
- high resolution / higher FPS

For each variant, report:
- does USB enumeration survive?
- does rs-enumerate-devices still see the camera?
- can a second open/start succeed immediately?

PHASE 4 — Determine Whether This Is a USB-Level Disappearance
After reproducing the failure, explicitly determine which of the following happened:

Case A: Camera disappears from lsusb entirely
Case B: Camera remains in lsusb but rs-enumerate-devices fails
Case C: Camera remains visible and enumerable but streaming fails
Case D: Camera only returns after physical replug or port reset

For each case, gather evidence from:
- dmesg / journalctl
- lsusb / lsusb -t
- udev monitor output
- rs-enumerate-devices
- application logs
- lsof/fuser if a lingering process still holds the device

Please check for:
- xhci_hcd errors
- usb disconnect/reset/fail messages
- uvcvideo errors
- bandwidth/power negotiation issues
- runtime suspend/autosuspend state changes
- device authorization toggles
- endpoint stalls / transfer failures

PHASE 5 — Check Power Management and Runtime PM
Inspect likely runtime power-management contributors:
- /sys/bus/usb/devices/*/power/control
- autosuspend settings
- whether the device or hub is being suspended after stream stop
- whether the host controller/runtime PM changes state at process exit

If you find a likely PM issue, test the least invasive mitigation first and document the before/after result.

PHASE 6 — DGX Spark / ARM64 / Host-Specific Factors
Because this is a DGX Spark on ARM64, specifically consider:
- host USB controller behavior
- USB4/USB topology issues
- hub routing or port compatibility
- kernel module compatibility on ARM64
- whether the camera behaves differently on another host
- whether the same DGX Spark port behaves differently with another USB 3 device

If possible, compare:
- direct attach vs powered hub
- alternate cable
- alternate port
- another host machine

PHASE 7 — Safe Recovery Tests (Only After Evidence Is Collected)
If the device disappears or becomes unusable, attempt the safest recovery steps in escalating order, documenting which one restores function:
1. wait and re-enumerate
2. terminate any lingering process and re-enumerate
3. software re-open
4. USB device authorization toggle if available
5. USB unbind/bind for the affected device or controller (only if clearly identified)
6. module reload only if justified
7. physical replug
8. reboot as last resort

Do NOT apply risky recovery steps unless you clearly explain:
- what you are changing
- why it is likely relevant
- what evidence supports doing it

PHASE 8 — Code-Level Root Cause Analysis
From the repro and logs, determine whether my app is likely doing one of these:
- tearing down the pipeline incorrectly
- exiting before callbacks/threads are drained
- holding references to frames/sensors too long
- leaving background threads running
- double-stopping or double-destroying objects
- mixing recorder/pause semantics with stream-stop semantics
- failing to release contexts/sensors cleanly
- racing process exit against device shutdown

If you suspect application issues, provide:
- a minimal corrected shutdown sequence
- clear lifecycle rules
- exact code changes with explanation

PHASE 9 — Final Deliverable
Your final answer must include:

1. Executive summary
- one-paragraph explanation of the most likely root cause

2. Evidence table
- hypothesis
- evidence for
- evidence against
- confidence level

3. Reproduction result
- exact minimal repro
- whether the bug reproduced consistently
- what signal appears at failure time

4. Root cause classification
Choose one:
- application shutdown bug
- librealsense lifecycle bug
- USB power-management issue
- kernel/driver/controller issue
- cable/power/hub issue
- firmware/device issue
- unknown but narrowed to top 2 causes

5. Safest fix
- code-level fix if software
- host config mitigation if USB/runtime PM
- hardware/cable/powered hub mitigation if signal integrity/power
- firmware/package update only if strongly justified

6. Recovery playbook
- exact commands/steps to recover the camera when it becomes non-enumerable

7. Prevention playbook
- how to prevent this from happening again in a production workflow

Formatting requirements:
- Be explicit and methodical.
- Show commands before asking me to run them.
- Explain what each command is meant to prove.
- Prefer high-signal diagnostics over generic advice.
- Call out uncertainty honestly.
- Do not stop at “try another cable” unless the evidence points there.
- If a step depends on output from a previous step, say exactly what output pattern you are looking for.

Start by collecting the baseline inventory and setting up monitoring, then build the minimal repro harness.


================================================================================
[2026-06-05] DESKTOP VALIDATION TOOLING — Desktop Actions for the GB10 viewer entry
================================================================================
(Appended by tooling-update session. All content above this banner is the original
hand-written debugging brief and is preserved unchanged.)

WHAT CHANGED
------------
The single Desktop launcher entry now carries DEBUG ACTIONS so an operator can
launch and debug the updated GB10 code paths from one right-click menu, instead of
needing a terminal + the justfile. Two files were kept byte-identical and in sync:
  - DEPLOYED : ~/Desktop/GB10-RealSense-Viewer.desktop
  - TEMPLATE : ~/dev/repos/librealsense/scripts/gb10/gb10-realsense-viewer.desktop

The main entry is UNCHANGED in behaviour: double-clicking still runs the interactive
python viewer (scripts/gb10/gb10-viewer-launch.sh, Terminal=true). Right-click the
icon to get the new sub-actions:

  Actions=Doctor;KeepOnGPU;Viewer313;

NEW DESKTOP ACTIONS (right-click sub-menu)
------------------------------------------
1) Diagnostics (Doctor) — camera-SAFE preflight  [USE THIS FIRST]
   Exec=bash -c "/home/damartel/dev/repos/librealsense/scripts/gb10/gb10-doctor.sh; read -p 'Press Enter to close...' _"
   Runs gb10-doctor.sh. Opens NO device — only checks USB presence + the full
   runtime (venv python, pyrealsense2 build/import, cv2 = opencv+ffmpeg libs, NVENC
   ffmpeg, librealsense2-gl, DISPLAY, and the controller HC-died journal guard).
   PASS/WARN/FAIL. The `read` holds the terminal open so you can read the report.

2) Keep-on-GPU viewer (GL-resident render path)
   Exec=bash -c "/home/damartel/dev/repos/librealsense/scripts/gb10/rs-gb10-keepongpu-build.sh && DISPLAY=${DISPLAY:-:1} /home/damartel/realsense-gb10-validation/rs-gb10-keepongpu-viewer; read -p 'Press Enter to close...' _"
   Exactly replicates `just hil-keepongpu`: builds the C++ keep-on-GPU viewer
   binary (rs-gb10-keepongpu-build.sh -> $HOME/realsense-gb10-validation/
   rs-gb10-keepongpu-viewer, against the installed GL SDK), then runs it with
   DISPLAY (defaults to :1, same as the justfile). This is the GL-resident path
   where frames stay on the GPU. NOTE: this DOES open the camera — run Doctor first.

3) Viewer (py3.13 — UNVERIFIED live streaming)
   Exec=env LRS_PY_TAG=python3.13 /home/damartel/dev/repos/librealsense/scripts/gb10/gb10-viewer-launch.sh
   Same interactive launcher, but LRS_PY_TAG=python3.13 retargets the whole stack
   in one var via gb10-env.sh: python3.13 -> build-gb10-py313 (the cpython-313
   pyrealsense2 .so) + the .venv313 interpreter. gb10-env.sh's loud ABI guard fires
   if the resolved venv's python minor != the tag (a 3.13 interpreter loading a 3.12
   .so dies with `undefined symbol _PyThreadState_*`), so a bad retarget is caught
   loudly rather than as a cryptic import error.

PY3.13 RETARGET — STATUS & cv2 CAVEAT
-------------------------------------
  - The flip is ONE variable: LRS_PY_TAG=python3.13. It moves build tree + venv +
    opencv site-packages together (see gb10-env.sh case stmt).
  - UNVERIFIED: 3.13 live STREAMING has only been OFFLINE-verified this session
    (env flip + ABI-guard logic + .desktop syntax). No device was opened.
  - cv2 on py3.13 is STOCK opencv-python 4.11 (pip wheel). The CUDA-accelerated
    OpenCV build (/opt/gb10-cuda/install/opencv) is python3.12-only — its cv2 stays
    on 3.12. So display/quality tools that need CUDA-OpenCV must run under 3.12; the
    3.13 path gets plain opencv-python.

.DESKTOP SYNTAX VALIDATION (offline, no camera)
-----------------------------------------------
  - desktop-file-validate run on BOTH files: NO errors, NO warnings. Only a benign
    "more than one main category" HINT (Development;Graphics;AudioVideo;Video — a
    dev+graphics+video tool legitimately spans menus). Added AudioVideo to clear the
    prior "Video requires AudioVideo" error that pre-existed in the entry.
  - Exec quoting follows the Desktop Entry spec, NOT shell rules: only DOUBLE quotes
    group an arg; ; $ & ` * ? ( ) are reserved. Each `bash -c "..."` therefore uses
    an OUTER double quote, INNER single quotes for the read prompt (literal inside
    ""), and \$ to pass a literal $ to bash (the .desktop file stores DISPLAY=\${...},
    bash receives DISPLAY=${DISPLAY:-:1}).
  - Each id in Actions= has a matching [Desktop Action <id>] group and vice-versa.
  - `bash -n` on the Doctor and KeepOnGPU scriptlets: syntax OK.
  - Terminal=true lives on the main [Desktop Entry] (it applies to the actions); it
    is NOT repeated in action groups (not a valid action-group key). The trailing
    `read` is a belt-and-braces hold in case a launcher does not allocate a terminal
    for an action, so output is not lost.

HOW TO DEBUG A BAD LAUNCH
-------------------------
  1. Run Diagnostics (Doctor) FIRST — it is camera-safe and tells you exactly which
     runtime piece is missing (venv / pyrealsense2 ABI / cv2 libs / DISPLAY / NVENC /
     GL SDK) and whether the USB controller shows a prior HC-died (=> REBOOT before
     touching the camera).
  2. If Doctor is HEALTHY and a viewer still fails, the launcher's own preflight
     (gb10-viewer-launch.sh) will print a friendly "!! Missing runtime: ..." line and
     hold for Enter — read that line.
  3. For the py3.13 action, an ABI-MISMATCH banner from gb10-env.sh means LRS_VENV
     and LRS_PY_TAG disagree — point LRS_VENV at a python3.13 interpreter (.venv313).
  4. Equivalent CLI fallbacks (no .desktop needed):
       just gb10-doctor
       just hil-keepongpu
       LRS_PY_TAG=python3.13 scripts/gb10/gb10-viewer-launch.sh
