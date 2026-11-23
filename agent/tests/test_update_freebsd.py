"""update_agent on FreeBSD: the platform it asks the server for.

A FreeBSD host must be told to download the freebsd-amd64 build; falling into
the Linux branch would hand it a Linux ELF binary to swap in over its own
agent, which preflight is left to refuse after a download of the wrong bytes.
"""

import unittest
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# isort: split
from vigil_agent import executor
from vigil_agent.config import AgentConfig


class _DownloadStopped(ConnectionError):
    """Raised once the download URL has been recorded."""


def _config() -> AgentConfig:
    return AgentConfig(server_url="https://vigil.example.com", agent_token="t" * 40,
                       mode="full_control")


class FreeBSDUpdateTests(unittest.TestCase):
    def _platform_for(self, machine: str) -> str:
        """The platform name the update asks for, read off the download URL."""
        uname = type("U", (), {"machine": machine})()
        captured: dict[str, str] = {}

        class Response:
            status_code = 200

            def __init__(self):
                self.headers = {}   # a fresh dict per response, as requests gives

            def raise_for_status(self):
                pass

            def iter_content(self, _size):
                return [b"x"]

        def fake_get(url, *args, **kwargs):
            captured["url"] = url
            raise _DownloadStopped(url)

        with patch.object(executor.sys, "platform", "freebsd14"), \
                patch.object(executor.os, "uname", return_value=uname, create=True), \
                patch("requests.get", fake_get):
            try:
                executor._update_agent({"binary_sha256": {"freebsd-amd64": "a" * 64}}, _config())
            except _DownloadStopped:
                pass
        return captured["url"].split("/agent/download/")[1].rstrip("/")

    def test_an_amd64_freebsd_host_asks_for_the_freebsd_build(self):
        self.assertEqual(self._platform_for("amd64"), "freebsd-amd64")

    def test_x86_64_is_the_same_build(self):
        self.assertEqual(self._platform_for("x86_64"), "freebsd-amd64")

    def test_another_architecture_is_refused_with_a_reason_and_downloads_nothing(self):
        uname = type("U", (), {"machine": "arm64"})()
        with patch.object(executor.sys, "platform", "freebsd14"), \
                patch.object(executor.os, "uname", return_value=uname, create=True), \
                patch("requests.get") as get, \
                self.assertRaises(RuntimeError) as ctx:
            executor._update_agent({}, _config())
        self.assertIn("FreeBSD on arm64", str(ctx.exception))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
