import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import reprovision


class BootloaderDetectionTests(unittest.TestCase):
    def test_grub2_detected_from_grub_cfg(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "grub").mkdir()
            (boot / "grub" / "grub.cfg").write_text("menuentry 'Linux' {}")
            with patch.object(reprovision, "BOOT_DIR", boot):
                self.assertEqual(reprovision.detect_bootloader(), "grub2")

    def test_grub2_detected_from_the_rhel_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "grub2").mkdir()
            (boot / "grub2" / "grub.cfg").write_text("menuentry 'Linux' {}")
            with patch.object(reprovision, "BOOT_DIR", boot):
                self.assertEqual(reprovision.detect_bootloader(), "grub2")

    def test_systemd_boot_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "loader" / "entries").mkdir(parents=True)
            with patch.object(reprovision, "BOOT_DIR", boot):
                self.assertEqual(reprovision.detect_bootloader(), "systemd-boot")

    def test_unknown_bootloader_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(reprovision, "BOOT_DIR", Path(tmp)):
                self.assertEqual(reprovision.detect_bootloader(), "unknown")


class PreflightTests(unittest.TestCase):
    def _preflight(self, params, **over):
        defaults = dict(bootloader="grub2", disks=[{"name": "/dev/sda",
                                                    "size_bytes": 1,
                                                    "mountpoints": []}],
                        free=10 * 1024**3, ram=8 * 1024**3)
        defaults.update(over)
        with patch.object(reprovision, "detect_bootloader",
                          return_value=defaults["bootloader"]), \
             patch.object(reprovision, "_list_disks",
                          return_value=defaults["disks"]), \
             patch.object(reprovision, "_free_boot_bytes",
                          return_value=defaults["free"]), \
             patch.object(reprovision, "_total_ram_bytes",
                          return_value=defaults["ram"]):
            return json.loads(reprovision.preflight(params, None))

    def test_healthy_machine_passes(self):
        result = self._preflight({"disk_target": "/dev/sda"})
        self.assertTrue(result["ok"], result["failures"])

    def test_unknown_bootloader_fails_preflight(self):
        result = self._preflight({}, bootloader="unknown")
        self.assertFalse(result["ok"])
        self.assertTrue(any("bootloader" in f.lower() for f in result["failures"]))

    def test_preflight_lists_every_disk_not_just_the_target(self):
        # The failure this guards against is picking the data disk by
        # mistake, which a human catches by reading the list.
        result = self._preflight(
            {"disk_target": "/dev/sda"},
            disks=[{"name": "/dev/sda", "size_bytes": 1, "mountpoints": ["/"]},
                   {"name": "/dev/sdb", "size_bytes": 2,
                    "mountpoints": ["/data"]}])
        self.assertEqual(len(result["disks"]), 2)

    def test_missing_target_disk_fails(self):
        result = self._preflight({"disk_target": "/dev/nvme9"})
        self.assertFalse(result["ok"])

    def test_tiny_boot_partition_fails(self):
        result = self._preflight({"disk_target": "/dev/sda"},
                                 free=10 * 1024 * 1024)
        self.assertFalse(result["ok"])
        self.assertTrue(any("boot" in f.lower() for f in result["failures"]))

    def test_insufficient_ram_fails(self):
        result = self._preflight({"disk_target": "/dev/sda",
                                  "os_family": "ubuntu"},
                                 ram=512 * 1024 * 1024)
        self.assertFalse(result["ok"])
        self.assertTrue(any("ram" in f.lower() for f in result["failures"]))

    def test_preflight_reports_architecture(self):
        self.assertIn("architecture", self._preflight({}))


