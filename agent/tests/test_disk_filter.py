"""Read-only image filesystems (snap/erofs) are not disks.

On Ubuntu every snap is a squashfs image mounted under /snap and psutil
reports each one at 100 % used, so the Monitor page fills with pink bars for
images no one can fill or free. Both partition walks — collect_disk and the
hardware inventory's _read_disks — have to skip them.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import collector


def _part(device, mountpoint, fstype):
    return SimpleNamespace(device=device, mountpoint=mountpoint, fstype=fstype, opts="")


def _usage(total=100, used=50, percent=50.0):
    return SimpleNamespace(total=total, used=used, free=total - used, percent=percent)


class IsRealDiskTest(unittest.TestCase):
    def test_is_real_disk(self):
        real = [
            _part("/dev/sda1", "/", "ext4"),
            _part("/dev/sdb1", "/home", "xfs"),
            _part("C:\\", "C:\\", "NTFS"),
            _part("/dev/mmcblk0p1", "/boot/efi", "vfat"),
        ]
        for part in real:
            self.assertTrue(collector._is_real_disk(part), part.mountpoint)

        images = [
            _part("/dev/loop0", "/snap/core22/2045", "squashfs"),
            _part("/dev/loop3", "/snap/gnome-42-2204", "squashfs"),
            _part("/dev/loop5", "/usr/lib/foo", "erofs"),
            _part("/dev/loop7", "/snap/bin-x", "ext4"),
        ]
        for part in images:
            self.assertFalse(collector._is_real_disk(part), part.mountpoint)


class DiskWalkTest(unittest.TestCase):
    def test_collect_disk_skips_snaps(self):
        parts = [
            _part("/dev/sda1", "/", "ext4"),
            _part("/dev/loop0", "/snap/core22/1", "squashfs"),
        ]
        with (
            patch.object(collector.psutil, "disk_partitions", return_value=parts),
            patch.object(collector.psutil, "disk_usage", return_value=_usage()),
        ):
            points = collector.collect_disk()
        self.assertTrue(points)
        for point in points:
            self.assertEqual("/", point["labels"]["mount"])

    def test_read_disks_skips_snaps(self):
        parts = [
            _part("/dev/sda1", "/", "ext4"),
            _part("/dev/loop0", "/snap/core22/1", "squashfs"),
        ]
        with (
            patch.object(collector.psutil, "disk_partitions", return_value=parts),
            patch.object(collector.psutil, "disk_usage", return_value=_usage()),
        ):
            disks = collector._read_disks()
        self.assertTrue(disks)
        for disk in disks:
            self.assertEqual("/", disk["mount"])


if __name__ == "__main__":
    unittest.main()
