"""Sending the software list: pending payload, `app_inventory`, and the wire.

The collect() call itself is tested in test_software_linux / test_software_windows;
this file covers the plumbing around it — what waits to be sent, what the
`app_inventory` action reports about it, and when the key goes on the check-in.

`software.collect` is patched throughout: these tests never run a package
manager.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# The package import needs the path above the tests package on it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import client, software
from vigil_agent import config as config_mod
from vigil_agent.config import AgentConfig
from vigil_agent.executor import _app_inventory


def _config(**overrides):
    kwargs = {"server_url": "http://s", "agent_token": "t"}
    kwargs.update(overrides)
    return AgentConfig(**kwargs)


def _payload(*names):
    return {
        "digest": "d" * 64,
        "collected_at": "2026-09-29T12:00:00Z",
        "items": [
            {"source": "apt", "id": n, "name": n, "version": "1.0",
             "latest": "", "scope": "machine", "user": "", "publisher": "",
             "managed": True}
            for n in names
        ],
        "errors": [],
    }


class PendingPayloadTests(unittest.TestCase):
    def tearDown(self):
        software._pending = None

    def test_collect_now_and_take_pending(self):
        with patch.object(software, "collect", return_value=_payload("curl")):
            collected = software.collect_now()
            self.assertEqual([i["id"] for i in collected["items"]], ["curl"])
            first = software.take_pending()
            self.assertIsNotNone(first)
            self.assertEqual(first["digest"], collected["digest"])
            self.assertIsNone(software.take_pending())

    def test_take_pending_is_empty_before_the_first_collect(self):
        self.assertIsNone(software.take_pending())


class AppInventoryHandlerTests(unittest.TestCase):
    def tearDown(self):
        software._pending = None

    def test_app_inventory_outputs(self):
        items = [
            # current, managed
            {"source": "apt", "id": "curl", "name": "curl", "version": "8.9",
             "latest": "8.9", "scope": "machine", "user": "", "publisher": "",
             "managed": True},
            # outdated, managed
            {"source": "apt", "id": "openssl", "name": "openssl",
             "version": "3.0", "latest": "3.5", "scope": "machine", "user": "",
             "publisher": "", "managed": True},
            # unmanaged registry entry, no version information at all
            {"source": "registry", "id": "{GUID}", "name": "Legacy App",
             "version": "", "latest": "", "scope": "machine", "user": "",
             "publisher": "", "managed": False},
        ]
        with patch.object(software, "collect", return_value={
                "digest": "e" * 64, "collected_at": "2026-09-29T12:00:00Z",
                "items": items, "errors": ["snap"]}):
            out = _app_inventory({}, _config())
        self.assertEqual(
            out.data,
            {"count": 3, "outdated": 1, "unmanaged": 1, "errors": 1},
        )
        self.assertIn("3 installed item(s), 1 outdated, 1 unmanaged", str(out))
        self.assertIn("errors: snap", str(out))
        self.assertIsNotNone(software.take_pending())

    def test_app_inventory_ignores_params_and_omits_the_error_clause(self):
        with patch.object(software, "collect", return_value=_payload("curl")):
            out = _app_inventory({"package_name": "whatever"}, _config())
        self.assertEqual(
            out.data, {"count": 1, "outdated": 0, "unmanaged": 0, "errors": 0},
        )
        self.assertNotIn("errors:", str(out))


class CheckinPayloadTests(unittest.TestCase):
    def setUp(self):
        sent = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        def fake_post(url, json=None, headers=None, timeout=None):
            sent.clear()
            sent.update(json)
            return _Resp()

        self.sent = sent
        self._post = patch.object(client.requests, "post", fake_post)
        self._post.start()
        self.addCleanup(self._post.stop)

    def test_checkin_includes_software_only_when_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(data_dir=Path(tmp))
            client.checkin(cfg, metrics=[])
            self.assertNotIn("software", self.sent)

            payload = _payload("curl")
            client.checkin(cfg, metrics=[], software=payload)
            self.assertEqual(self.sent["software"], payload)

            client.checkin(cfg, metrics=[], software=None)
            self.assertNotIn("software", self.sent)


class ConfigTests(unittest.TestCase):
    def _load(self, tmp, body):
        path = Path(tmp) / "agent.yml"
        path.write_text(body, encoding="utf-8")
        return config_mod.load_config(path)

    def test_software_interval_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                self._load(tmp, "server_url: http://s\nsoftware_interval: 899\n")
            self.assertIn("software_interval must be at least 900 seconds",
                          str(ctx.exception))
            self.assertEqual(
                self._load(tmp, "server_url: http://s\n").software_interval,
                21600,
            )
            self.assertEqual(
                self._load(tmp, "server_url: http://s\nsoftware_interval: 900\n"
                            ).software_interval,
                900,
            )

    def test_app_inventory_is_allowlistable(self):
        self.assertIn("app_inventory", config_mod._ALL_ACTIONS)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._load(tmp, "server_url: http://s\nallowlist:\n"
                                  "  - app_inventory\n")
            self.assertEqual(cfg.allowlist, {"app_inventory"})

    def test_app_inventory_is_reachable_through_the_dispatcher(self):
        # A handler nobody registered is an action the server can sign and
        # the agent then refuses as unknown.
        from vigil_agent import executor

        self.assertIs(executor._HANDLERS["app_inventory"], _app_inventory)
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                software, "collect", return_value=_payload("curl")):
            out = executor.execute_action(
                "app_inventory", {}, _config(allowlist={"app_inventory"},
                                             data_dir=Path(tmp)),
            )
        self.assertEqual(out.data["count"], 1)
        software._pending = None


if __name__ == "__main__":
    unittest.main()
