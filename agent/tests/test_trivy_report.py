"""A Trivy report has to survive the trip to the server (GitHub #18, #19).

It never did. ``client.report_result`` capped every task result at 10,000
characters, and a real ``trivy fs /`` report is ~30 MB — measured at
30,159,056 characters on a stock Ubuntu workstation, 379 results and 803
vulnerabilities. What arrived at the server was the first 10,000 characters
of an unterminated JSON object, so ingest could only report that it found no
report at all and the Vulnerabilities tab stayed empty. Every Trivy scan ever
run hit this, including after the transcript-extraction fix, because that fix
addressed the wrapper and not the size.

Two things keep it fixed:

  * the report is condensed to the fields the server ingests before it is
    ever handed to the transport — ``Packages`` alone was 21 MB of the
    23.5 MB, and the server reads none of it;
  * the transport cap is large enough for the result and says so when it
    does bite, instead of silently handing over a fragment.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import client, executor
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t",
                mode="full_control")
    base.update(kw)
    return AgentConfig(**base)


CVE = {
    "VulnerabilityID": "CVE-2024-0001",
    "PkgName": "openssl",
    "PkgIdentifier": {"PURL": "pkg:deb/ubuntu/openssl@3.0.2", "UID": "abc123"},
    "InstalledVersion": "3.0.2",
    "FixedVersion": "3.0.3",
    "Severity": "HIGH",
    "Title": "bad thing",
    "Description": "x" * 4000,
    "References": [f"https://example.com/advisory/{i}" for i in range(40)],
    "CVSS": {"nvd": {"V3Score": 9.8, "V3Vector": "AV:N/AC:L"}},
    "DataSource": {"ID": "ubuntu", "Name": "Ubuntu CVE Tracker"},
    "Layer": {"Digest": "sha256:" + "d" * 64},
}


def report(*results):
    return json.dumps({
        "SchemaVersion": 2,
        "Trivy": {"Version": "0.73.0"},
        "ArtifactName": "/",
        "Results": list(results),
    })


def vuln_result(*vulns):
    return {
        "Target": "ubuntu (ubuntu 24.04)",
        "Class": "os-pkgs",
        "Type": "ubuntu",
        # The SBOM inventory: 21 MB of the 23.5 MB measured, never ingested.
        "Packages": [{"Name": f"pkg{i}", "Version": "1.0",
                      "Licenses": ["GPL-3.0-only", "GFDL-1.2-only"],
                      "Digest": "sha256:" + "a" * 64} for i in range(500)],
        "Vulnerabilities": list(vulns),
    }


def packages_only_result():
    """A result Trivy emitted with no Vulnerabilities key at all."""
    return {
        "Target": "Node.js",
        "Class": "lang-pkgs",
        "Type": "node-pkg",
        "Packages": [{"Name": "left-pad", "Version": "1.3.0"}],
    }


class CondenseTests(unittest.TestCase):
    def test_packages_are_dropped(self):
        out = json.loads(executor._condense_trivy_report(report(vuln_result(CVE))))
        self.assertNotIn("Packages", out["Results"][0])

    def test_the_fields_the_server_ingests_survive(self):
        out = json.loads(executor._condense_trivy_report(report(vuln_result(CVE))))
        vuln = out["Results"][0]["Vulnerabilities"][0]
        for field in ("VulnerabilityID", "PkgName", "Severity", "Title",
                      "InstalledVersion", "FixedVersion"):
            self.assertEqual(vuln[field], CVE[field], field)

    def test_prose_and_references_are_dropped(self):
        out = json.loads(executor._condense_trivy_report(report(vuln_result(CVE))))
        vuln = out["Results"][0]["Vulnerabilities"][0]
        for field in ("Description", "References", "CVSS", "Layer",
                      "DataSource", "PkgIdentifier"):
            self.assertNotIn(field, vuln, field)

    def test_every_result_survives(self):
        """The server's #18/#19 guard reasons over the whole Results list.
        Dropping the ones without findings would turn 'this scan never looked
        for vulnerabilities' into 'nothing was scanned'."""
        raw = report(vuln_result(CVE), packages_only_result())
        out = json.loads(executor._condense_trivy_report(raw))
        self.assertEqual(len(out["Results"]), 2)

    def test_a_missing_vulnerabilities_key_stays_missing(self):
        """That absence is exactly what the SBOM refusal keys off."""
        raw = report(vuln_result(CVE), packages_only_result())
        out = json.loads(executor._condense_trivy_report(raw))
        self.assertIn("Vulnerabilities", out["Results"][0])
        self.assertNotIn("Vulnerabilities", out["Results"][1])

    def test_an_empty_vulnerabilities_list_stays_empty_not_absent(self):
        """A genuinely clean host must keep reading as clean, not as an SBOM."""
        clean = {"Target": "t", "Class": "os-pkgs", "Vulnerabilities": []}
        out = json.loads(executor._condense_trivy_report(report(clean)))
        self.assertEqual(out["Results"][0]["Vulnerabilities"], [])

    def test_report_metadata_survives(self):
        """The SBOM refusal quotes the Trivy version back at the operator."""
        out = json.loads(executor._condense_trivy_report(report(vuln_result(CVE))))
        self.assertEqual(out["SchemaVersion"], 2)
        self.assertEqual(out["Trivy"]["Version"], "0.73.0")

    def test_it_actually_shrinks_the_report(self):
        raw = report(vuln_result(CVE, CVE, CVE))
        condensed = executor._condense_trivy_report(raw)
        self.assertLess(len(condensed), len(raw) / 10,
                        "condensing must be worth doing at all")

    def test_the_result_fits_the_transport_cap(self):
        """The whole point: what comes out must be shippable."""
        raw = report(vuln_result(*([CVE] * 200)))
        condensed = executor._condense_trivy_report(raw)
        self.assertLessEqual(len(condensed), client._MAX_OUTPUT)


