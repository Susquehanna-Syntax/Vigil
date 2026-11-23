"""``vigil-agent measure``: what this agent costs the machine it runs on.

Run it from source or from the released binary, on any platform; it measures
the process it is. Nothing is sent anywhere and no config is read: it runs the
same local collection a check-in runs, and the software scan, and reports

  * the size on disk of what was installed, and what a one-file build unpacks
    at each start (a frozen build only),
  * how long the process took to start,
  * memory at rest, after a check-in's collection has run,
  * the wall and CPU time of one check-in's collection (the first, which pays
    for every lazy import, and the median of the rest),
  * the wall time, CPU time and peak memory of a software inventory scan.

Sprint M8 starts here: every later change to the agent (another packager, a
persistent connection, a new collector) is judged against these numbers. The
release build runs this on each platform and docs/agent-footprint.md keeps the
table.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import statistics
import sys
import threading
import time
from pathlib import Path

import psutil

from . import __version__

#: Warm check-in passes measured after the cold one.
PASSES = 5
#: The pause between them: just past the CPU collector's shortest window.
PASS_GAP_SECONDS = 1.05
#: How often the peak-memory sampler looks. A scan runs for seconds, so this
#: catches its peak without the sampler becoming the thing measured.
SAMPLE_SECONDS = 0.02


def _rss(process: psutil.Process) -> int:
    """This process and its children: a scan's memory is mostly the package
    manager it runs, and the machine pays for that too."""
    total = process.memory_info().rss
    for child in process.children(recursive=True):
        try:
            total += child.memory_info().rss
        except psutil.Error:
            continue
    return total


def _cpu(process: psutil.Process) -> float:
    """User plus system seconds, children that have exited included."""
    times = process.cpu_times()
    return (times.user + times.system
            + getattr(times, "children_user", 0.0) + getattr(times, "children_system", 0.0))


class _Peak:
    """The highest RSS seen while the block runs."""

    def __init__(self, process: psutil.Process):
        self._process = process
        self._stop = threading.Event()
        self.bytes = 0
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self.bytes = max(self.bytes, _rss(self._process))
            except psutil.Error:
                pass
            self._stop.wait(SAMPLE_SECONDS)

    def __enter__(self):
        self.bytes = _rss(self._process)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self.bytes = max(self.bytes, _rss(self._process))


def _timed(process: psutil.Process, fn) -> dict:
    cpu, wall = _cpu(process), time.perf_counter()
    try:
        fn()
        error = ""
    except Exception as exc:  # a collector that cannot run here still costs what it cost
        error = f"{type(exc).__name__}: {exc}"
    result = {"wall_seconds": round(time.perf_counter() - wall, 4),
              "cpu_seconds": round(_cpu(process) - cpu, 4)}
    if error:
        result["error"] = error
    return result


def installed_bytes() -> int | None:
    """What a frozen build put on disk: the one file, or the whole folder of a
    one-folder build. None from source, where the answer would be the size of
    a Python install."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    if (exe.parent / "_internal").is_dir():  # PyInstaller one-folder
        return sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
    return exe.stat().st_size


def _launched(process: psutil.Process) -> float:
    """When the agent was launched. A one-file build is two processes: a
    loader that unpacks the bundle, then this one. The unpacking is start-up
    time the service pays on every boot, so the clock starts at the loader."""
    started = process.create_time()
    if getattr(sys, "frozen", False):
        try:
            parent = process.parent()
            if parent is not None and parent.exe() == process.exe():
                started = parent.create_time()
        except psutil.Error:
            pass
    return started


def unpacked_bytes() -> int | None:
    """What a one-file build unpacks into a temporary folder on every start,
    on top of its installed size. None for any other build."""
    bundle = getattr(sys, "_MEIPASS", None)
    if not bundle or Path(bundle) == Path(sys.executable).parent / "_internal":
        return None
    return sum(f.stat().st_size for f in Path(bundle).rglob("*") if f.is_file())


