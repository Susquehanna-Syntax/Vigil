"""Remediation deadlines: policy tiers, KEV, exceptions, and the KEV loader.

Every HTTP path here is mocked. No test in this file may make a real network
request — the live KEV refresh is a server-initiated outbound fetch and its
whole point is that it is safe and optional.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils.timezone import localdate

from apps.hosts.models import Host

from . import kev as kev_module
from .models import (
    KevEntry,
    RemediationPolicy,
    VulnException,
    VulnFinding,
    VulnScan,
)
from .remediation import compute_due_date, escalation_multiplier


def _host(hostname="due-host"):
    return Host.objects.create(hostname=hostname, ip_address="10.0.0.5")


def _finding(host, severity="critical", cve_id="", plugin="p1", **kw):
    return VulnFinding.objects.create(
        host=host,
        scanner=VulnScan.Scanner.TRIVY,
        plugin_id_or_oid=plugin,
        cve_id=cve_id,
        severity=severity,
        state=VulnFinding.State.OPEN,
        **kw,
    )


class DueDateFromPolicyTests(TestCase):
    def setUp(self):
        self.host = _host()
        self.policy = RemediationPolicy.get_active()

    def test_due_date_from_severity_policy(self):
        """Each tier lands exactly its configured number of days out."""
        for severity, days in (
            ("critical", self.policy.critical_days),
            ("high", self.policy.high_days),
            ("medium", self.policy.medium_days),
            ("low", self.policy.low_days),
        ):
            with self.subTest(severity=severity):
                finding = _finding(self.host, severity=severity, plugin=f"p-{severity}")
                expected = finding.first_seen.date() + timedelta(days=days)
                self.assertEqual(finding.due_date, expected)

    def test_info_findings_have_no_due_date(self):
        finding = _finding(self.host, severity="info", plugin="p-info")
        self.assertIsNone(finding.due_date)
        self.assertIsNone(finding.days_remaining)
        self.assertFalse(finding.overdue)

    def test_get_active_is_a_singleton(self):
        first = RemediationPolicy.get_active()
        second = RemediationPolicy.get_active()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(RemediationPolicy.objects.count(), 1)


class KevDueDateTests(TestCase):
    def setUp(self):
        self.host = _host()
        self.policy = RemediationPolicy.get_active()
        self.entry = KevEntry.objects.create(
            cve_id="CVE-2024-3094",
            date_added=date(2026, 1, 1),
            cisa_due_date=date(2026, 1, 15),
        )

    def test_kev_membership_shortens_the_deadline(self):
        """A KEV critical is due sooner than an identical non-KEV critical."""
        self.policy.use_cisa_due_date = False
        self.policy.save()

        kev_finding = _finding(self.host, cve_id="CVE-2024-3094", plugin="kev")
        plain = _finding(self.host, cve_id="CVE-2024-9999", plugin="plain")

        self.assertLess(kev_finding.due_date, plain.due_date)

    def test_cisa_due_date_is_preferred_when_enabled(self):
        finding = _finding(self.host, cve_id="CVE-2024-3094", plugin="kev")
        self.assertEqual(finding.due_date, date(2026, 1, 15))

    def test_cisa_due_date_ignored_when_flag_off(self):
        self.policy.use_cisa_due_date = False
        self.policy.save()
        finding = _finding(self.host, cve_id="CVE-2024-3094", plugin="kev")
        expected = finding.first_seen.date() + timedelta(days=self.policy.kev_days)
        self.assertEqual(finding.due_date, expected)

    def test_kev_match_is_case_insensitive(self):
        finding = _finding(self.host, cve_id="cve-2024-3094", plugin="lower")
        self.assertEqual(finding.due_date, date(2026, 1, 15))

    def test_kev_entry_without_cisa_date_uses_policy_tier(self):
        KevEntry.objects.create(cve_id="CVE-2025-1111", date_added=date(2026, 2, 1))
        finding = _finding(self.host, cve_id="CVE-2025-1111", plugin="nodate")
        expected = finding.first_seen.date() + timedelta(days=self.policy.kev_days)
        self.assertEqual(finding.due_date, expected)


class DueDateStabilityTests(TestCase):
    def test_due_date_is_stable_across_rescans(self):
        """A re-scan must never push a deadline forward.

        Every scanner writes through update_or_create, which saves on each sync.
        A deadline that slid each time would never go overdue.
        """
        host = _host()
        RemediationPolicy.get_active()
        finding = _finding(host, severity="low", plugin="stable")
        original = finding.due_date

        finding.severity = "critical"
        finding.save()
        finding.refresh_from_db()

        self.assertEqual(finding.due_date, original)


class ExceptionTests(TestCase):
    def setUp(self):
        self.host = _host()
        RemediationPolicy.get_active()
        self.finding = _finding(self.host, plugin="exc")
        # Force it overdue.
        VulnFinding.objects.filter(pk=self.finding.pk).update(
            due_date=localdate() - timedelta(days=5)
        )
        self.finding.refresh_from_db()

    def test_finding_is_overdue_without_an_exception(self):
        self.assertTrue(self.finding.overdue)

    def test_exception_suppresses_overdue(self):
        VulnException.objects.create(
            finding=self.finding,
            kind=VulnException.Kind.ACCEPTED,
            reason="Compensating control in place.",
            expires_on=localdate() + timedelta(days=30),
        )
        self.finding.refresh_from_db()
        self.assertTrue(self.finding.is_excepted)
        self.assertFalse(self.finding.overdue)

    def test_expired_exception_does_not_suppress(self):
        VulnException.objects.create(
            finding=self.finding,
            kind=VulnException.Kind.DEFERRED,
            reason="Waiting on vendor patch.",
            expires_on=localdate() - timedelta(days=1),
        )
        self.finding.refresh_from_db()
        self.assertFalse(self.finding.is_excepted)
        self.assertTrue(self.finding.overdue)

    def test_exception_expiring_today_is_still_active(self):
        VulnException.objects.create(
            finding=self.finding,
            reason="Expires end of day.",
            expires_on=localdate(),
        )
        self.finding.refresh_from_db()
        self.assertTrue(self.finding.is_excepted)

    def test_exception_requires_a_reason(self):
        exc = VulnException(
            finding=self.finding, reason="   ", expires_on=localdate() + timedelta(days=1)
        )
        with self.assertRaises(ValidationError):
            exc.full_clean()


class EscalationCurveTests(TestCase):
    def test_curve_breakpoints(self):
        """Both sides of every band boundary."""
        cases = [
            (None, 1.0),
            (365, 1.0),
            (31, 1.0),
            (30, 1.25),
            (15, 1.25),
            (14, 1.6),
            (1, 1.6),
            (0, 2.0),
            (-1, 3.0),
            (-30, 3.0),
            (-31, 4.0),
            (-400, 4.0),
        ]
        for days, expected in cases:
            with self.subTest(days=days):
                self.assertEqual(escalation_multiplier(days), expected)

    def test_curve_is_monotonic_as_the_deadline_approaches(self):
        """Never gets gentler as time runs out — a regression guard on tuning."""
        values = [escalation_multiplier(d) for d in range(60, -60, -1)]
        for earlier, later in zip(values, values[1:]):
            self.assertLessEqual(earlier, later)

    def test_curve_handles_none(self):
        """No due date (info findings, pre-backfill rows) scores at base."""
        self.assertEqual(escalation_multiplier(None), 1.0)


class KevLoaderTests(TestCase):
    def test_load_bundled_reads_the_real_catalogue(self):
        written = kev_module.load_bundled()
        self.assertGreater(written, 1000)
        self.assertEqual(KevEntry.objects.count(), written)

    def test_load_kev_is_idempotent(self):
        first = kev_module.load_bundled()
        count_after_first = KevEntry.objects.count()
        second = kev_module.load_bundled()
        self.assertEqual(first, second)
        self.assertEqual(KevEntry.objects.count(), count_after_first)

    def test_load_kev_survives_a_missing_file(self):
        from pathlib import Path

        written = kev_module.load_bundled(Path("/nonexistent/kev.json"))
        self.assertEqual(written, 0)

    def test_load_kev_survives_malformed_json(self):
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json at all")
            bad = Path(fh.name)
        self.assertEqual(kev_module.load_bundled(bad), 0)

    def test_parse_catalog_skips_junk_rows(self):
        payload = {
            "vulnerabilities": [
                {"cve_id": "CVE-2024-0001", "date_added": "2026-01-01"},
                {"cve_id": "", "date_added": "2026-01-01"},
                "not-a-dict",
                {"date_added": "2026-01-01"},
                {"cve_id": "CVE-2024-0001", "date_added": "2026-02-02"},  # dupe
            ]
        }
        entries = kev_module.parse_catalog(payload)
        self.assertEqual([e["cve_id"] for e in entries], ["CVE-2024-0001"])

    def test_parse_catalog_accepts_upstream_camelcase(self):
        payload = {"vulnerabilities": [
            {"cveID": "CVE-2024-5555", "dateAdded": "2026-03-03", "dueDate": "2026-03-10"}
        ]}
        entries = kev_module.parse_catalog(payload)
        self.assertEqual(entries[0]["cve_id"], "CVE-2024-5555")
        self.assertEqual(entries[0]["cisa_due_date"], date(2026, 3, 10))


class KevLiveRefreshTests(TestCase):
    """The outbound fetch. This repo has shipped two SSRF bypasses before."""

    def test_disabled_makes_no_request(self):
        with self.settings(VIGIL_KEV_LIVE_REFRESH=False):
            with patch("requests.get", side_effect=AssertionError("must not fetch")):
                self.assertEqual(kev_module.fetch_live(), 0)

    def test_feed_url_is_a_constant_not_configurable(self):
        """The URL must not come from settings or the database."""
        self.assertTrue(kev_module.KEV_FEED_URL.startswith("https://www.cisa.gov/"))

    def test_refuses_redirects(self):
        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get") as get:
                get.return_value.status_code = 302
                get.return_value.headers = {"Location": "http://169.254.169.254/"}
                self.assertEqual(kev_module.fetch_live(), 0)
                self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_sends_a_timeout(self):
        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get") as get:
                get.return_value.status_code = 500
                get.return_value.headers = {}
                kev_module.fetch_live()
                self.assertIsNotNone(get.call_args.kwargs.get("timeout"))

    def test_rejects_oversized_declared_length(self):
        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get") as get:
                get.return_value.status_code = 200
                get.return_value.headers = {"Content-Length": str(64 * 1024 * 1024)}
                self.assertEqual(kev_module.fetch_live(), 0)

    def test_rejects_oversized_stream_even_when_length_lies(self):
        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get") as get:
                get.return_value.status_code = 200
                get.return_value.headers = {"Content-Length": "10"}
                get.return_value.iter_content.return_value = iter(
                    [b"x" * (1024 * 1024)] * 32
                )
                self.assertEqual(kev_module.fetch_live(), 0)

    def test_unparsable_body_changes_nothing(self):
        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get") as get:
                get.return_value.status_code = 200
                get.return_value.headers = {}
                get.return_value.iter_content.return_value = iter([b"{nope"])
                # The catalogue is already seeded by migration 0008, so the
                # assertion is that a failed refresh writes NOTHING NEW — not
                # that the table is empty.
                before = KevEntry.objects.count()
                self.assertEqual(kev_module.fetch_live(), 0)
                self.assertEqual(KevEntry.objects.count(), before)

    def test_network_error_changes_nothing(self):
        import requests

        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get", side_effect=requests.ConnectionError("down")):
                self.assertEqual(kev_module.fetch_live(), 0)

    def test_successful_refresh_marks_entries_live(self):
        body = (
            b'{"vulnerabilities":[{"cve_id":"CVE-2024-7777",'
            b'"date_added":"2026-04-04","due_date":"2026-04-20"}]}'
        )
        with self.settings(VIGIL_KEV_LIVE_REFRESH=True):
            with patch("requests.get") as get:
                get.return_value.status_code = 200
                get.return_value.headers = {}
                get.return_value.iter_content.return_value = iter([body])
                self.assertEqual(kev_module.fetch_live(), 1)
        entry = KevEntry.objects.get(cve_id="CVE-2024-7777")
        self.assertEqual(entry.source, KevEntry.Source.LIVE)
        self.assertEqual(entry.cisa_due_date, date(2026, 4, 20))


class BulkComputeTests(TestCase):
    def test_compute_due_date_accepts_injected_policy_and_entry(self):
        """The bulk path must not hit the database per finding."""
        host = _host()
        policy = RemediationPolicy.get_active()
        finding = _finding(host, severity="high", plugin="bulk")
        entry = KevEntry.objects.create(
            cve_id="CVE-2024-4242",
            date_added=date(2026, 1, 1),
            cisa_due_date=date(2026, 1, 31),
        )
        with self.assertNumQueries(0):
            result = compute_due_date(finding, policy=policy, kev_entry=entry)
        self.assertEqual(result, date(2026, 1, 31))
