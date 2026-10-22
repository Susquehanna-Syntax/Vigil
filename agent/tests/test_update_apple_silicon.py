"""update_agent on an Intel Mac: refused with a reason, before any download."""

import unittest
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# isort: split
from vigil_agent import executor
from vigil_agent.config import AgentConfig


def _config() -> AgentConfig:
    return AgentConfig(server_url="https://vigil.example.com", agent_token="t" * 40,
                       mode="full_control")


class IntelMacUpdateTests(unittest.TestCase):
    def test_an_intel_mac_is_told_why_and_downloads_nothing(self):
        uname = type("U", (), {"machine": "x86_64"})()
        with patch.object(executor.sys, "platform", "darwin"), \
                patch.object(executor.os, "uname", return_value=uname, create=True), \
                patch("requests.get") as get, \
                self.assertRaises(RuntimeError) as ctx:
            executor._update_agent({}, _config())
        self.assertIn("Apple Silicon", str(ctx.exception))
        get.assert_not_called()

    def test_asking_for_the_intel_build_by_name_is_refused_too(self):
        with patch("requests.get") as get, self.assertRaises(RuntimeError):
            executor._update_agent({"platform": "darwin-amd64"}, _config())
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
