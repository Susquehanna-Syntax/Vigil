"""Trivy ingest must never turn a broken scan into a clean bill of health.

GitHub #19. A report with no Vulnerabilities section is not a clean host —
it is a scan that did not look for vulnerabilities. Treating the two alike
marked every existing finding FIXED and reported success, which is the worst
possible failure mode for a security feature: it destroys real findings and
tells you the host is fine.
"""
import json

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host

from .models import VulnFinding, VulnScan, VulnSummary
from .scanners.trivy import ScanIngestError, TrivyScanner


def vuln_report(*vulns):
    """A real vulnerability report — Vulnerabilities key present."""
    return json.dumps({
        "SchemaVersion": 2,
        "Results": [{"Class": "os-pkgs", "Type": "ubuntu",
                     "Vulnerabilities": list(vulns)}],
    })


def clean_report():
    """A genuine clean scan: the key is present and empty."""
    return json.dumps({
        "SchemaVersion": 2,
        "Results": [{"Class": "os-pkgs", "Type": "ubuntu",
                     "Vulnerabilities": []}],
    })


def sbom_report():
    """What issue #18 observed: packages listed, no Vulnerabilities key."""
    return json.dumps({
        "SchemaVersion": 2,
        "Trivy": {"Version": "0.72.0"},
        "Results": [{"Class": "os-pkgs", "Type": "ubuntu",
                     "Packages": [{"Name": "openssl", "Version": "3.0.2"}]}],
    })


CVE = {
    "VulnerabilityID": "CVE-2024-0001", "PkgName": "openssl",
    "Severity": "HIGH", "Title": "bad thing",
    "InstalledVersion": "3.0.2", "FixedVersion": "3.0.3",
}


class TrivyIngestTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="h1", agent_token="t" * 32, status=Host.Status.ONLINE)
        self.scanner = TrivyScanner()

    def _open_findings(self):
        return VulnFinding.objects.filter(
            host=self.host, scanner=VulnScan.Scanner.TRIVY,
            state=VulnFinding.State.OPEN)

    def test_vulnerabilities_are_ingested(self):
        self.scanner.ingest_report(self.host, vuln_report(CVE))
        self.assertEqual(self._open_findings().count(), 1)

    def test_clean_report_marks_previous_findings_fixed(self):
        self.scanner.ingest_report(self.host, vuln_report(CVE))
        self.scanner.ingest_report(self.host, clean_report())
        self.assertEqual(self._open_findings().count(), 0)

    # ── The dangerous case ───────────────────────────────────────────────
    def test_sbom_report_is_refused(self):
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(self.host, sbom_report())

    def test_sbom_report_does_not_destroy_existing_findings(self):
        """The bug: no Vulnerabilities key produced an empty seen-set, so
        every open finding was marked FIXED and the host looked clean."""
        self.scanner.ingest_report(self.host, vuln_report(CVE))
        self.assertEqual(self._open_findings().count(), 1)
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(self.host, sbom_report())
        self.assertEqual(self._open_findings().count(), 1,
                         "an SBOM-only report must not mark findings fixed")

    def test_sbom_error_names_the_likely_cause(self):
        with self.assertRaises(ScanIngestError) as ctx:
            self.scanner.ingest_report(self.host, sbom_report())
        self.assertIn("inconclusive", str(ctx.exception).lower())

    def test_invalid_json_is_refused(self):
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(self.host, "not json at all")

    def test_invalid_json_does_not_destroy_findings(self):
        self.scanner.ingest_report(self.host, vuln_report(CVE))
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(self.host, "{{{")
        self.assertEqual(self._open_findings().count(), 1)

    def test_empty_results_is_refused(self):
        # No results at all means the scan produced nothing to reason about.
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(
                self.host, json.dumps({"SchemaVersion": 2, "Results": []}))

    # ── Counts and timestamps ────────────────────────────────────────────
    def test_status_reports_counts(self):
        status = self.scanner.ingest_report(self.host, vuln_report(CVE))
        self.assertIn("1", status)
        self.assertIn("finding", status.lower())

    def test_last_scan_at_is_recorded(self):
        """Without this, a Trivy-scanned host is indistinguishable from one
        that has never been scanned — only Nessus used to set it."""
        before = now()
        self.scanner.ingest_report(self.host, vuln_report(CVE))
        summary = VulnSummary.objects.get(host=self.host)
        self.assertIsNotNone(summary.last_scan_at)
        self.assertGreaterEqual(summary.last_scan_at, before)

    def test_clean_scan_also_records_last_scan_at(self):
        self.scanner.ingest_report(self.host, clean_report())
        summary = VulnSummary.objects.get(host=self.host)
        self.assertIsNotNone(summary.last_scan_at)

    def test_refused_report_does_not_record_a_scan_time(self):
        # A refused ingest must not make the host look freshly scanned.
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(self.host, sbom_report())
        summary = VulnSummary.objects.filter(host=self.host).first()
        self.assertTrue(summary is None or summary.last_scan_at is None)


