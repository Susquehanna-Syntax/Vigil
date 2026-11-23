"""Read-only image mounts are not disks.

``collect_disk`` and the hardware inventory's ``_read_disks`` both walk
psutil's partitions, so the filter that keeps snap/CD-ROM images out of the
disk series lives here rather than being spelled twice.
"""

#: Read-only image filesystems: always 100% used by construction, so counting them as
#: disks makes every Ubuntu (snaps) and CD-ROM-equipped Windows host read full. Snap mounts
#: every snap as squashfs under /snap; erofs is the same idea (Android, some distros).
_IMAGE_FSTYPES = frozenset({"squashfs", "erofs", "iso9660", "udf", "cdfs"})


def _is_real_disk(part) -> bool:
    if (part.fstype or "").lower() in _IMAGE_FSTYPES:
        return False
    if "cdrom" in (part.opts or "").split(","):
        return False
    mount = part.mountpoint or ""
    return not (mount == "/snap" or mount.startswith("/snap/"))
