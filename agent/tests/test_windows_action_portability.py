"""Actions that quietly assumed Linux.

Found by dispatching a spread of actions at a real Windows agent rather than
reading the code. Two of them had no platform handling at all:

  clear_temp_files
      ran `find /tmp -type f -mtime +N -delete` unconditionally. On Windows
      "find" is C:\\Windows\\System32\\FIND.exe — a string-search tool sharing
      only the name — so the task failed with "FIND: Invalid switch", and /tmp
      does not exist there either.

  execute_script
      guarded against path traversal with
      `str(script_path).startswith(str(scripts_dir) + "/")`. That separator
      never appears in a Windows path, so every script was refused. Fail-safe,
      but it meant the action could not work on Windows at all. The POSIX
      mode-bit check beside it is equally meaningless there, and its remedy —
      chmod — is not a command the operator has.
"""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vigil_agent import config as agent_config
from vigil_agent.executor import _clear_temp_files, _execute_script


class ClearTempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = Path(self.tmp) / "old.log"
        self.new = Path(self.tmp) / "new.log"
        self.old.write_text("x" * 2048)
        self.new.write_text("y" * 2048)
        os.utime(self.old, (time.time() - 30 * 86400,) * 2)

    def _run(self, **params):
        with patch("tempfile.gettempdir", return_value=self.tmp):
            return _clear_temp_files(params, None)

    def test_removes_old_files_and_keeps_recent_ones(self):
        out = self._run(older_than_days=7)
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.exists())
        self.assertIn("Removed 1 file", out)

    def test_uses_the_platform_temp_dir_not_a_hardcoded_slash_tmp(self):
        out = self._run(older_than_days=7)
        self.assertIn(self.tmp, out)

    def test_shells_out_to_nothing(self):
        """The whole point: no `find`, so no FIND.exe."""
        with patch("vigil_agent.executor._run") as run:
            self._run(older_than_days=7)
            run.assert_not_called()

    def test_rejects_a_negative_age(self):
        with self.assertRaises(ValueError):
            self._run(older_than_days=-1)

    def test_a_locked_file_does_not_fail_the_task(self):
        """Windows holds temp files open as a matter of course."""
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            out = self._run(older_than_days=7)
        self.assertIn("in use or not permitted", out)


class ExecuteScriptPathGuard(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.script = self.dir / "ok.sh"
        self.script.write_text("#!/bin/sh\necho hi\n")
        self.script.chmod(0o700)

    class _Cfg:
        def __init__(self, scripts_dir):
            self.scripts_dir = scripts_dir

    def test_a_script_inside_the_directory_is_accepted(self):
        with patch("vigil_agent.executor._run", return_value="hi") as run:
            out = _execute_script({"script_name": "ok.sh"}, self._Cfg(self.dir))
        self.assertEqual(out, "hi")
        run.assert_called_once()

    def test_traversal_is_still_refused(self):
        with self.assertRaises(ValueError):
            _execute_script({"script_name": "../etc/passwd"},
                            self._Cfg(self.dir))

    def test_a_world_writable_script_is_refused_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX mode bits only")
        self.script.chmod(0o777)
        with self.assertRaises(ValueError) as ctx:
            _execute_script({"script_name": "ok.sh"}, self._Cfg(self.dir))
        self.assertIn("writable by group/others", str(ctx.exception))


class ScriptsDirDefault(unittest.TestCase):
    def test_windows_default_is_under_programdata(self):
        with patch.dict(agent_config.os.environ,
                        {"ProgramData": r"C:\ProgramData"}):
            path = str(agent_config._default_scripts_dir(is_windows=True))
        self.assertIn("Vigil", path)

    def test_posix_default_is_etc(self):
        self.assertEqual(str(agent_config._default_scripts_dir(is_windows=False)),
                         "/etc/vigil/scripts")


if __name__ == "__main__":
    unittest.main()
