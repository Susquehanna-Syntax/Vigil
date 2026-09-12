"""The default config search must know where each platform keeps agent.yml.

It did not. DEFAULT_CONFIG_PATHS was ['/etc/vigil/agent.yml', 'agent.yml'] on
every platform. On Windows the first resolves to '\\etc\\vigil\\agent.yml' on
the current drive, which nothing ever writes, so an agent started without -c
found no config and exited:

    FileNotFoundError: No config file found. Tried: VIGIL_CONFIG_PATH,
    ['\\etc\\vigil\\agent.yml', 'agent.yml']

install.ps1 has always written C:\\ProgramData\\Vigil\\agent.yml, and the
service's binPath carried no -c, so the Windows service could never have
started regardless of how the agent was packaged. Found by watching a onedir
service reach START_PENDING and then stop.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from vigil_agent import config


class DefaultConfigPaths(unittest.TestCase):
    def test_posix_looks_in_etc(self):
        paths = [str(p) for p in config._default_config_paths(is_windows=False)]
        self.assertIn("/etc/vigil/agent.yml", paths)

    def test_windows_looks_in_programdata(self):
        with patch.dict(config.os.environ, {"ProgramData": r"C:\ProgramData"}):
            paths = [str(p) for p in config._default_config_paths(is_windows=True)]
        self.assertTrue(
            any("Vigil" in p and "agent.yml" in p for p in paths),
            f"Windows default paths never reach ProgramData: {paths}",
        )

    def test_windows_does_not_look_in_etc(self):
        """'/etc/...' on Windows is a path on the current drive that nothing
        writes; offering it only produces a misleading error message."""
        with patch.dict(config.os.environ, {"ProgramData": r"C:\ProgramData"}):
            paths = [str(p) for p in config._default_config_paths(is_windows=True)]
        self.assertFalse(
            any(p.replace("\\", "/").startswith("/etc/") for p in paths), paths
        )

    def test_cwd_fallback_is_kept_everywhere(self):
        for is_win in (False, True):
            with patch.dict(config.os.environ, {"ProgramData": r"C:\ProgramData"}):
                paths = [Path(str(p)).name for p in config._default_config_paths(is_windows=is_win)]
            self.assertIn("agent.yml", paths)

    def test_windows_without_programdata_still_has_a_fallback(self):
        env = {k: v for k, v in config.os.environ.items() if k != "ProgramData"}
        with patch.dict(config.os.environ, env, clear=True):
            paths = config._default_config_paths(is_windows=True)
        self.assertTrue(paths, "no candidates at all leaves a useless error")


if __name__ == "__main__":
    unittest.main()