class CondensePassthroughTests(unittest.TestCase):
    """When we cannot read it, we must not eat it — the server's diagnostics
    are better than anything we can say here."""

    def test_non_json_passes_through_untouched(self):
        raw = "trivy: command not found"
        self.assertEqual(executor._condense_trivy_report(raw), raw)

    def test_truncated_json_passes_through_untouched(self):
        raw = report(vuln_result(CVE))[:500]
        self.assertEqual(executor._condense_trivy_report(raw), raw)

    def test_empty_output_passes_through(self):
        self.assertEqual(executor._condense_trivy_report(""), "")

    def test_stderr_noise_around_the_report_is_preserved(self):
        """_run returns stdout + stderr concatenated, so a WARN line can sit
        on either side of the JSON. Keep it — it is diagnostic."""
        raw = f"WARN\tdb is old\n{report(vuln_result(CVE))}\nWARN\tdone"
        out = executor._condense_trivy_report(raw)
        self.assertTrue(out.startswith("WARN\tdb is old"))
        self.assertTrue(out.endswith("WARN\tdone"))
        self.assertNotIn("Packages", out)

    def test_the_embedded_report_is_still_parseable_after_condensing(self):
        raw = f"WARN\tdb is old\n{report(vuln_result(CVE))}"
        out = executor._condense_trivy_report(raw)
        start = out.index("{")
        data, _ = json.JSONDecoder().raw_decode(out, start)
        self.assertEqual(data["Results"][0]["Vulnerabilities"][0]["PkgName"],
                         "openssl")


class ScanHandlerTests(unittest.TestCase):
    def test_run_trivy_scan_condenses_before_returning(self):
        raw = report(vuln_result(CVE))
        with patch.object(executor.shutil, "which", return_value="/usr/bin/trivy"), \
             patch.object(executor, "_run", lambda cmd, timeout=None: raw):
            out = executor._run_trivy_scan({"scope": "fs"}, _config())
        self.assertNotIn("Packages", out)
        self.assertIn("CVE-2024-0001", out)


class OutputCapTests(unittest.TestCase):
    """The cap that swallowed every Trivy report for the life of the feature."""

    def test_short_output_is_untouched(self):
        self.assertEqual(client._cap_output("hello"), "hello")

    def test_output_at_the_cap_is_untouched(self):
        text = "x" * client._MAX_OUTPUT
        self.assertEqual(client._cap_output(text), text)

    def test_the_cap_is_large_enough_for_a_condensed_report(self):
        """243,399 characters was the measured condensed size of a 30 MB
        report. A cap under that puts us straight back where we started."""
        self.assertGreaterEqual(client._MAX_OUTPUT, 500_000)

    def test_oversized_output_says_it_was_truncated(self):
        capped = client._cap_output("x" * (client._MAX_OUTPUT + 5000))
        self.assertIn("OUTPUT TRUNCATED", capped)

    def test_the_notice_reports_how_much_was_lost(self):
        capped = client._cap_output("x" * (client._MAX_OUTPUT + 5000))
        self.assertIn("5,000", capped)

    def test_truncation_keeps_the_head_of_the_output(self):
        capped = client._cap_output("HEAD" + "x" * (client._MAX_OUTPUT * 2))
        self.assertTrue(capped.startswith("HEAD"))

    def test_none_is_tolerated(self):
        self.assertEqual(client._cap_output(None), "")

    def test_report_result_sends_the_capped_output(self):
        sent = {}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {}

        def fake_post(url, json=None, headers=None, timeout=None):
            sent.update(json)
            return _Resp()

        with patch.object(client.requests, "post", fake_post):
            client.report_result(_config(), "task-1", "completed",
                                 "y" * (client._MAX_OUTPUT + 10))
        self.assertIn("OUTPUT TRUNCATED", sent["output"])

    def test_report_result_does_not_truncate_a_real_report(self):
        sent = {}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {}

        def fake_post(url, json=None, headers=None, timeout=None):
            sent.update(json)
            return _Resp()

        payload = executor._condense_trivy_report(report(vuln_result(*([CVE] * 200))))
        with patch.object(client.requests, "post", fake_post):
            client.report_result(_config(), "task-1", "completed", payload)
        self.assertNotIn("OUTPUT TRUNCATED", sent["output"])
        self.assertEqual(sent["output"], payload)


if __name__ == "__main__":
    unittest.main()
