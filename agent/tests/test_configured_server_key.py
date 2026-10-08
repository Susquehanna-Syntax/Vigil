"""SEC-3: the installer writes the server's public key into agent.yml, and then it
is the only key the agent trusts. No trust on first use, and a pin file in
data_dir (owned by the service account in monitor mode) cannot override it."""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from nacl.signing import SigningKey

from vigil_agent import config as agent_config
from vigil_agent import verify

REAL = base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()
OTHER = base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()


class ConfiguredKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)

    def test_the_configured_key_is_accepted(self):
        key = verify.pin_public_key(self.data, REAL, configured=REAL)
        self.assertEqual(base64.b64encode(bytes(key)).decode(), REAL)

    def test_a_different_key_from_the_server_is_refused(self):
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, OTHER, configured=REAL)

    def test_a_planted_pin_file_cannot_override_the_configured_key(self):
        (self.data / "server_public_key.pin").write_text(OTHER)
        key = verify.get_pinned_key(self.data, configured=REAL)
        self.assertEqual(base64.b64encode(bytes(key)).decode(), REAL)
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, OTHER, configured=REAL)

    def test_without_a_configured_key_first_use_still_pins(self):
        verify.pin_public_key(self.data, REAL)
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, OTHER)


class ConfigFieldTests(unittest.TestCase):
    def _load(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yml"
            path.write_text(f'server_url: "https://v"\nagent_token: "t"\nmode: monitor\n{extra}')
            path.chmod(0o600)
            return agent_config.load_config(path)

    def test_key_is_read_from_agent_yml(self):
        self.assertEqual(self._load(f'server_public_key: "{REAL}"\n').server_public_key, REAL)

    def test_a_malformed_key_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('server_public_key: "not-a-key"\n')

    def test_absent_means_empty(self):
        self.assertEqual(self._load("").server_public_key, "")


if __name__ == "__main__":
    unittest.main()
