"""Cleanup must not leave a boot entry pointing at a deleted kernel.

cleanup() is the recovery path — the docstring says it is dispatched on abort
and on failure while the old OS is still reachable. It removed the staged
kernel and initrd, and the GRUB generator script, but:

  * removing /etc/grub.d/42_vigil_reprovision does not remove the menuentry it
    already generated into grub.cfg. Verified on a VM: after a successful
    cleanup, `grep -c 'Vigil Reprovision' /boot/grub/grub.cfg` still returned 1,
    pointing at /vigil-reprovision/vmlinuz which had just been deleted;
  * the systemd-boot entry was never removed at all, because only the GRUB
    generator was unlinked.

Selecting such an entry — or a one-shot that outlived the job — drops the
machine at the GRUB rescue prompt, which on a headless host looks like a hang.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vigil_agent import reprovision


class CleanupRemovesBootEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.boot = root / "boot"
        self.stage = self.boot / "vigil-reprovision"
        self.grub_d = root / "etc" / "grub.d" / "42_vigil_reprovision"
        self.sd_entry = self.boot / "loader" / "entries" / "vigil-reprovision.conf"
        for p in (self.stage, self.grub_d.parent, self.sd_entry.parent):
            p.mkdir(parents=True, exist_ok=True)
        (self.stage / "vmlinuz").write_bytes(b"kernel")
        (self.stage / "initrd").write_bytes(b"initrd")

        self.patches = [
            patch.object(reprovision, "BOOT_DIR", self.boot),
            patch.object(reprovision, "STAGE_DIR", self.stage),
            patch.object(reprovision, "GRUB_D_ENTRY", self.grub_d),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(self.tmp.cleanup)

    def test_staged_files_are_removed(self):
        with patch.object(reprovision, "shutil") as sh:
            sh.which.return_value = None
            reprovision.cleanup({"job_id": "j1"}, None)
            sh.rmtree.assert_called_once()

    def test_grub_is_regenerated_after_the_generator_is_removed(self):
        """Deleting the generator leaves the already-generated menuentry."""
        self.grub_d.write_text("#!/bin/sh\n")
        with patch.object(reprovision, "_regenerate_grub") as regen:
            reprovision.cleanup({"job_id": "j1"}, None)
            regen.assert_called_once()
        self.assertFalse(self.grub_d.exists())

    def test_grub_not_regenerated_when_there_was_no_entry(self):
        """Idempotent: a second cleanup must not shell out for nothing."""
        with patch.object(reprovision, "_regenerate_grub") as regen:
            reprovision.cleanup({"job_id": "j1"}, None)
            regen.assert_not_called()

    def test_systemd_boot_entry_is_removed(self):
        self.sd_entry.write_text("title Vigil Reprovision\n")
        with patch.object(reprovision.shutil, "which", return_value=None):
            reprovision.cleanup({"job_id": "j1"}, None)
        self.assertFalse(
            self.sd_entry.exists(),
            "the systemd-boot entry survived cleanup and still points at a "
            "kernel that has been deleted",
        )

    def test_systemd_oneshot_is_cleared_when_bootctl_exists(self):
        self.sd_entry.write_text("title Vigil Reprovision\n")
        with patch.object(reprovision.shutil, "which", return_value="/usr/bin/bootctl"):
            with patch.object(reprovision, "_run") as run:
                reprovision.cleanup({"job_id": "j1"}, None)
                self.assertTrue(
                    any("bootctl" in " ".join(c.args[0]) for c in run.call_args_list),
                    "a one-shot left pointing at a removed entry selects "
                    "something that no longer exists on the next boot",
                )


if __name__ == "__main__":
    unittest.main()