def _checkin_collection() -> None:
    """Everything the check-in loop gathers locally on an ordinary pass. The
    hourly inventory, the software list and the registry lookups for image
    updates have their own, slower clocks and are not part of it."""
    from . import collector, netmap

    collector.collect_all(None)
    containers = collector.collect_docker_containers()
    if containers is not None:
        collector.collect_docker_resources()
    collector.reboot_required()
    netmap.collect_network()


def measure(*, software: bool = True, passes: int = PASSES) -> dict:
    process = psutil.Process()
    startup = max(0.0, time.time() - _launched(process))

    cold = _timed(process, _checkin_collection)
    warm = []
    for _ in range(passes):
        # A real agent's passes are a check-in interval apart. Run back to
        # back, each would look like a first reading to the CPU collector and
        # take its one-second sample, which no later check-in pays.
        time.sleep(PASS_GAP_SECONDS)
        warm.append(_timed(process, _checkin_collection))
    gc.collect()
    idle = _rss(process)

    report = {
        "agent_version": __version__,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "installed_bytes": installed_bytes(),
        "unpacked_bytes": unpacked_bytes(),
        "startup_seconds": round(startup, 3),
        "idle_rss_bytes": idle,
        "checkin_cold": cold,
        "checkin_warm": {
            "passes": passes,
            "wall_seconds": round(statistics.median(p["wall_seconds"] for p in warm), 4),
            "cpu_seconds": round(statistics.median(p["cpu_seconds"] for p in warm), 4),
        },
    }
    if software:
        from . import software as software_scan

        with _Peak(process) as peak:
            scan = _timed(process, software_scan.collect)
        scan["peak_rss_bytes"] = peak.bytes
        report["software_scan"] = scan
    return report


def _mb(value) -> str:
    return "-" if value is None else f"{value / 1048576:.1f} MB"


def markdown_row(report: dict) -> str:
    """One row of the table in docs/agent-footprint.md."""
    scan = report.get("software_scan") or {}
    warm = report["checkin_warm"]
    return " | ".join([
        "| " + report["platform"],
        "binary" if report["frozen"] else "source",
        _mb(report["installed_bytes"]),
        f"{report['startup_seconds']:.2f} s",
        _mb(report["idle_rss_bytes"]),
        f"{warm['cpu_seconds'] * 1000:.0f} ms",
        f"{warm['wall_seconds'] * 1000:.0f} ms",
        f"{scan['wall_seconds']:.1f} s" if scan else "-",
        _mb(scan.get("peak_rss_bytes")),
    ]) + " |"


def render(report: dict) -> str:
    scan = report.get("software_scan")
    warm, cold = report["checkin_warm"], report["checkin_cold"]
    lines = [
        f"Vigil agent {report['agent_version']} on {report['platform']} "
        f"(Python {report['python']}, {'binary' if report['frozen'] else 'from source'})",
        f"  Installed size        {_mb(report['installed_bytes'])}"
        + (f", unpacking to {_mb(report['unpacked_bytes'])} at each start"
           if report.get("unpacked_bytes") else ""),
        f"  Start-up              {report['startup_seconds']:.2f} s",
        f"  Memory at rest        {_mb(report['idle_rss_bytes'])}",
        f"  First check-in        {cold['wall_seconds'] * 1000:.0f} ms wall, "
        f"{cold['cpu_seconds'] * 1000:.0f} ms CPU",
        f"  Each check-in after   {warm['wall_seconds'] * 1000:.0f} ms wall, "
        f"{warm['cpu_seconds'] * 1000:.0f} ms CPU (median of {warm['passes']})",
    ]
    if scan:
        lines.append(f"  Software scan         {scan['wall_seconds']:.1f} s wall, "
                     f"{scan['cpu_seconds']:.1f} s CPU, peak memory {_mb(scan['peak_rss_bytes'])}"
                     + (f" ({scan['error']})" if scan.get("error") else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="vigil-agent measure",
        description="Measure what this agent costs this machine. Sends nothing, reads no config.")
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    parser.add_argument("--row", action="store_true",
                        help="print one Markdown table row (for docs/agent-footprint.md)")
    parser.add_argument("--no-software", action="store_true",
                        help="skip the software inventory scan (it can take a minute)")
    args = parser.parse_args(argv)
    report = measure(software=not args.no_software)
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.row:
        print(markdown_row(report))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
