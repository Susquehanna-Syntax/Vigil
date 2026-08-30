"""Escalated scoring: per-finding weights, dedup, summary counts, and the
finding-list read surface (filters + sort allowlist).

Dates are set explicitly via queryset updates so no test depends on the
wall clock: ``save()`` derives a due date from ``first_seen`` + policy,
which is the right behaviour in production but the wrong thing to
reason about here.
"""

import itertools

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import localdate

from rest_framework.test import APIClient

from apps.hosts.models import Host

from .models import VulnException, VulnFinding, VulnScan, VulnScoreHistory, VulnSummary
from .scoring import compute_score, recompute_summary

_host_counter = itertools.count()


def _host(hostname="esc"):
    token = f"tok-{next(_host_counter)}-{hostname}".ljust(32, "x")[:32]
    return Host.objects.create(hostname=hostname, ip_address="10.0.0.9", agent_token=token)


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


def _set_due_date(finding, days_from_today):
    """Move a finding to an exact point on the clock, bypassing save()."""
    VulnFinding.objects.filter(pk=finding.pk).update(
        due_date=localdate() + timedelta(days=days_from_today)
    )
    finding.refresh_from_db()
    return finding


def _clear_due_date(finding):
    """Force a NULL due date (info-level findings are the natural case)."""
    VulnFinding.objects.filter(pk=finding.pk).update(due_date=None)
    finding.refresh_from_db()
    return finding


