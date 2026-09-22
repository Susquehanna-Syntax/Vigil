"""A monitor-mode agent on a real host must not lose its metrics to one bad sub-call.

Three failures observed on the enrolling VMs:

  A. Windows monitor mode runs as NT SERVICE\\vigil-agent, which cannot open
     performance counters (PDH), so two psutil calls raise every cycle and,
     because the exception escaped, the whole collector's output was dropped:

         ERROR vigil.collector Collector collect_cpu failed
           File "vigil_agent\\collector.py", line 47, in collect_cpu
         RuntimeError: PdhAddEnglishCounterW failed. Performance counters may be
         disabled.
         ERROR vigil.collector Collector collect_memory failed
           File "psutil\\_pswindows.py", line 264, in swap_memory
         RuntimeError: PdhAddEnglishCounterW failed. Performance counters may be
         disabled.

     Usage % and virtual memory do not need PDH — only load average and swap do.

  B. Both platforms report read-only image mounts as 100% disks, so the host
     card (max across mounts) reads full on healthy machines:

         Ubuntu:   {'mount': '/snap/firefox/8763', 'device': '/dev/loop3',
                    'fstype': 'squashfs'} 100.0   (x13 snaps)
         Windows:  {'mount': 'D:\\', 'device': 'D:\\', 'fstype': 'CDFS'} 100.0
"""

import collections
import unittest
from unittest.mock import patch

from vigil_agent import collector

SdiskPart = collections.namedtuple("sdiskpart", "device mountpoint fstype opts")
SdiskUsage = collections.namedtuple("sdiskusage", "total used free percent")
Smem = collections.namedtuple("smem", "total used available percent")

PARTITIONS = [
    SdiskPart("/dev/sda2", "/", "ext4", "rw"),
    SdiskPart("/dev/loop3", "/snap/firefox/8763", "squashfs", "ro"),
    SdiskPart("D:\\", "D:\\", "CDFS", "ro,cdrom"),
    SdiskPart("E:\\", "E:\\", "", "cdrom"),
    SdiskPart("C:\\", "C:\\", "NTFS", "rw,fixed"),
]


def _cpu_percent(*args, **kwargs):
    return [10.0, 20.0] if kwargs.get("percpu") else 15.0


class SubMetricResilience(unittest.TestCase):
    def setUp(self):
        collector._warned.clear()

    def test_cpu_usage_survives_a_loadavg_failure(self):
        with (
            patch.object(collector.psutil, "cpu_percent", side_effect=_cpu_percent),
            patch.object(
                collector.psutil, "getloadavg",
                side_effect=RuntimeError("PdhAddEnglishCounterW failed"),
            ),
        ):
            points = collector.collect_cpu()

        total = [
            p for p in points
            if p["category"] == "cpu" and p["metric"] == "usage_percent"
            and p["labels"].get("core") == "total"
        ]
        self.assertEqual(len(total), 1)
        self.assertEqual(total[0]["value"], 15.0)
        self.assertNotIn("load_1m", {p["metric"] for p in points})

    def test_memory_survives_a_swap_failure(self):
        with (
            patch.object(
                collector.psutil, "virtual_memory",
                return_value=Smem(total=16, used=8, available=8, percent=50.0),
            ),
            patch.object(
                collector.psutil, "swap_memory",
                side_effect=RuntimeError("PdhAddEnglishCounterW failed"),
            ),
        ):
            points = collector.collect_memory()

        self.assertIn("usage_percent", {p["metric"] for p in points})
        self.assertFalse(any(p["metric"].startswith("swap_") for p in points))

    def test_sub_metric_failure_warns_once(self):
        with (
            patch.object(collector.psutil, "cpu_percent", side_effect=_cpu_percent),
            patch.object(
                collector.psutil, "getloadavg",
                side_effect=RuntimeError("PdhAddEnglishCounterW failed"),
            ),
            self.assertLogs("vigil.collector", "WARNING") as cm,
        ):
            collector.collect_cpu()
            collector.collect_cpu()

        loadavg_warnings = [
            r for r in cm.records
            if r.levelname == "WARNING" and "loadavg" in r.getMessage()
        ]
        self.assertEqual(len(loadavg_warnings), 1)


class ImageMountFiltering(unittest.TestCase):
    def _partitioned(self):
        return (
            patch.object(collector.psutil, "disk_partitions", return_value=PARTITIONS),
            patch.object(
                collector.psutil, "disk_usage",
                return_value=SdiskUsage(total=100, used=10, free=90, percent=10.0),
            ),
        )

    def test_image_mounts_are_not_disks(self):
        p1, p2 = self._partitioned()
        with p1, p2:
            points = collector.collect_disk()
        self.assertEqual(
            {p["labels"]["mount"] for p in points if p["metric"] == "usage_percent"},
            {"/", "C:\\"},
        )

    def test_read_disks_skips_image_mounts(self):
        p1, p2 = self._partitioned()
        with p1, p2:
            disks = collector._read_disks()
        self.assertEqual({d["mount"] for d in disks}, {"/", "C:\\"})


if __name__ == "__main__":
    unittest.main()
