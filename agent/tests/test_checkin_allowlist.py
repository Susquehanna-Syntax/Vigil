"""The check-in carries the agent's allowlist, so the server can warn before a
deploy to a host that would refuse the task (M7 phase 08)."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import client
from vigil_agent.config import AgentConfig


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "ok", "tasks": []}


class CheckinAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.sent = {}

        def fake_post(url, json=None, **kwargs):
            self.sent.clear()
            self.sent.update(json or {})
            return _Resp()

        p = patch.object(client.requests, "post", fake_post)
        p.start()
        self.addCleanup(p.stop)

    def test_payload_carries_sorted_allowlist_and_reprovision_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                              data_dir=Path(tmp), mode="managed",
                              allowlist={"hunt_service", "app_inventory", "check_service"})
            client.checkin(cfg, metrics=[])
        self.assertEqual(self.sent["allowlist"], ["app_inventory", "check_service", "hunt_service"])
        self.assertIs(self.sent["allow_reprovision"], False)

    def test_empty_allowlist_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                              data_dir=Path(tmp), mode="monitor")
            client.checkin(cfg, metrics=[])
        self.assertEqual(self.sent["allowlist"], [])


if __name__ == "__main__":
    unittest.main()
