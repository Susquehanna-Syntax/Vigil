"""app_install_custom on the agent: download, verify the SHA-256, run silently, clean up.

Nothing here downloads or runs anything real: ``requests.get`` is a fake
streaming response and the two command seams record what they were asked.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
# executor first: the actions modules import from it, and it imports their handlers.
from vigil_agent import executor, pkg_manager, software  # noqa: F401
from vigil_agent.actions import apps
from vigil_agent.config import AgentConfig

BODY = b"MZ" + b"\x00" * 4000 + b"installer"
SHA = hashlib.sha256(BODY).hexdigest()


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.body), 1000):
            yield self.body[i:i + 1000]


class InstallCustomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                                  data_dir=Path(self.tmp.name))
        self.windows_calls, self.linux_calls = [], []
        patchers = (
            mock.patch.object(apps.requests, "get", side_effect=lambda *a, **k: _Response(self.body)),
            mock.patch.object(apps, "_run_windows",
                              side_effect=lambda argv, **k: (self.windows_calls.append(list(argv)) or self.code, "")),
            mock.patch.object(pkg_manager, "_run",
                              side_effect=lambda argv, **k: self.linux_calls.append(list(argv)) or ""),
            mock.patch.object(software, "collect_now", side_effect=lambda: self.collection),
            mock.patch.object(apps.shutil, "which", side_effect=lambda name: "/usr/bin/" + name
                              if name in self.present else None),
            mock.patch.object(apps.sys, "platform", "linux"),
        )
        started = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        self.get = started[0]
        self.platform_patch = patchers[-1]
        self.body, self.code, self.present = BODY, 0, ("apt-get",)
        self.collection = {"items": []}

    def downloads(self):
        d = Path(self.tmp.name) / "downloads"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def windows(self):
        self.platform_patch.stop()
        p = mock.patch.object(apps.sys, "platform", "win32")
        p.start()
        self.addCleanup(p.stop)

    def run_action(self, **params):
        return apps._app_install_custom({"sha256": SHA, **params}, self.config)

    def test_hash_mismatch_runs_nothing_and_deletes(self):
        self.body = BODY + b"tampered"
        with self.assertRaises(ValueError) as ctx:
            self.run_action(url="https://example.com/tool.deb")
        self.assertIn("failed SHA-256 verification", str(ctx.exception))
        self.assertEqual((self.linux_calls, self.windows_calls), ([], []))
        self.assertEqual(self.downloads(), [])

    def test_msi_and_exe_commands_and_cleanup(self):
        self.windows()
        out = self.run_action(url="https://example.com/acme-4.2.msi")
        argv = self.windows_calls[0]
        self.assertEqual(argv[:2], ["msiexec.exe", "/i"])
        self.assertTrue(argv[2].endswith(".msi"))
        self.assertEqual(argv[3:], ["/qn", "/norestart"])
        self.assertEqual(out.data["sha256"], SHA)
        self.windows_calls.clear()
        self.run_action(url="https://example.com/setup.exe", args="/S /norestart")
        self.assertEqual(self.windows_calls[0][1:], ["/S", "/norestart"])
        self.assertEqual(self.downloads(), [], "the installer is deleted after it runs")

    def test_deb_rpm_commands_and_platform_refusal(self):
        self.run_action(url="https://example.com/tool.deb")
        self.assertEqual(self.linux_calls[0][:3], ["apt-get", "install", "-y"])
        self.present = ("dnf",)
        self.run_action(url="https://example.com/tool.rpm")
        self.assertEqual(self.linux_calls[1][:3], ["dnf", "install", "-y"])
        self.get.reset_mock()
        with self.assertRaises(RuntimeError):
            self.run_action(url="https://example.com/acme.msi")
        self.get.assert_not_called()

    def test_size_cap(self):
        with mock.patch.object(apps, "_CUSTOM_MAX_BYTES", 1500):
            with self.assertRaises(RuntimeError) as ctx:
                self.run_action(url="https://example.com/tool.deb")
        self.assertIn("2 GiB", str(ctx.exception))
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.linux_calls, [])

    def test_bad_params_refused_before_download(self):
        for params in ({"url": "http://example.com/tool.deb"},
                       {"url": "https://example.com/tool.deb", "sha256": SHA[:63]},
                       {"url": "https://example.com/tool.deb", "kind": "rpm"},
                       {"url": "https://example.com/download/42"},
                       {"url": "https://example.com/tool.deb", "app": "-oProxy"}):
            with self.subTest(params=params):
                with self.assertRaises(RuntimeError):
                    apps._app_install_custom({"sha256": SHA, **params}, self.config)
        self.windows()
        with self.assertRaises(RuntimeError):
            self.run_action(url="https://example.com/setup.exe", args="/S; calc")
        self.get.assert_not_called()

    def test_outputs(self):
        self.collection = {"items": [{"source": "dpkg", "id": "acme", "version": "4.2.0"}]}
        out = self.run_action(url="https://example.com/acme.deb", app="acme")
        self.assertEqual(out.data, {"sha256": SHA, "installed_version": "4.2.0"})


if __name__ == "__main__":
    unittest.main()