class IngestSurfacingTests(TestCase):
    """The outcome must reach run details, not just the worker log (#19)."""

    def setUp(self):
        from apps.tasks.models import Task

        self.host = Host.objects.create(
            hostname="h2", agent_token="s" * 32, status=Host.Status.ONLINE)
        self.Task = Task

    def _complete(self, output):
        import secrets

        from apps.tasks.views import _maybe_ingest_trivy_report

        task = self.Task.objects.create(
            host=self.host, action="run_trivy_scan",
            state=self.Task.State.COMPLETED, nonce=secrets.token_hex(16),
            result_output=output)
        _maybe_ingest_trivy_report(task, output)
        task.refresh_from_db()
        return task

    def test_success_records_counts_in_run_details(self):
        task = self._complete(vuln_report(CVE))
        self.assertIn("[INGEST]", task.result_output)
        self.assertIn("1 finding", task.result_output)

    def test_refusal_is_visible_in_run_details(self):
        task = self._complete(sbom_report())
        self.assertIn("[INGEST FAILED]", task.result_output)
        self.assertIn("inconclusive", task.result_output)

    def test_refusal_keeps_the_raw_report_for_diagnosis(self):
        task = self._complete(sbom_report())
        self.assertIn("SchemaVersion", task.result_output)

    def test_large_successful_report_is_elided(self):
        padded = json.dumps({
            "SchemaVersion": 2,
            "Padding": "x" * 20000,
            "Results": [{"Class": "os-pkgs", "Vulnerabilities": [CVE]}],
        })
        task = self._complete(padded)
        self.assertIn("elided", task.result_output)
        self.assertLess(len(task.result_output), len(padded))
        self.assertIn("[INGEST]", task.result_output)

    def test_small_successful_report_is_left_intact(self):
        task = self._complete(vuln_report(CVE))
        self.assertNotIn("elided", task.result_output)

    def test_completion_still_succeeds_when_ingest_refuses(self):
        # A bad payload must not poison the task-result path.
        task = self._complete(sbom_report())
        self.assertEqual(task.state, self.Task.State.COMPLETED)


class SbomDiagnosticsTests(TestCase):
    """The refusal must be self-diagnosing (#18): the reported root cause was
    a guess, and the operator needs the facts to check it themselves."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="h3", agent_token="d" * 32, status=Host.Status.ONLINE)

    def _refusal(self):
        with self.assertRaises(ScanIngestError) as ctx:
            TrivyScanner().ingest_report(self.host, sbom_report())
        return str(ctx.exception)

    def test_names_the_trivy_version(self):
        self.assertIn("0.72.0", self._refusal())

    def test_lists_the_keys_that_did_arrive(self):
        self.assertIn("Packages", self._refusal())

    def test_explains_why_it_refused(self):
        self.assertIn("left untouched", self._refusal())


def multistep_transcript(report_json):
    """Exactly what a multi-step _script task returns: the runtime's
    transcript, with each step's output prefixed. This is what ingest has
    actually been receiving in production all along."""
    return f"[OK] refresh_db: ok\n[OK] scan: {report_json}"


class TranscriptExtractionTests(TestCase):
    """Multi-step scans ship a transcript, not raw JSON (the real cause of
    'nothing is ever ingested'). json.loads saw the leading '[', read it as
    a JSON array, and died at char 1."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="h4", agent_token="e" * 32, status=Host.Status.ONLINE)
        self.scanner = TrivyScanner()

    def _open(self):
        return VulnFinding.objects.filter(
            host=self.host, scanner=VulnScan.Scanner.TRIVY,
            state=VulnFinding.State.OPEN)

    def test_transcript_wrapped_report_is_ingested(self):
        self.scanner.ingest_report(
            self.host, multistep_transcript(vuln_report(CVE)))
        self.assertEqual(self._open().count(), 1)

    def test_bare_report_still_works(self):
        self.scanner.ingest_report(self.host, vuln_report(CVE))
        self.assertEqual(self._open().count(), 1)

    def test_transcript_with_a_clean_report(self):
        self.scanner.ingest_report(
            self.host, multistep_transcript(vuln_report(CVE)))
        self.scanner.ingest_report(
            self.host, multistep_transcript(clean_report()))
        self.assertEqual(self._open().count(), 0)

    def test_leading_bracket_no_longer_breaks_parsing(self):
        """Regression marker for the exact production error:
        'Expecting value: line 1 column 2 (char 1)'."""
        from apps.vulns.scanners.trivy import _extract_report

        data = _extract_report(multistep_transcript(vuln_report(CVE)))
        self.assertEqual(data["SchemaVersion"], 2)

    def test_report_with_packages_before_vulnerabilities(self):
        """Trivy emits Packages ahead of Vulnerabilities, so a truncated
        view looks like an SBOM when it is not."""
        import json as _json

        report = _json.dumps({
            "SchemaVersion": 2,
            "Trivy": {"Version": "0.72.0"},
            "Results": [{
                "Class": "os-pkgs", "Type": "ubuntu",
                "Packages": [{"Name": "adduser", "Version": "3.137"}],
                "Vulnerabilities": [CVE],
            }],
        })
        self.scanner.ingest_report(self.host, multistep_transcript(report))
        self.assertEqual(self._open().count(), 1)

    def test_trailing_step_output_after_the_report(self):
        transcript = (f"[OK] refresh_db: ok\n[OK] scan: {vuln_report(CVE)}\n"
                      f"[OK] cleanup: done")
        self.scanner.ingest_report(self.host, transcript)
        self.assertEqual(self._open().count(), 1)

    def test_output_with_no_json_at_all_is_refused(self):
        with self.assertRaises(ScanIngestError) as ctx:
            self.scanner.ingest_report(
                self.host, "[ERROR] scan: trivy: command not found")
        self.assertIn("No Trivy JSON report", str(ctx.exception))

    def test_refusal_shows_what_it_actually_got(self):
        with self.assertRaises(ScanIngestError) as ctx:
            self.scanner.ingest_report(self.host, "[ERROR] scan: boom")
        self.assertIn("boom", str(ctx.exception))

    def test_empty_output_is_refused(self):
        with self.assertRaises(ScanIngestError):
            self.scanner.ingest_report(self.host, "   ")
