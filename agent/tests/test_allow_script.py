"""`vigil-agent allow-script` CLI — approve, list and revoke inline-script hashes."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from vigil_agent import allowscript, scripthash
from vigil_agent.config import load_config

_HASH = scripthash.script_hash("echo hi\n")

BASE = (
    "# written by install.sh\n"
    "server_url: https://vigil.example.com\n"
    "agent_token: sekret\n"
    "mode: managed\n"
)


def _config_path(tmpdir: Path, body: str) -> Path:
    p = tmpdir / "agent.yml"
    p.write_text(body, encoding="utf-8")
    return p


class AllowScriptTests(unittest.TestCase):
    def test_file_is_hashed_like_the_agent(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            script = tmpdir / "cleanup.sh"
            script.write_bytes(b"echo hi\r\n")  # CRLF on purpose
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = allowscript.main([str(script), "--no-restart",
                                       "-c", str(cfg)], None)
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().splitlines()[0], _HASH)

    def test_hash_is_added_and_comments_survive(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            rc = allowscript.main(["--hash", _HASH, "--no-restart",
                                   "-c", str(cfg)], None)
            self.assertEqual(rc, 0)
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("# written by install.sh", text)
            data = yaml.safe_load(text)
            self.assertIn(_HASH, data["allowed_script_hashes"])

    def test_adding_twice_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            self.assertEqual(allowscript.main(["--hash", _HASH, "--no-restart",
                                               "-c", str(cfg)], None), 0)
            self.assertEqual(allowscript.main(["--hash", _HASH, "--no-restart",
                                               "-c", str(cfg)], None), 0)
            text = cfg.read_text(encoding="utf-8")
            self.assertEqual(text.count(_HASH), 1)

    def test_remove_revokes(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            self.assertEqual(allowscript.main(["--hash", _HASH, "--no-restart",
                                               "-c", str(cfg)], None), 0)
            self.assertEqual(allowscript.main(["--remove", _HASH, "--no-restart",
                                               "-c", str(cfg)], None), 0)
            data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            self.assertFalse(data.get("allowed_script_hashes"))

    def test_missing_key_is_appended(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            self.assertEqual(allowscript.main(["--hash", _HASH, "--no-restart",
                                               "-c", str(cfg)], None), 0)
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("allowed_script_hashes:\n  - " + _HASH, text)

    def test_flow_style_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE + "allowed_script_hashes: []\n")
            before = cfg.read_text(encoding="utf-8")
            rc = allowscript.main(["--hash", _HASH, "--no-restart",
                                   "-c", str(cfg)], None)
            self.assertNotEqual(rc, 0)
            self.assertEqual(cfg.read_text(encoding="utf-8"), before)

    def test_bad_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            before = cfg.read_text(encoding="utf-8")
            rc = allowscript.main(["--hash", "sha256:xyz", "--no-restart",
                                   "-c", str(cfg)], None)
            self.assertEqual(rc, 2)
            self.assertEqual(cfg.read_text(encoding="utf-8"), before)

    @unittest.skipUnless(sys.platform != "win32", "POSIX only")
    def test_file_mode_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            os.chmod(cfg, 0o600)
            rc = allowscript.main(["--hash", _HASH, "--no-restart",
                                   "-c", str(cfg)], None)
            self.assertEqual(rc, 0)
            self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)

    def test_approved_hash_is_loaded_by_the_agent(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            cfg = _config_path(tmpdir, BASE)
            self.assertEqual(allowscript.main(["--hash", _HASH, "--no-restart",
                                               "-c", str(cfg)], None), 0)
            config = load_config(cfg)
            self.assertIn(_HASH, config.allowed_script_hashes)


if __name__ == "__main__":
    unittest.main()
