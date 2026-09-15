"""A config with a UTF-8 BOM must still load.

install.ps1 wrote one for the life of the product: PowerShell 5.1's
`Set-Content -Encoding UTF8` emits a BOM, and Windows ships 5.1. The BOM
becomes part of the first key, so `raw.get("server_url")` returns None and the
agent exits with

    ValueError: server_url is required in config

while the operator looks at a config file that plainly has a server_url. It
went unnoticed because the Windows service could never start to read it.

Notepad also writes a BOM, so this is not only an installer concern — it is
what happens when a Windows admin edits agent.yml by hand.
"""

import tempfile
import unittest
from pathlib import Path

from vigil_agent.config import load_config

CONFIG = (
    'server_url: "https://vigil.example.com"\n'
    'agent_token: "%s"\n'
    "mode: monitor\n"
    "checkin_interval: 30\n"
) % ("a" * 64)


class ConfigWithABom(unittest.TestCase):
    def _write(self, text, encoding):
        d = tempfile.mkdtemp()
        p = Path(d) / "agent.yml"
        p.write_text(text, encoding=encoding)
        return p

    def test_loads_without_a_bom(self):
        cfg = load_config(self._write(CONFIG, "utf-8"))
        self.assertEqual(cfg.server_url, "https://vigil.example.com")

    def test_loads_with_a_utf8_bom(self):
        path = self._write(CONFIG, "utf-8-sig")
        self.assertEqual(
            path.read_bytes()[:3], b"\xef\xbb\xbf", "fixture lost its BOM"
        )
        cfg = load_config(path)
        self.assertEqual(
            cfg.server_url, "https://vigil.example.com",
            "a BOM made the first key unreadable; the agent would exit with "
            "'server_url is required'",
        )

    def test_windows_style_data_dir_survives(self):
        """Single-quoted so backslashes stay literal — a double-quoted YAML
        scalar treats them as escapes and fails on \\P."""
        text = CONFIG + "data_dir: 'C:\\ProgramData\\Vigil\\data'\n"
        cfg = load_config(self._write(text, "utf-8-sig"))
        self.assertEqual(str(cfg.data_dir), "C:\\ProgramData\\Vigil\\data")


if __name__ == "__main__":
    unittest.main()
