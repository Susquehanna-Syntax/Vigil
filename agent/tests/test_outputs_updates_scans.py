"""Windows Update, self-update, tag and scan actions report the facts a later step branches on."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, windows_update
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control"}
    base.update(kw)
    return AgentConfig(**base)


UPDATES = [
    {"update_id": "1", "title": "Security update", "kb": "KB1",
     "severity": "critical", "categories": ["Security Updates"],
     "reboot_required": True, "is_downloaded": False, "is_mandatory": True},
    {"update_id": "2", "title": "Low update", "kb": "KB2",
     "severity": "low", "categories": ["Updates"],
     "reboot_required": False, "is_downloaded": False, "is_mandatory": False},
]


class _FakeBackend:
    def __init__(self, updates=None, installed=None, failed=None, reboot=False):
        self._updates = list(updates or [])
        self.installed = list(installed or [])
        self.failed = list(failed or [])
        self.reboot = reboot

    def scan(self, criteria_extra=""):
        return list(self._updates)

    def install(self, update_ids):
        return {"result_code": windows_update.RESULT_SUCCEEDED,
                "reboot_required": self.reboot, "installed": self.installed,
                "failed": self.failed, "detail": "succeeded"}


class WindowsUpdateOutputTests(unittest.TestCase):
    def test_windows_update_scan_reports_count(self):
        with patch.object(windows_update, "detect",
                          return_value=_FakeBackend(UPDATES)):
            out = executor._windows_update_scan({}, _config())
        data = json.loads(out)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["updates"], UPDATES)
        self.assertEqual(out.data, {"count": 2})

    def test_windows_update_install_reports_counts(self):
        backend = _FakeBackend(UPDATES, installed=["1"], failed=["2"], reboot=True)
        with patch.object(windows_update, "detect", return_value=backend):
            out = executor._windows_update_install({}, _config())
        data = json.loads(out)
        self.assertEqual(data["installed"], ["1"])
        self.assertEqual(data["failed"], ["2"])
        self.assertTrue(data["reboot_required"])
        self.assertEqual(out.data, {"installed_count": 1, "failed_count": 1,
                                    "reboot_required": True})

    def test_windows_update_install_reports_zero_counts_when_nothing_matched(self):
        with patch.object(windows_update, "detect",
                          return_value=_FakeBackend(UPDATES)):
            out = executor._windows_update_install(
                {"include_kb": ["KB9999999"]}, _config())
        data = json.loads(out)
        self.assertEqual(data["installed"], [])
        self.assertEqual(data["failed"], [])
        self.assertEqual(out.data, {"installed_count": 0, "failed_count": 0,
                                    "reboot_required": False})


class UpdateAgentOutputTests(unittest.TestCase):
    def _run(self):
        tmp = TemporaryDirectory()
        fake_exe = Path(tmp.name) / "vigil-agent"
        fake_exe.write_bytes(b"\x7fELF" + b"\x00" * 16)
        payload = fake_exe.read_bytes()
        sha = hashlib.sha256(payload).hexdigest()
        resp = mock.MagicMock()
        resp.headers = {"X-Vigil-Version": "9999.9.9"}
        resp.iter_content.return_value = [payload]
        try:
            with (patch.object(executor.sys, "platform", "linux"),
                  patch.object(executor.sys, "argv", [str(fake_exe)]),
                  patch.object(executor.os, "uname",
                               return_value=mock.Mock(machine="x86_64")),
                  patch("requests.get", return_value=resp),
                  patch.object(executor.shutil, "which", return_value=None)):
                return executor._update_agent(
                    {"binary_sha256": {"linux-amd64": sha}}, _config())
        finally:
            tmp.cleanup()

    def test_update_agent_reports_version(self):
        out = self._run()
        self.assertIn("Agent updated to 9999.9.9", str(out))
        self.assertEqual(out.data, {"version": "9999.9.9"})


class TagAndScanRequestOutputTests(unittest.TestCase):
    def test_tag_actions_report_tags(self):
        self.assertEqual(executor._add_tag({"tags": "web, staging"}, _config()).data,
                         {"tags": "web, staging"})
        self.assertEqual(executor._remove_tag({"tags": ["web", "staging"]}, _config()).data,
                         {"tags": "web, staging"})

    def test_scan_requests_report(self):
        self.assertEqual(executor._request_nessus_scan({}, _config()).data,
                         {"requested": True})
        self.assertEqual(executor._request_network_scan({"engine": "greenbone"}, _config()).data,
                         {"engine": "greenbone"})
        self.assertEqual(executor._request_network_scan({}, _config()).data,
                         {"engine": "auto"})


def _canned_raw():
    cve = {"VulnerabilityID": "CVE-2024-0001", "PkgName": "openssl",
           "Severity": "HIGH"}
    return json.dumps({"SchemaVersion": 2, "Trivy": {"Version": "0.73.0"},
                       "Results": [
                           {"Target": "a", "Vulnerabilities": [
                               cve, {**cve, "PkgName": "libxml2"}]},
                           {"Target": "b", "Vulnerabilities": []},
                           {"Target": "c"},
                       ]})


class TrivyOutputTests(unittest.TestCase):
    def test_trivy_counts_vulnerabilities(self):
        raw = _canned_raw()
        with patch.object(executor.shutil, "which",
                          return_value="/usr/bin/trivy"), \
             patch.object(executor, "_run", lambda cmd, timeout=None: raw):
            out = executor._run_trivy_scan({"scope": "fs"}, _config())
        self.assertEqual(out.data, {"vulnerabilities": 2})
        self.assertEqual(str(out), executor._condense_trivy_report(raw))

    def test_trivy_unparseable_is_minus_one(self):
        with patch.object(executor.shutil, "which",
                          return_value="/usr/bin/trivy"), \
             patch.object(executor, "_run",
                          lambda cmd, timeout=None: "trivy: command not found"):
            out = executor._run_trivy_scan({"scope": "fs"}, _config())
        self.assertEqual(out.data, {"vulnerabilities": -1})

    def test_trivy_count_is_minus_one_when_no_results_object(self):
        self.assertEqual(executor._count_trivy_vulnerabilities(
            '{"not": "a report"}'), -1)

    def test_trivy_count_sums_across_results(self):
        self.assertEqual(executor._count_trivy_vulnerabilities(_canned_raw()), 2)
        self.assertEqual(executor._count_trivy_vulnerabilities(
            '{"Results": [{"Vulnerabilities": [1, 2]}, {"Vulnerabilities": [3]}]}'),
            3)

    def test_trivy_count_finds_the_report_among_stderr(self):
        raw = f"WARN\tdb is old\n{_canned_raw()}\nWARN\tdone"
        self.assertEqual(executor._count_trivy_vulnerabilities(raw), 2)

    def test_trivy_text_unchanged(self):
        raw = _canned_raw()
        with patch.object(executor.shutil, "which",
                          return_value="/usr/bin/trivy"), \
             patch.object(executor, "_run", lambda cmd, timeout=None: raw):
            out = executor._run_trivy_scan({"scope": "fs"}, _config())
        self.assertEqual(str(out), executor._condense_trivy_report(raw))


class TrivyDbUpdateOutputTests(unittest.TestCase):
    def test_trivy_db_update_reports_updated(self):
        with patch.object(executor.shutil, "which",
                          return_value="/usr/bin/trivy"), \
             patch.object(executor, "_run", lambda cmd, timeout=None: "ok"):
            out = executor._trivy_db_update({}, _config())
        self.assertEqual(str(out), "ok")
        self.assertEqual(out.data, {"updated": True})


if __name__ == "__main__":
    unittest.main()