class EscalatedScoringTests(TestCase):
    def setUp(self):
        self.host = _host()

    def test_overdue_critical_scores_worse_than_fresh_critical(self):
        """The acceptance criterion: the same severity at two points on the
        clock must not read the same."""
        # Host A: two criticals both due in 60 days — flat band, x1.0 each.
        host_a = _host("a")
        _set_due_date(_finding(host_a, plugin="a1"), 60)
        _set_due_date(_finding(host_a, plugin="a2"), 60)
        # Host B: one critical due in 60 days, one 45 days overdue (x4.0).
        host_b = _host("b")
        _set_due_date(_finding(host_b, plugin="b1"), 60)
        _set_due_date(_finding(host_b, plugin="b2"), -45)

        score_a = recompute_summary(host_a).score
        score_b = recompute_summary(host_b).score

        # A: 100 - round(10×0.1 + 10×0.1) = 98 — two criticals, both well
        # inside their window, cost almost nothing.
        # B: 100 - round(10×0.1 + 10×4.0) = 59 — one of them badly overdue.
        self.assertEqual(score_a, 98)
        self.assertEqual(score_b, 59)
        self.assertLess(score_b, score_a)

    def test_excepted_finding_does_not_escalate(self):
        """An accepted risk hits the score at base weight, not the curve."""
        fresh = _set_due_date(_finding(self.host, plugin="fresh"), -45)
        excepted = _set_due_date(_finding(self.host, plugin="excepted"), -45)
        VulnException.objects.create(
            finding=excepted,
            kind=VulnException.Kind.ACCEPTED,
            reason="Compensating control in place.",
            expires_on=localdate() + timedelta(days=30),
        )

        summary = recompute_summary(self.host)

        # Without the exception: 100 - round(10×4.0 + 10×4.0) = 20.
        # With it: 100 - round(10×4.0 + 10×0.1) = 59 — the accepted risk drops
        # to the same weight as a finding with runway.
        self.assertEqual(summary.score, 59)
        self.assertEqual(summary.overdue_count, 1)
        self.assertEqual(summary.due_soon_count, 0)
        # The exception itself must still be readable on the finding.
        self.assertTrue(excepted.is_excepted)
        self.assertTrue(fresh.overdue)

    def test_dedup_keeps_soonest_due_date(self):
        """Two scanners, one CVE, different dates: the score must match the
        sooner date, not the row that happens to carry the worst severity."""
        # The overdue row is the *less* severe one — worst-severity dedup
        # alone would keep the fresh date and launder the overdue one out.
        _set_due_date(
            _finding(self.host, severity="critical", cve_id="CVE-2026-1", plugin="nessus:1"),
            60,
        )
        _set_due_date(
            _finding(self.host, severity="high", cve_id="CVE-2026-1", plugin="trivy:1"),
            -45,
        )
        summary = recompute_summary(self.host)

        # Deduped as one critical (worst severity) at the sooner date
        # (-45 days, x4.0): 100 - round(10×4.0) = 60. Had the 60-day date
        # won it would have been 99 — which is the whole point: laundering an
        # overdue finding onto a later date is now worth 39 points, not 30.
        self.assertEqual(summary.score, 60)
        self.assertEqual(summary.critical, 1)
        self.assertEqual(summary.high, 0)

    def test_dedup_still_keeps_worst_severity(self):
        """The pre-change dedup behaviour must not regress."""
        _set_due_date(
            _finding(self.host, severity="medium", cve_id="CVE-2026-9", plugin="g:1"),
            60,
        )
        _set_due_date(
            _finding(self.host, severity="critical", cve_id="CVE-2026-9", plugin="t:1"),
            60,
        )
        summary = recompute_summary(self.host)
        self.assertEqual(summary.critical, 1)
        self.assertEqual(summary.medium, 0)
        # One critical, 60 days of runway: 100 - round(10×0.1) = 99.
        self.assertEqual(summary.score, 99)

    def test_summary_counts_unchanged_by_escalation(self):
        """Counts still equal the pre-change values for the same fixture.

        Two criticals, one high, one medium, one low, one info — plus a
        duplicate CVE — must produce the same summary counts the old
        counting code produced, even though the score now escalates.
        """
        _set_due_date(_finding(self.host, severity="critical", cve_id="CVE-2026-1", plugin="a"), 60)
        _set_due_date(_finding(self.host, severity="critical", cve_id="CVE-2026-2", plugin="b"), -45)
        _set_due_date(_finding(self.host, severity="high", cve_id="CVE-2026-3", plugin="c"), 7)
        _set_due_date(_finding(self.host, severity="medium", cve_id="CVE-2026-4", plugin="d"), 20)
        _set_due_date(_finding(self.host, severity="low", cve_id="CVE-2026-5", plugin="e"), 40)
        _finding(self.host, severity="info", cve_id="CVE-2026-6", plugin="f")
        # Duplicate CVE reported by a second scanner, 45 days overdue —
        # counted once, and the dedup keeps the SOONER date (-45, x4.0).
        _set_due_date(
            _finding(self.host, severity="critical", cve_id="CVE-2026-1", plugin="dup"),
            -45,
        )

        summary = recompute_summary(self.host)

        self.assertEqual(summary.critical, 2)
        self.assertEqual(summary.high, 1)
        self.assertEqual(summary.medium, 1)
        self.assertEqual(summary.low, 1)
        self.assertEqual(summary.info, 1)
        # And the pre-change score for exactly these counts:
        self.assertEqual(compute_score(2, 1, 1, 1), 100 - 24)
        # The escalated score reflects the due dates, not the counts:
        # C-1 at -45d (x4.0), C-2 at -45d (x4.0), high 7d (x0.5),
        # medium 20d (x0.25), low 40d (x0.1), info undated (weight 0).
        expected = 100 - int(round(10 * 4.0 + 10 * 4.0 + 3 * 0.5 + 1 * 0.25 + 0.2 * 0.1))
        self.assertEqual(summary.score, expected)
        # Nearly all of that deduction is the two overdue criticals; the three
        # findings still inside their windows account for under two points.
        self.assertEqual(expected, 18)

    def test_overdue_and_due_soon_counts(self):
        _set_due_date(_finding(self.host, severity="high", cve_id="C-1", plugin="a"), -1)
        _set_due_date(_finding(self.host, severity="high", cve_id="C-2", plugin="b"), -30)
        _set_due_date(_finding(self.host, severity="high", cve_id="C-3", plugin="c"), 0)
        _set_due_date(_finding(self.host, severity="high", cve_id="C-4", plugin="d"), 14)
        _set_due_date(_finding(self.host, severity="high", cve_id="C-5", plugin="e"), 15)
        _finding(self.host, severity="info", cve_id="C-6", plugin="f")  # undated

        excepted = _set_due_date(_finding(self.host, severity="high", cve_id="C-7", plugin="g"), -2)
        VulnException.objects.create(
            finding=excepted,
            kind=VulnException.Kind.DEFERRED,
            reason="Vendor patch pending.",
            expires_on=localdate() + timedelta(days=10),
        )

        summary = recompute_summary(self.host)

        # Overdue: C-1, C-2 (excepted C-7 excluded).
        self.assertEqual(summary.overdue_count, 2)
        # Due soon: 0..14 → C-3 (today), C-4. C-5 at 15 is not due soon.
        self.assertEqual(summary.due_soon_count, 2)


class FindingListFilterTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = _host("flt")
        self._set = _set_due_date

    def _make(self, plugin, severity="high", cve="", due=None):
        f = _finding(self.host, severity=severity, cve_id=cve, plugin=plugin)
        if due is not None:
            self._set(f, due)
        return f

    def test_overdue_filter(self):
        self._make("a", due=-5)
        self._make("b", due=5)
        self._make("c", due=-2)
        excepted = self._make("d", due=-2)
        VulnException.objects.create(
            finding=excepted,
            kind=VulnException.Kind.ACCEPTED,
            reason="Accepted.",
            expires_on=localdate() + timedelta(days=30),
        )

        resp = self.client.get("/api/v1/vulns/findings/", {"overdue": "1"})
        self.assertEqual(resp.status_code, 200)
        plugins = [f["plugin_id_or_oid"] for f in resp.data]
        self.assertEqual(sorted(plugins), ["a", "c"])

    def test_due_within_filter(self):
        self._make("soon", due=7)
        self._make("later", due=60)
        self._make("past", due=-3)

        resp = self.client.get("/api/v1/vulns/findings/", {"due_within": "14"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([f["plugin_id_or_oid"] for f in resp.data], ["soon"])

    def test_due_within_validates_input(self):
        for bad in ("abc", "-5", "0", "3651", "1.5", ""):
            with self.subTest(value=bad):
                resp = self.client.get("/api/v1/vulns/findings/", {"due_within": bad})
                self.assertEqual(resp.status_code, 400)
        # 3650 is valid — the cap is exclusive.
        resp = self.client.get("/api/v1/vulns/findings/", {"due_within": "3650"})
        self.assertEqual(resp.status_code, 200)

    def test_sort_allowlist_rejects_injection(self):
        for bad in ("due_date; DROP TABLE", "../x", "password", "-id", "id", "id; --"):
            with self.subTest(sort=bad):
                resp = self.client.get("/api/v1/vulns/findings/", {"sort": bad})
                self.assertEqual(resp.status_code, 400, getattr(resp, "data", None))

    def test_sort_by_severity_ranks_correctly(self):
        self._make("m", severity="medium")
        self._make("c", severity="critical")
        self._make("i", severity="info")
        self._make("h", severity="high")

        resp = self.client.get("/api/v1/vulns/findings/", {"sort": "severity"})
        self.assertEqual(resp.status_code, 200)
        # Critical before medium: string ordering would have put "medium"
        # (and "high", and "info") above "critical".
        self.assertEqual([f["severity"] for f in resp.data], ["critical", "high", "medium", "info"])

        resp = self.client.get("/api/v1/vulns/findings/", {"sort": "-severity"})
        self.assertEqual([f["severity"] for f in resp.data], ["info", "medium", "high", "critical"])

    def test_null_due_dates_sort_last(self):
        self._make("due-som", due=10)
        self._make("due-soon", due=2)
        _clear_due_date(self._make("undated", due=None))

        resp = self.client.get("/api/v1/vulns/findings/", {"sort": "due_date"})
        self.assertEqual(resp.status_code, 200)
        plugins = [f["plugin_id_or_oid"] for f in resp.data]
        self.assertEqual(plugins, ["due-soon", "due-som", "undated"])

        resp = self.client.get("/api/v1/vulns/findings/", {"sort": "-due_date"})
        plugins = [f["plugin_id_or_oid"] for f in resp.data]
        self.assertEqual(plugins, ["due-som", "due-soon", "undated"])


class FindingListFieldTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = _host("fields")

    def test_finding_serializer_exposes_due_and_exception(self):
        f = _finding(self.host, severity="high", cve_id="CVE-2026-55", plugin="x")
        _set_due_date(f, -3)
        VulnException.objects.create(
            finding=f,
            kind=VulnException.Kind.DEFERRED,
            reason="Patch window next month.",
            expires_on=localdate() + timedelta(days=12),
        )

        resp = self.client.get("/api/v1/vulns/findings/")
        self.assertEqual(resp.status_code, 200)
        payload = resp.data[0]
        self.assertEqual(payload["days_remaining"], -3)
        self.assertFalse(payload["overdue"])  # excepted
        self.assertEqual(payload["exception"]["kind"], "deferred")
        self.assertEqual(payload["exception"]["reason"], "Patch window next month.")
        self.assertIsNotNone(payload["due_date"])

    def test_summary_serializer_exposes_counts(self):
        f = _finding(self.host, severity="critical", cve_id="CVE-2026-56", plugin="y")
        _set_due_date(f, -45)

        recompute_summary(self.host)
        resp = self.client.get("/api/v1/vulns/")
        self.assertEqual(resp.status_code, 200)
        # The API round-trips host ids as UUID objects in parsed test
        # responses — compare string forms, not raw equality.
        row = next(s for s in resp.data if str(s["host"]) == str(self.host.id))
        self.assertEqual(row["overdue_count"], 1)
        self.assertEqual(row["due_soon_count"], 0)
        self.assertEqual(row["score"], 60)

    def test_summary_serializer_exception_null_when_absent(self):
        f = _finding(self.host, severity="high", cve_id="CVE-2026-57", plugin="z")
        _set_due_date(f, 30)

        resp = self.client.get("/api/v1/vulns/findings/")
        self.assertEqual(resp.status_code, 200)
        payload = next(p for p in resp.data if p["plugin_id_or_oid"] == "z")
        self.assertIsNone(payload["exception"])
        self.assertEqual(payload["days_remaining"], 30)
        self.assertFalse(payload["overdue"])


class HistoryBackfillTests(TestCase):
    """Migration 0010 re-scores history rows under the new curve."""

    def setUp(self):
        self.host = _host("hist")
        self.today = localdate()

    def _migration(self):
        import importlib

        return importlib.import_module(
            "apps.vulns.migrations.0010_recompute_score_history_escalated"
        )

    def test_backfill_uses_findings_as_they_were_then(self):
        from django.apps import apps

        from django.utils.timezone import now

        # Finding A: detected 100 days ago, due exactly on (today - 70d) —
        # 70 days overdue today.
        a = _finding(self.host, severity="critical", cve_id="C-A", plugin="a")
        VulnFinding.objects.filter(pk=a.pk).update(
            first_seen=now() - timedelta(days=100),
            due_date=self.today - timedelta(days=70),
        )
        # Finding B: detected 30 days ago, 5 days overdue today.
        b = _finding(self.host, severity="high", cve_id="C-B", plugin="b")
        VulnFinding.objects.filter(pk=b.pk).update(
            first_seen=now() - timedelta(days=30),
            due_date=self.today - timedelta(days=5),
        )
        # Finding C: badly overdue but excepted — base weight, no curve.
        c = _finding(self.host, severity="critical", cve_id="C-C", plugin="c")
        VulnFinding.objects.filter(pk=c.pk).update(
            first_seen=now() - timedelta(days=100),
            due_date=self.today - timedelta(days=45),
        )
        VulnException.objects.create(
            finding=c,
            kind=VulnException.Kind.ACCEPTED,
            reason="Accepted in the test.",
            expires_on=self.today + timedelta(days=30),
        )

        # Historical rows carrying the OLD un-escalated scores.
        history = {
            days_ago: VulnScoreHistory.objects.create(
                host=self.host, date=self.today - timedelta(days=days_ago), score=old,
            )
            for days_ago, old in ((200, 90), (70, 90), (0, 80))
        }

        self._migration().recompute_history(apps, None)

        # 200 days ago: nothing was detected yet → clean score.
        history[200].refresh_from_db()
        self.assertEqual(history[200].score, 100)
        # 70 days ago: A exists and is due exactly that day (x1.0, the
        # anchor); C existed then too (detected 100 days ago) and is excepted,
        # so it counts at the no-escalation weight (x0.1); B was not detected
        # yet. 100 - round(10×1.0 + 10×0.1) = 89.
        history[70].refresh_from_db()
        self.assertEqual(history[70].score, 89)
        # Today: A 70 days overdue (x4.0), B 5 days overdue (x2.5),
        # C excepted (x0.1). That is 48.5, and Python rounds halves to even,
        # so the deduction is 48 and the score is 52 — not 51.
        history[0].refresh_from_db()
        self.assertEqual(history[0].score, 52)

    def test_backfill_is_idempotent(self):
        from django.apps import apps

        a = _finding(self.host, severity="critical", cve_id="C-A", plugin="a")
        _set_due_date(a, -45)
        row = VulnScoreHistory.objects.create(host=self.host, date=self.today, score=90)

        self._migration().recompute_history(apps, None)
        row.refresh_from_db()
        first = row.score
        # 45 days overdue: 100 - round(10×4.0) = 60.
        self.assertEqual(first, 60)
        self._migration().recompute_history(apps, None)
        row.refresh_from_db()
        self.assertEqual(first, row.score)


class SummaryCountConsistencyTests(TestCase):
    """VulnSummary counts must still match what the findings list shows,
    under the pre-change dedup rule: worst severity per CVE, once."""

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = _host("parity")

    def test_summary_counts_match_findings_list(self):
        _set_due_date(_finding(self.host, severity="critical", cve_id="C-1", plugin="a"), 60)
        _set_due_date(_finding(self.host, severity="critical", cve_id="C-1", plugin="b"), -45)
        _set_due_date(_finding(self.host, severity="high", cve_id="C-2", plugin="c"), 60)
        _set_due_date(_finding(self.host, severity="medium", cve_id="C-3", plugin="d"), 60)
        _finding(self.host, severity="info", cve_id="C-4", plugin="e")

        recompute_summary(self.host)

        listing = self.client.get("/api/v1/vulns/findings/").data
        summary = VulnSummary.objects.get(host=self.host)
        # The list shows the raw open findings (5 rows, the CVE-1 dupe
        # included); the summary counts them the same way it always has —
        # one entry per CVE, worst severity. That set is what the counts
        # must equal, unchanged by the escalation work.
        self.assertEqual(len(listing), 5)
        self.assertEqual(summary.critical, 1)
        self.assertEqual(summary.high, 1)
        self.assertEqual(summary.medium, 1)
        self.assertEqual(summary.info, 1)
        # One entry per CVE at the worst severity — the pre-change dedup.
        from .scoring import SEVERITY_RANK

        worst_per_cve: dict[str, str] = {}
        for f in listing:
            key = (f["cve_id"] or f["plugin_id_or_oid"]).strip().upper()
            cur = worst_per_cve.get(key)
            if cur is None or SEVERITY_RANK[f["severity"]] > SEVERITY_RANK[cur]:
                worst_per_cve[key] = f["severity"]
        for name in ("critical", "high", "medium", "info"):
            with self.subTest(severity=name):
                count = sum(1 for s in worst_per_cve.values() if s == name)
                self.assertEqual(getattr(summary, name), count)


class OverdueWeightedScoreTests(TestCase):
    """2026.9.1: the score tracks remediation promises, not finding counts.

    The behaviour these pin is the reason the curve was re-anchored — a host
    that is doing everything right should not read as failing because its
    scanner is thorough.
    """

    def test_a_host_inside_every_window_scores_near_perfect(self):
        host = _host("compliant")
        for i in range(20):
            _set_due_date(_finding(host, severity="critical", plugin=f"c{i}"), 90)
        score = recompute_summary(host).score
        # 20 criticals, every one of them still 90 days from its deadline.
        # Under the old curve this host scored -100.
        self.assertEqual(score, 80)
        self.assertGreater(score, 0)

    def test_one_overdue_critical_outweighs_twenty_compliant_ones(self):
        """The headline claim, stated as a test so it cannot quietly stop
        being true."""
        compliant = _host("many-compliant")
        for i in range(20):
            _set_due_date(_finding(compliant, severity="critical", plugin=f"c{i}"), 90)

        one_overdue = _host("one-overdue")
        _set_due_date(_finding(one_overdue, severity="critical", plugin="x"), -60)

        self.assertLess(recompute_summary(one_overdue).score,
                        recompute_summary(compliant).score)

    def test_accepting_a_risk_never_makes_the_score_worse(self):
        """The inversion this release fixed.

        While findings inside their window weighed the same as excepted ones
        this was trivially true. Once they were discounted, routing exceptions
        through the no-deadline path would have made an accepted risk cost
        more than an ignored one.
        """
        for days in (-45, -1, 0, 7, 90):
            with self.subTest(days=days):
                plain = _host(f"plain{days}")
                _set_due_date(_finding(plain, severity="critical", plugin="p"), days)

                accepted = _host(f"accepted{days}")
                f = _set_due_date(
                    _finding(accepted, severity="critical", plugin="p"), days)
                VulnException.objects.create(
                    finding=f, kind=VulnException.Kind.ACCEPTED,
                    reason="Compensating control in place.",
                    expires_on=localdate() + timedelta(days=30),
                )

                self.assertGreaterEqual(
                    recompute_summary(accepted).score,
                    recompute_summary(plain).score,
                    "accepting a risk must never lower the score")

    def test_overdue_findings_still_drive_the_score_negative(self):
        """Discounting the compliant ones must not defang the overdue ones."""
        host = _host("neglected")
        for i in range(15):
            _set_due_date(_finding(host, severity="critical", plugin=f"o{i}"), -60)
        # 15 × 10 × 4.0 = 600.
        self.assertEqual(recompute_summary(host).score, -500)
