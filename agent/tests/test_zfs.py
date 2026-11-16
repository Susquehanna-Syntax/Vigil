"""ZFS pool health from zpool list and zpool status, run against recorded output.

The test machine has no ZFS, so every case feeds the collector text recorded
from OpenZFS (the same format on FreeBSD and Linux). A host without zpool is a
silent empty result.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import subprocess as _subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import collector

# The reference time for scrub ages: a Monday, a week after the zroot scrub.
NOW = datetime(2026, 11, 16, 12, 0, 0)

# zpool list -Hp -o name,size,alloc,free,frag,cap,health
# Tab-separated, no header. tank has no fragmentation figure, which is "-".
LIST_HEALTHY = (
    "zroot\t499074813952\t96223367168\t402851446784\t4\t19\tONLINE\n"
    "tank\t1000000000000\t470000000000\t530000000000\t-\t47\tDEGRADED\n"
)

# zpool status -p. tank has a faulted disk and errors; the pool and raidz rows
# repeat their children's counts (6/5/12), so the leaf total is 23, not 69.
STATUS_HEALTHY = "\n".join([
    "  pool: tank",
    " state: DEGRADED",
    "status: One or more devices are faulted in response to persistent errors.",
    "\tSufficient replicas exist for the pool to continue functioning in a",
    "\tdegraded state.",
    "action: Replace the faulted device, and run 'zpool clear'.",
    "  scan: resilvered 1.2G in 00:10:00 with 0 errors on Mon Nov 10 03:00:00 2026",
    "config:",
    "",
    "\tNAME        STATE     READ WRITE CKSUM",
    "\ttank        DEGRADED     6     5    12",
    "\t  raidz1-0  DEGRADED     6     5    12",
    "\t    da0     FAULTED      5     3    12  too many errors",
    "\t    da1     ONLINE       1     0     0",
    "\t    da2     ONLINE       0     2     0",
    "",
    "errors: No known data errors",
    "",
    "  pool: zroot",
    " state: ONLINE",
    "  scan: scrub repaired 0B in 00:01:23 with 0 errors on Sun Nov  9 00:25:24 2026",
    "config:",
    "",
    "\tNAME        STATE     READ WRITE CKSUM",
    "\tzroot       ONLINE       0     0     0",
    "\t  mirror-0  ONLINE       0     0     0",
    "\t    nvme0p3 ONLINE       0     0     0",
    "\t    nvme1p3 ONLINE       0     0     0",
    "",
    "errors: No known data errors",
    "",
])

# One pool, with the scan line filled in per case.
LIST_ONE = "tank\t1000\t500\t500\t-\t50\tONLINE\n"
STATUS_ONE_TEMPLATE = "\n".join([
    "  pool: tank",
    " state: ONLINE",
    "  scan: {scan}",
    "config:",
    "",
    "\tNAME        STATE     READ WRITE CKSUM",
    "\ttank        ONLINE       0     0     0",
    "\t  da0       ONLINE       0     0     0",
    "",
    "errors: No known data errors",
    "",
])


def _status_one(scan: str) -> str:
    return STATUS_ONE_TEMPLATE.format(scan=scan)


def _proc(stdout="", returncode=0, stderr=""):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def _zpool(list_result, status_result):
    """A subprocess.run stand-in: each result is text, a MagicMock, or an exception to raise."""
    def run(argv, **_kwargs):
        result = list_result if argv[1] == "list" else status_result
        if isinstance(result, BaseException):
            raise result
        return result
    return run


def _summary(points):
    return sorted(
        (p["metric"], p["value"], tuple(sorted(p["labels"].items()))) for p in points
    )


def _pool(name, **extra):
    return (name, tuple(sorted({"pool": name, **extra}.items())))


class ZfsTests(unittest.TestCase):
    def test_zpool_absent_returns_empty_and_never_runs_it(self):
        with patch.object(collector.shutil, "which", return_value=None), \
             patch.object(collector.subprocess, "run") as run:
            self.assertEqual(collector.collect_zfs(now=NOW), [])
        run.assert_not_called()

    def test_healthy_two_pool_system_gives_exact_points(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_HEALTHY), _proc(STATUS_HEALTHY))):
            points = collector.collect_zfs(now=NOW)
        zroot = {"pool": "zroot"}
        tank = {"pool": "tank"}
        expected = [
            ("pool_size_bytes", 499074813952, tuple(sorted(zroot.items()))),
            ("pool_used_bytes", 96223367168, tuple(sorted(zroot.items()))),
            ("pool_free_bytes", 402851446784, tuple(sorted(zroot.items()))),
            ("pool_capacity_percent", 19, tuple(sorted(zroot.items()))),
            ("pool_fragmentation_percent", 4, tuple(sorted(zroot.items()))),
            ("pool_healthy", 1, tuple(sorted(zroot.items()))),
            ("pool_state", 0, tuple(sorted({"pool": "zroot", "state": "ONLINE"}.items()))),
            ("pool_errors", 0, tuple(sorted(zroot.items()))),
            ("scrub_age_days", 7.5, tuple(sorted(zroot.items()))),
            ("pool_size_bytes", 1000000000000, tuple(sorted(tank.items()))),
            ("pool_used_bytes", 470000000000, tuple(sorted(tank.items()))),
            ("pool_free_bytes", 530000000000, tuple(sorted(tank.items()))),
            ("pool_capacity_percent", 47, tuple(sorted(tank.items()))),
            ("pool_healthy", 0, tuple(sorted(tank.items()))),
            ("pool_state", 1, tuple(sorted({"pool": "tank", "state": "DEGRADED"}.items()))),
            ("pool_errors", 23, tuple(sorted(tank.items()))),
            # Unlabelled host-level summary over both pools.
            ("pools_unhealthy", 1, ()),
            ("max_capacity_percent", 47, ()),
            ("total_errors", 23, ()),
            ("max_scrub_age_days", 7.5, ()),
        ]
        self.assertEqual(_summary(points), sorted(expected))
        for point in points:
            self.assertEqual(point["category"], "zfs")

    def test_frag_of_dash_emits_no_fragmentation_point(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE), _proc(_status_one("none requested")))):
            points = collector.collect_zfs(now=NOW)
        self.assertNotIn("pool_fragmentation_percent", [p["metric"] for p in points])

    def test_scrub_repaired_gives_age_in_days(self):
        scan = "scrub repaired 0B in 00:01:23 with 0 errors on Sun Nov  9 00:25:24 2026"
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE), _proc(_status_one(scan)))):
            points = collector.collect_zfs(now=NOW)
        ages = [(p["value"], p["labels"]) for p in points if p["metric"] == "scrub_age_days"]
        # Nov 9 00:25:24 to Nov 16 12:00:00 is 7 days 11h34m36s, i.e. 7.48 days.
        self.assertEqual(ages, [(7.5, {"pool": "tank"})])
        self.assertNotIn("scrub_in_progress", [p["metric"] for p in points])

    def test_scrub_in_progress_gives_flag_and_no_age(self):
        scan = "scrub in progress since Sun Nov 16 10:00:00 2026"
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE), _proc(_status_one(scan)))):
            points = collector.collect_zfs(now=NOW)
        scrub = [(p["metric"], p["value"], p["labels"]) for p in points
                 if p["metric"].startswith("scrub")]
        self.assertEqual(scrub, [("scrub_in_progress", 1, {"pool": "tank"})])

    def test_resilver_is_not_a_scrub(self):
        scan = "resilvered 1.2G in 00:10:00 with 0 errors on Mon Nov 10 03:00:00 2026"
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE), _proc(_status_one(scan)))):
            points = collector.collect_zfs(now=NOW)
        scrub = [p for p in points if p["metric"].startswith("scrub")]
        self.assertEqual(scrub, [])

    def test_none_requested_gives_minus_one(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE),
                                             _proc(_status_one("none requested")))):
            points = collector.collect_zfs(now=NOW)
        scrub = [(p["metric"], p["value"]) for p in points if p["metric"].startswith("scrub")]
        self.assertEqual(scrub, [("scrub_age_days", -1)])

    def test_never_scrubbed_pool_gives_the_sentinel_summary(self):
        # zroot was scrubbed 7.5 days ago; tank has never been scrubbed. The
        # sentinel must win over the finite age so a never-scrubbed pool alerts.
        never = STATUS_HEALTHY.replace(
            "scan: resilvered 1.2G in 00:10:00 with 0 errors on Mon Nov 10 03:00:00 2026",
            "scan: none requested")
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_HEALTHY), _proc(never))):
            points = collector.collect_zfs(now=NOW)
        summary = {p["metric"]: p["value"] for p in points if p["labels"] == {}}
        self.assertEqual(summary["max_scrub_age_days"], 9999)

    def test_status_failure_keeps_capacity_summary_without_errors_or_scrub(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE), _proc("", 1, "boom"))):
            points = collector.collect_zfs(now=NOW)
        unlabelled = sorted(p["metric"] for p in points if p["labels"] == {})
        self.assertEqual(unlabelled, ["max_capacity_percent", "pools_unhealthy"])

    def test_no_summary_when_zpool_list_is_empty(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(""), _proc(STATUS_HEALTHY))):
            points = collector.collect_zfs(now=NOW)
        self.assertEqual([p for p in points if not p["labels"]], [])

    def test_list_timeout_returns_empty_without_raising(self):
        timeout = _subprocess.TimeoutExpired(cmd="zpool", timeout=10)
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(timeout, _proc(STATUS_HEALTHY))):
            self.assertEqual(collector.collect_zfs(now=NOW), [])

    def test_status_timeout_keeps_the_list_points(self):
        timeout = _subprocess.TimeoutExpired(cmd="zpool", timeout=10)
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(LIST_ONE), timeout)):
            points = collector.collect_zfs(now=NOW)
        metrics = {p["metric"] for p in points}
        self.assertIn("pool_size_bytes", metrics)
        self.assertIn("pool_state", metrics)
        self.assertNotIn("pool_errors", metrics)
        self.assertNotIn("scrub_age_days", metrics)

    def test_nonzero_exit_returns_empty(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc("", 1, "permission denied"), _proc(""))):
            self.assertEqual(collector.collect_zfs(now=NOW), [])

    def test_unparseable_list_line_is_skipped_and_the_rest_kept(self):
        listing = "bad\tnot-a-number\t1\t1\t-\t1\tONLINE\n" + LIST_ONE
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          side_effect=_zpool(_proc(listing), _proc(""))):
            points = collector.collect_zfs(now=NOW)
        self.assertEqual({p["labels"]["pool"] for p in points if p["labels"]}, {"tank"})

    def test_runs_zpool_with_timeout_and_clean_env(self):
        with patch.object(collector.shutil, "which", return_value="/usr/sbin/zpool"), \
             patch.object(collector.subprocess, "run",
                          return_value=_proc(LIST_ONE)) as run:
            collector.collect_zfs(now=NOW)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertIn("env", call.kwargs)
        self.assertEqual(run.call_args_list[0].args[0][:2], ["zpool", "list"])
        self.assertEqual(run.call_args_list[1].args[0], ["zpool", "status", "-p"])


class CollectAllZfsTests(unittest.TestCase):
    def test_collect_all_includes_zfs_points(self):
        zfs_points = [{"category": "zfs", "metric": "pool_healthy", "value": 1,
                       "labels": {"pool": "zroot"}, "time": "t"}]
        with patch.object(collector.aggregate, "collect_cpu", return_value=[]), \
             patch.object(collector.aggregate, "collect_memory", return_value=[]), \
             patch.object(collector.aggregate, "collect_disk", return_value=[]), \
             patch.object(collector.aggregate, "collect_network", return_value=[]), \
             patch.object(collector.aggregate, "collect_temperatures", return_value=[]), \
             patch.object(collector.aggregate, "collect_top_processes", return_value=[]), \
             patch.object(collector.aggregate, "collect_gpu", return_value=[]), \
             patch.object(collector.aggregate, "collect_zfs", return_value=zfs_points):
            points = collector.collect_all()
        self.assertEqual(points, zfs_points)


if __name__ == "__main__":
    unittest.main()
