"""Self-update must not write a zip over the service binary.

Windows ships a PyInstaller --onedir build as a zip, because a --onefile
executable cannot host a Windows service. _update_agent downloaded whatever the
server served and did:

    os.replace(tmp_path, current_exe)

which on Windows would have replaced vigil-agent-windows-amd64.exe with a zip
archive — every Windows agent bricked on its first self-update — and could not
have worked anyway, since Windows locks a running executable and a onedir build
is a directory of DLLs rather than one file.

The archive is now unpacked beside the install and swapped in by a detached
helper once the service has actually stopped.
"""

import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vigil_agent import executor


class LooksLikeZip(unittest.TestCase):
    def test_detects_a_zip(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.zip"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("x", "y")
            self.assertTrue(executor._looks_like_zip(str(p)))

    def test_a_bare_binary_is_not_a_zip(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "agent"
            p.write_bytes(b"\x7fELF" + b"\x00" * 64)
            self.assertFalse(executor._looks_like_zip(str(p)))

    def test_a_missing_file_is_not_a_zip(self):
        self.assertFalse(executor._looks_like_zip("/nonexistent/nope"))


class StageOnedirUpdate(unittest.TestCase):
    def _archive(self, tmp, names):
        p = Path(tmp) / "update.zip"
        with zipfile.ZipFile(p, "w") as zf:
            for n in names:
                zf.writestr(n, "x")
        return p

    def test_refuses_a_zip_on_non_windows(self):
        """A Linux agent handed a zip should fail loudly, not improvise."""
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Vigil" / "vigil-agent"
            exe.parent.mkdir()
            arc = self._archive(tmp, ["vigil-agent"])
            with patch.object(executor.sys, "platform", "linux"):
                with self.assertRaises(ValueError) as ctx:
                    executor._stage_onedir_update(str(arc), exe)
            self.assertIn("non-Windows", str(ctx.exception))

    def test_refuses_an_archive_without_the_executable(self):
        """Swapping in a directory that cannot start is worse than failing."""
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Vigil" / "vigil-agent-windows-amd64.exe"
            exe.parent.mkdir()
            arc = self._archive(tmp, ["readme.txt", "_internal/x.dll"])
            with patch.object(executor.sys, "platform", "win32"):
                with patch.object(executor.subprocess, "Popen") as popen:
                    with self.assertRaises(ValueError) as ctx:
                        executor._stage_onedir_update(str(arc), exe)
                    popen.assert_not_called()
            self.assertIn("no vigil-agent-windows-amd64.exe", str(ctx.exception))
            self.assertFalse((Path(tmp) / "Vigil.new").exists(),
                             "a refused update must leave nothing staged")

    def test_stages_beside_the_install_and_does_not_touch_it(self):
        with TemporaryDirectory() as tmp:
            install = Path(tmp) / "Vigil"
            install.mkdir()
            exe = install / "vigil-agent-windows-amd64.exe"
            exe.write_bytes(b"old")
            arc = self._archive(
                tmp, ["vigil-agent-windows-amd64.exe", "_internal/base.dll"])
            with patch.object(executor.sys, "platform", "win32"):
                with patch.object(executor.subprocess, "Popen") as popen:
                    executor._stage_onedir_update(str(arc), exe)
                    popen.assert_called_once()
            # The running install is untouched; the swap happens after stop.
            self.assertEqual(exe.read_bytes(), b"old")
            self.assertTrue((Path(tmp) / "Vigil.new" /
                             "vigil-agent-windows-amd64.exe").exists())
            self.assertTrue((Path(tmp) / "vigil-agent-update.cmd").exists())

    def test_the_helper_waits_for_the_service_to_stop(self):
        with TemporaryDirectory() as tmp:
            install = Path(tmp) / "Vigil"
            install.mkdir()
            exe = install / "vigil-agent-windows-amd64.exe"
            exe.write_bytes(b"old")
            arc = self._archive(tmp, ["vigil-agent-windows-amd64.exe"])
            with patch.object(executor.sys, "platform", "win32"):
                with patch.object(executor.subprocess, "Popen"):
                    executor._stage_onedir_update(str(arc), exe)
            script = (Path(tmp) / "vigil-agent-update.cmd").read_text()
            self.assertIn("sc stop vigil-agent", script)
            self.assertIn("STOPPED", script,
                          "swapping before the service releases its files "
                          "leaves a half-updated directory")
            self.assertIn("sc start vigil-agent", script)
            self.assertIn("if not exist", script,
                          "there must be a rollback when the new build has "
                          "no executable")


if __name__ == "__main__":
    unittest.main()
