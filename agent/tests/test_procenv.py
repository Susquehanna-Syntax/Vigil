"""procenv: the PyInstaller --onefile bootloader sets LD_LIBRARY_PATH (and the
DYLD/LD_PRELOAD family) to its /tmp/_MEI<random>/ extraction directory. Every
child the agent spawns must inherit a clean environment, plus the initramfs
poison check and the executor's sanitized env.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, pkg_manager, procenv
from vigil_agent.procenv import clean_env

_BUNDLE = "/tmp/_MEIabc123"
_IMAGE = "/boot/initrd.img-6.8.0-40-generic"


class CleanEnvTests(unittest.TestCase):
    def test_restores_original_value_when_orig_is_set(self):
        with patch.dict(os.environ, {
                "LD_LIBRARY_PATH": _BUNDLE,
                "LD_LIBRARY_PATH_ORIG": "/opt/lib",
            }, clear=True):
            env = clean_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)

    def test_drops_variable_when_orig_is_empty(self):
        with patch.dict(os.environ, {
                "LD_LIBRARY_PATH": _BUNDLE,
                "LD_LIBRARY_PATH_ORIG": "",
            }, clear=True):
            env = clean_env()
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)

    def test_strips_bundle_dir_when_frozen_without_orig(self):
        with patch.dict(os.environ, {
            "LD_LIBRARY_PATH": f"{_BUNDLE}:/usr/local/lib",
        }, clear=True), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", _BUNDLE, create=True):
            env = clean_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/local/lib")

    def test_removes_variable_when_frozen_and_bundle_is_the_only_entry(self):
        with patch.dict(os.environ, {
            "LD_LIBRARY_PATH": _BUNDLE,
        }, clear=True), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", _BUNDLE, create=True):
            env = clean_env()
        self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_leaves_operator_value_alone_when_not_frozen(self):
        with patch.dict(os.environ, {
            "LD_LIBRARY_PATH": "/opt/lib",
        }, clear=True):
            env = clean_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")

    def test_sanitizes_ld_preload_and_dyld_variables(self):
        with patch.dict(os.environ, {
                "LD_PRELOAD": _BUNDLE,
                "LD_PRELOAD_ORIG": "/usr/lib/preload.so",
                "DYLD_INSERT_LIBRARIES": _BUNDLE,
                "DYLD_INSERT_LIBRARIES_ORIG": "",
            }, clear=True):
            env = clean_env()
        self.assertEqual(env["LD_PRELOAD"], "/usr/lib/preload.so")
        self.assertNotIn("DYLD_INSERT_LIBRARIES", env)

    def test_preserves_unrelated_environment(self):
        with patch.dict(os.environ, {
                "PATH": "/usr/bin:/bin",
                "HOME": "/root",
                "LD_LIBRARY_PATH": _BUNDLE,
                "LD_LIBRARY_PATH_ORIG": "",
            }, clear=True):
            env = clean_env()
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["HOME"], "/root")

    def test_extra_overrides_take_precedence(self):
        with patch.dict(os.environ, {"FOO": "baz"}, clear=True):
            env = clean_env({"FOO": "bar"})
        self.assertEqual(env["FOO"], "bar")


class PoisonedInitramfsTests(unittest.TestCase):
    def _result(self, stdout, returncode=0):
        return subprocess.CompletedProcess(
            args=["lsinitramfs"], returncode=returncode,
            stdout=stdout, stderr="",
        )

    def test_reports_images_containing_ephemeral_paths(self):
        stdout = (
            "usr/lib/x86_64-linux-gnu/libzstd.so.1\n"
            "tmp/_MEImJMjhx/libzstd.so.1\n"
        )
        with patch.object(pkg_manager.sys, "platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/lsinitramfs"), \
             patch("glob.glob", return_value=[_IMAGE]), \
             patch.object(pkg_manager.subprocess, "run",
                          return_value=self._result(stdout)) as mock_run:
            self.assertEqual(pkg_manager.poisoned_initramfs(), [_IMAGE])
        self.assertIn("lsinitramfs", mock_run.call_args.args[0])

    def test_reports_nothing_for_a_clean_image(self):
        stdout = "usr/lib/x86_64-linux-gnu/libzstd.so.1\n"
        with patch.object(pkg_manager.sys, "platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/lsinitramfs"), \
             patch("glob.glob", return_value=[_IMAGE]), \
             patch.object(pkg_manager.subprocess, "run",
                          return_value=self._result(stdout)):
            self.assertEqual(pkg_manager.poisoned_initramfs(), [])

    def test_reports_nothing_when_lsinitramfs_is_absent(self):
        with patch.object(pkg_manager.sys, "platform", "linux"), \
             patch("shutil.which", return_value=None):
            self.assertEqual(pkg_manager.poisoned_initramfs(), [])


class ExecutorRunEnvTests(unittest.TestCase):
    def test_executor_run_passes_a_sanitized_environment(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch.dict(os.environ, {
                "LD_LIBRARY_PATH": "/tmp/_MEIabc",
                "LD_LIBRARY_PATH_ORIG": "",
            }, clear=True), \
             patch.object(executor.subprocess, "run", fake_run):
            executor._run(["true"])
        self.assertIsNotNone(captured["env"])
        self.assertNotIn("LD_LIBRARY_PATH", captured["env"])
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", captured["env"])


if __name__ == "__main__":
    unittest.main()
