#!/usr/bin/env python3
"""Read, set and revert the host state levers that govern latency determinism.

WHY THIS EXISTS
---------------
Operators perceive randomly timed visual and audio events far more readily than
they perceive throughput, so the levers that matter most on these hosts are the
ones that make timing *predictable*: CPU idle states, GPU clock floors, timer
migration, IRQ placement. Those live in five different places with five
different interfaces, none of which reverts itself, and all of which are
host-wide -- they affect the vLLM router, the aarch64 CI runner and any fleet
worker sharing the box, not just the process being tuned.

So this tool is deliberately conservative:

  * it is DRY-RUN by default; `--apply` is required to change anything
  * it snapshots prior state to JSON BEFORE the first write, so `revert` is
    always possible even if the session dies midway
  * it refuses to apply while co-tenants are running unless forced, because
    these settings are not scoped to the caller
  * every write is READ BACK and reported as applied / ignored / failed

That last point is the important one. Several of these interfaces accept a
write and silently do nothing -- and this fleet has already shipped one false
green (a stress entry that delivered zero frames and reported PASS). A tuning
tool that reports "applied" without confirming the value took is the same
defect wearing different clothes.

MEASURED CAVEAT, GB10 SPECIFIC
------------------------------
`nvidia-smi -lgc <max>,<max>` is ACCEPTED on GB10 and does move the clock, but
it does NOT reach the requested ceiling: on spark-0060 the observed SM clock
went 2411 -> 2574 MHz against a reported max of 3003, and `-rgc` reverted
cleanly to 2411. So the lever is real but partial, and this tool reports the
delta it actually achieved rather than the value it asked for.

USAGE
    rs-gb10-runtime-profile.py show
    rs-gb10-runtime-profile.py apply determinism          # dry run
    rs-gb10-runtime-profile.py apply determinism --apply
    rs-gb10-runtime-profile.py revert --apply
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

SNAPSHOT_DEFAULT = pathlib.Path.home() / ".local/state/vigil/gb10-runtime-profile.json"

#: Processes whose presence means these host-wide changes are not the caller's
#: alone to make. Matched against the full command line.
CO_TENANT_PATTERNS = (
    ("vllm", "vLLM inference server"),
    ("Runner.Listener", "GitHub Actions self-hosted runner"),
    ("run_fleet_worker", "vigil fleet worker"),
    ("sam3_sidecar", "SAM3 sidecar"),
)


def _run(cmd: list[str], *, timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def _read(path: str) -> str | None:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_privileged(path: str, value: str) -> tuple[bool, str]:
    """Write via sudo -n, then READ BACK. Never trust the exit code alone."""
    rc, out = _run(["sudo", "-n", "sh", "-c", f"echo {value} > {path}"])
    if rc != 0:
        return False, out or "sudo write failed"
    observed = _read(path)
    if observed is None:
        return False, "unreadable after write"
    if observed.strip() != value:
        # Accepted the write and kept its own value: the interface is advisory
        # here. Reporting this as success is exactly the false green this tool
        # exists to avoid.
        return False, f"write ignored (asked {value}, still {observed})"
    return True, observed


# --------------------------------------------------------------------------
# levers
# --------------------------------------------------------------------------


def cpuidle_state() -> dict:
    files = sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpuidle/state*/disable"))
    values = [_read(f) for f in files]
    disabled = sum(1 for v in values if v == "1")
    return {
        "lever": "cpu_idle_states",
        "files": len(files),
        "disabled": disabled,
        "enabled": len(files) - disabled,
        "summary": (
            "no cpuidle interface"
            if not files
            else f"{disabled}/{len(files)} idle states disabled"
        ),
    }


def gpu_clock_state() -> dict:
    if not shutil.which("nvidia-smi"):
        return {"lever": "gpu_clocks", "summary": "nvidia-smi absent"}
    rc, out = _run(
        [
            "nvidia-smi",
            "--query-gpu=clocks.sm,clocks.max.sm,persistence_mode",
            "--format=csv,noheader,nounits",
        ]
    )
    if rc != 0:
        return {"lever": "gpu_clocks", "summary": f"query failed: {out[:60]}"}
    first = out.splitlines()[0] if out.splitlines() else ""
    parts = [p.strip() for p in first.split(",")]
    return {
        "lever": "gpu_clocks",
        "sm_mhz": parts[0] if parts else None,
        "max_mhz": parts[1] if len(parts) > 1 else None,
        "persistence": parts[2] if len(parts) > 2 else None,
        "summary": f"SM {parts[0] if parts else '?'} of max "
        f"{parts[1] if len(parts) > 1 else '?'} MHz",
    }


def timer_migration_state() -> dict:
    value = _read("/proc/sys/kernel/timer_migration")
    return {
        "lever": "timer_migration",
        "value": value,
        "summary": f"timer_migration={value}"
        + (" (migrating timers add jitter)" if value == "1" else ""),
    }


def irqbalance_state() -> dict:
    _rc, out = _run(["systemctl", "is-active", "irqbalance"])
    return {
        "lever": "irqbalance",
        "value": out or "unknown",
        "summary": f"irqbalance {out}",
    }


def governor_state() -> dict:
    files = sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"))
    values = {_read(f) for f in files}
    return {
        "lever": "cpu_governor",
        "values": sorted(v for v in values if v),
        "summary": "governor "
        + (",".join(sorted(v for v in values if v)) or "unknown"),
    }


LEVERS = (
    cpuidle_state,
    gpu_clock_state,
    timer_migration_state,
    irqbalance_state,
    governor_state,
)


def snapshot_state() -> dict:
    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": os.uname().nodename,
        "levers": [fn() for fn in LEVERS],
        "cpuidle_disable_files": {
            f: _read(f)
            for f in sorted(
                glob.glob("/sys/devices/system/cpu/cpu*/cpuidle/state*/disable")
            )
        },
    }


def detect_co_tenants() -> list[str]:
    rc, out = _run(["ps", "-eo", "cmd"])
    if rc != 0:
        return ["<could not enumerate processes>"]
    found = []
    for pattern, label in CO_TENANT_PATTERNS:
        if any(pattern in line for line in out.splitlines()):
            found.append(label)
    return found


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------


def plan_determinism() -> list[tuple[str, str, str]]:
    """(description, kind, argument) triples. Nothing here executes."""
    actions: list[tuple[str, str, str]] = []
    for f in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpuidle/state*/disable")):
        if _read(f) != "1":
            actions.append((f"disable idle state {f}", "sysfs", f))
    if _read("/proc/sys/kernel/timer_migration") != "0":
        actions.append(
            (
                "stop migrating timers between cpus",
                "sysctl",
                "/proc/sys/kernel/timer_migration",
            )
        )
    if shutil.which("nvidia-smi"):
        actions.append(("lock GPU clocks to reported max", "gpu_lock", ""))
    _rc, out = _run(["systemctl", "is-active", "irqbalance"])
    if out == "active":
        actions.append(
            ("stop irqbalance (pin IRQ placement)", "service_stop", "irqbalance")
        )
    return actions


def apply_determinism(*, do_apply: bool) -> int:
    actions = plan_determinism()
    if not actions:
        print("determinism profile: already fully applied, nothing to do")
        return 0

    print(f"determinism profile: {len(actions)} change(s)")
    for desc, kind, _arg in actions:
        print(f"  [{kind}] {desc}")
    if not do_apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply to make these changes.")
        return 0

    failures = 0
    for desc, kind, arg in actions:
        if kind in ("sysfs", "sysctl"):
            value = "1" if kind == "sysfs" else "0"
            ok, detail = _write_privileged(arg, value)
        elif kind == "gpu_lock":
            rc, out = _run(
                [
                    "nvidia-smi",
                    "--query-gpu=clocks.max.sm",
                    "--format=csv,noheader,nounits",
                ]
            )
            maximum = out.splitlines()[0].strip() if rc == 0 and out else ""
            if not maximum:
                ok, detail = False, "could not read max clock"
            else:
                before = gpu_clock_state().get("sm_mhz")
                rc, out = _run(
                    ["sudo", "-n", "nvidia-smi", "-lgc", f"{maximum},{maximum}"]
                )
                time.sleep(2.0)
                after = gpu_clock_state().get("sm_mhz")
                # GB10 honours -lgc partially: report the delta achieved, never
                # the value requested.
                ok = rc == 0
                detail = f"asked {maximum}, SM {before} -> {after}"
        elif kind == "service_stop":
            rc, out = _run(["sudo", "-n", "systemctl", "stop", arg])
            ok, detail = rc == 0, out or "stopped"
        else:
            ok, detail = False, f"unknown action kind {kind}"

        print(f"  {'OK  ' if ok else 'FAIL'} {desc}: {detail}")
        failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} change(s) did not take effect - state is now MIXED.")
        print("Run `revert --apply` to return to the snapshot.")
    return 1 if failures else 0


def revert(snapshot: dict, *, do_apply: bool) -> int:
    saved = snapshot.get("cpuidle_disable_files", {})
    actions = [(f, v) for f, v in saved.items() if _read(f) != v]
    gpu_needed = shutil.which("nvidia-smi") is not None

    print(
        f"revert to snapshot from {snapshot.get('captured_at')} "
        f"on {snapshot.get('host')}"
    )
    print(f"  {len(actions)} cpuidle file(s) to restore")
    if gpu_needed:
        print("  reset GPU clocks (nvidia-smi -rgc)")
    prior_tm = next(
        (
            x.get("value")
            for x in snapshot.get("levers", [])
            if x.get("lever") == "timer_migration"
        ),
        None,
    )
    if prior_tm is not None and _read("/proc/sys/kernel/timer_migration") != prior_tm:
        print(f"  restore timer_migration={prior_tm}")
    if not do_apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply.")
        return 0

    failures = 0
    for f, value in actions:
        ok, detail = _write_privileged(f, value or "0")
        if not ok:
            failures += 1
            print(f"  FAIL {f}: {detail}")
    if prior_tm is not None:
        ok, detail = _write_privileged("/proc/sys/kernel/timer_migration", prior_tm)
        if not ok:
            failures += 1
            print(f"  FAIL timer_migration: {detail}")
    if gpu_needed:
        rc, out = _run(["sudo", "-n", "nvidia-smi", "-rgc"])
        if rc != 0:
            failures += 1
            print(f"  FAIL gpu reset: {out[:80]}")
    print(f"revert complete, {failures} failure(s)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the current state of every lever")
    ap = sub.add_parser("apply", help="apply a named profile")
    ap.add_argument("profile", choices=["determinism"])
    rp = sub.add_parser("revert", help="restore the recorded snapshot")
    for p in (ap, rp):
        p.add_argument("--apply", dest="do_apply", action="store_true")
        p.add_argument(
            "--force", action="store_true", help="proceed despite co-tenants"
        )
        p.add_argument("--snapshot", type=pathlib.Path, default=SNAPSHOT_DEFAULT)
    args = parser.parse_args()

    if args.command == "show":
        state = snapshot_state()
        print(f"host {state['host']}  {state['captured_at']}")
        for lever in state["levers"]:
            print(f"  {lever['lever']:20s} {lever['summary']}")
        tenants = detect_co_tenants()
        print("  co-tenants: " + (", ".join(tenants) if tenants else "none detected"))
        return 0

    tenants = detect_co_tenants()
    if tenants and not args.force:
        print("REFUSING: these levers are host-wide and other tenants are running:")
        for t in tenants:
            print(f"  - {t}")
        print(
            "\nChanging idle states, timer migration or IRQ placement affects\n"
            "them too. Coordinate first, then re-run with --force if that is\n"
            "genuinely intended."
        )
        return 3

    if args.command == "apply":
        # Snapshot BEFORE the first write, so revert survives a dead session.
        if args.do_apply:
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            args.snapshot.write_text(
                json.dumps(snapshot_state(), indent=2), encoding="utf-8"
            )
            print(f"snapshot written to {args.snapshot}")
        return apply_determinism(do_apply=args.do_apply)

    if not args.snapshot.exists():
        print(f"no snapshot at {args.snapshot} - nothing to revert to")
        return 2
    return revert(
        json.loads(args.snapshot.read_text(encoding="utf-8")), do_apply=args.do_apply
    )


if __name__ == "__main__":
    sys.exit(main())