class StagingTests(unittest.TestCase):
    def test_checksum_mismatch_refuses_to_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            with patch.object(reprovision, "STAGE_DIR", stage), \
                 patch.object(reprovision, "_download",
                              side_effect=lambda url, dest: dest.write_bytes(b"x")):
                with self.assertRaises(reprovision.PreflightError):
                    reprovision.stage({
                        "job_id": "j1",
                        "kernel_url": "https://x/vmlinuz",
                        "initrd_url": "https://x/initrd",
                        "kernel_sha256": "0" * 64,
                        "initrd_sha256": "0" * 64,
                    }, None)

    def test_failed_stage_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            with patch.object(reprovision, "STAGE_DIR", stage), \
                 patch.object(reprovision, "_download",
                              side_effect=lambda url, dest: dest.write_bytes(b"x")):
                with self.assertRaises(reprovision.PreflightError):
                    reprovision.stage({
                        "job_id": "j1",
                        "kernel_url": "https://x/vmlinuz",
                        "initrd_url": "https://x/initrd",
                        "kernel_sha256": "0" * 64,
                        "initrd_sha256": "0" * 64,
                    }, None)
            self.assertFalse(stage.exists())

    def test_matching_checksums_stage_successfully(self):
        import hashlib

        payload = b"kernel bytes"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            with patch.object(reprovision, "STAGE_DIR", stage), \
                 patch.object(reprovision, "_download",
                              side_effect=lambda url, dest: dest.write_bytes(payload)):
                out = reprovision.stage({
                    "job_id": "j1",
                    "kernel_url": "https://x/vmlinuz",
                    "initrd_url": "https://x/initrd",
                    "kernel_sha256": digest,
                    "initrd_sha256": digest,
                }, None)
            self.assertIn("j1", out)
            self.assertTrue((stage / "vmlinuz").exists())
            self.assertTrue((stage / "initrd").exists())


class CommitTests(unittest.TestCase):
    def test_unknown_bootloader_refuses_to_commit(self):
        with patch.object(reprovision, "detect_bootloader", return_value="unknown"):
            with self.assertRaises(reprovision.PreflightError):
                reprovision.commit({"job_id": "j1", "cmdline": "x"}, None)

    def test_grub_commit_uses_a_one_shot_entry_never_the_default(self):
        """The one-shot entry is the entire safety story: a failed installer
        boot must return to the old OS unaided."""
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            grubd = Path(tmp) / "grub.d"
            grubd.mkdir()
            with patch.object(reprovision, "detect_bootloader", return_value="grub2"), \
                 patch.object(reprovision, "GRUB_D_ENTRY", grubd / "42_vigil"), \
                 patch.object(reprovision, "_run",
                              side_effect=lambda cmd, **kw: calls.append(cmd)), \
                 patch("shutil.which", return_value="/usr/sbin/tool"), \
                 patch("subprocess.Popen"):
                reprovision.commit({"job_id": "j1", "cmdline": "autoinstall"}, None)
        flat = [" ".join(c) for c in calls]
        self.assertTrue(any("grub-reboot" in f or "grub2-reboot" in f
                            for f in flat), flat)
        self.assertFalse(any("set-default" in f for f in flat), flat)

    def test_grub_commit_refuses_without_a_oneshot_binary(self):
        # Falling back to a default entry would make a failed installer boot
        # permanent. Refusing is the safer failure.
        with tempfile.TemporaryDirectory() as tmp:
            grubd = Path(tmp) / "grub.d"
            grubd.mkdir()
            with patch.object(reprovision, "detect_bootloader", return_value="grub2"), \
                 patch.object(reprovision, "GRUB_D_ENTRY", grubd / "42_vigil"), \
                 patch.object(reprovision, "_run"), \
                 patch("shutil.which", return_value=None):
                with self.assertRaises(reprovision.PreflightError):
                    reprovision.commit({"job_id": "j1", "cmdline": "x"}, None)


class CleanupTests(unittest.TestCase):
    def test_cleanup_removes_the_stage_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "vmlinuz").write_bytes(b"x")
            entry = Path(tmp) / "42_vigil"
            entry.write_text("x")
            with patch.object(reprovision, "STAGE_DIR", stage), \
                 patch.object(reprovision, "GRUB_D_ENTRY", entry):
                reprovision.cleanup({"job_id": "j1"}, None)
            self.assertFalse(stage.exists())
            self.assertFalse(entry.exists())

    def test_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(reprovision, "STAGE_DIR", Path(tmp) / "absent"), \
                 patch.object(reprovision, "GRUB_D_ENTRY", Path(tmp) / "absent2"):
                reprovision.cleanup({"job_id": "j1"}, None)  # must not raise


if __name__ == "__main__":
    unittest.main()
