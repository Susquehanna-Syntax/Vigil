"""Configuration and startup surface: env parsing and the version banner."""

import hashlib
import unittest

from vigil_relay import __version__
from vigil_relay.__main__ import banner
from vigil_relay.config import Config, from_env

GOOD = hashlib.sha256(b"k1").hexdigest()


def _env(**overrides):
    env = {}
    for key, value in overrides.items():
        if value is not None:
            env[key] = value
    return env


class ConfigTest(unittest.TestCase):
    def test_single_key_becomes_the_default_tenant(self):
        cfg = from_env(_env(RELAY_KEY_SHA256=GOOD))
        self.assertEqual(cfg.tenants, {"default": GOOD})
        self.assertEqual(cfg.port, 8443)
        self.assertEqual(cfg.queue_dir, "/var/lib/vigil-relay/queue")
        self.assertFalse(cfg.trust_proxy)

    def test_tenants_json_and_overrides(self):
        cfg = from_env(_env(
            RELAY_TENANTS=f'{{"acme": "{GOOD}"}}',
            RELAY_PORT="9000",
            RELAY_QUEUE_DIR="/tmp/q",
            RELAY_TLS_CERT="/tls/c.pem",
            RELAY_TLS_KEY="/tls/k.pem",
            RELAY_TRUST_PROXY="yes",
        ))
        self.assertEqual(cfg.tenants, {"acme": GOOD})
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.queue_dir, "/tmp/q")
        self.assertEqual((cfg.tls_cert, cfg.tls_key), ("/tls/c.pem", "/tls/k.pem"))
        self.assertTrue(cfg.trust_proxy)

    def test_nothing_configured_exits_naming_both_variables(self):
        with self.assertRaises(SystemExit) as ctx:
            from_env(_env())
        message = str(ctx.exception)
        self.assertIn("RELAY_TENANTS", message)
        self.assertIn("RELAY_KEY_SHA256", message)

    def test_bad_slug_names_the_bad_one(self):
        with self.assertRaises(SystemExit) as ctx:
            from_env(_env(RELAY_TENANTS=f'{{"Acme!": "{GOOD}"}}'))
        self.assertIn("Acme!", str(ctx.exception))

    def test_bad_hash_names_the_bad_one(self):
        with self.assertRaises(SystemExit) as ctx:
            from_env(_env(RELAY_TENANTS='{"acme": "DEADBEEF"}'))
        self.assertIn("acme", str(ctx.exception))

    def test_malformed_tenants_json(self):
        with self.assertRaises(SystemExit):
            from_env(_env(RELAY_TENANTS="acme=" + GOOD))

    def test_non_numeric_port(self):
        with self.assertRaises(SystemExit) as ctx:
            from_env(_env(RELAY_KEY_SHA256=GOOD, RELAY_PORT="http"))
        self.assertIn("RELAY_PORT", str(ctx.exception))


class BannerTest(unittest.TestCase):
    def test_banner_names_version_port_tls_and_tenants_but_never_keys(self):
        text = banner(Config(tenants={"acme": GOOD, "default": hashlib.sha256(b"k1").hexdigest()},
                             port=8443, tls_cert="/tls/c.pem", tls_key="/tls/k.pem"))
        self.assertEqual(text, f"vigil-relay {__version__} on :8443 (tls on), tenants: acme,default")
        self.assertNotIn(GOOD, text)

    def test_banner_reports_tls_off(self):
        text = banner(Config(tenants={"default": GOOD}))
        self.assertIn("(tls off)", text)
        self.assertIn("tenants: default", text)


if __name__ == "__main__":
    unittest.main()
