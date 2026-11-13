from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.hosts.models import Host

from .models import VulnFinding, VulnScan, VulnScoreHistory
from .scanners.greenbone import _parse_gmp_url
from .scoring import compute_score, recompute_summary


class ScoringTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32, status=Host.Status.ONLINE,
        )

    def _finding(self, scanner, plugin, severity, cve=""):
        return VulnFinding.objects.create(
            host=self.host, scanner=scanner, plugin_id_or_oid=plugin,
            severity=severity, cve_id=cve, state=VulnFinding.State.OPEN,
        )

    def test_compute_score_goes_negative(self):
        # 15 criticals → 100 - 150 = -50, no floor.
        self.assertEqual(compute_score(15, 0, 0, 0), -50)

    def test_same_cve_across_scanners_counted_once(self):
        self._finding(VulnScan.Scanner.NESSUS, "19506", "high", cve="CVE-2026-1")
        self._finding(VulnScan.Scanner.TRIVY, "openssl:CVE-2026-1", "high", cve="CVE-2026-1")
        summary = recompute_summary(self.host)
        self.assertEqual(summary.high, 1)

    def test_cve_dedup_keeps_worst_severity_order_independent(self):
        # Same CVE: Trivy says critical, Greenbone says medium. The
        # dedup must land on critical regardless of row iteration order.
        self._finding(VulnScan.Scanner.GREENBONE, "1.3.6.1", "medium", cve="CVE-2026-9")
        self._finding(VulnScan.Scanner.TRIVY, "pkg:CVE-2026-9", "critical", cve="CVE-2026-9")
        summary = recompute_summary(self.host)
        self.assertEqual(summary.critical, 1)
        self.assertEqual(summary.medium, 0)

    def test_no_cve_findings_not_deduped(self):
        self._finding(VulnScan.Scanner.NESSUS, "1001", "low")
        self._finding(VulnScan.Scanner.NESSUS, "1002", "low")
        summary = recompute_summary(self.host)
        self.assertEqual(summary.low, 2)


class FindingOrderingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32, status=Host.Status.ONLINE,
        )

    def test_critical_sorts_before_medium(self):
        # String ordering would put "medium" above "critical"; the
        # numeric rank annotation must not.
        VulnFinding.objects.create(
            host=self.host, scanner=VulnScan.Scanner.NESSUS, plugin_id_or_oid="m",
            severity="medium", state=VulnFinding.State.OPEN,
        )
        VulnFinding.objects.create(
            host=self.host, scanner=VulnScan.Scanner.NESSUS, plugin_id_or_oid="c",
            severity="critical", state=VulnFinding.State.OPEN,
        )
        resp = self.client.get("/api/v1/vulns/findings/")
        self.assertEqual(resp.data[0]["severity"], "critical")


class GmpUrlParseTests(TestCase):
    def test_host_only_defaults_port(self):
        self.assertEqual(_parse_gmp_url("gvm"), ("gvm", 9390))

    def test_host_port(self):
        self.assertEqual(_parse_gmp_url("gvm:9999"), ("gvm", 9999))

    def test_scheme_stripped(self):
        self.assertEqual(_parse_gmp_url("tls://gvm:9390"), ("gvm", 9390))

    def test_ipv6_bracketed_with_port(self):
        self.assertEqual(_parse_gmp_url("[::1]:9390"), ("::1", 9390))

    def test_ipv6_bare_defaults_port(self):
        self.assertEqual(_parse_gmp_url("fe80::1"), ("fe80::1", 9390))


class GreenboneLaunchTests(TestCase):
    """The GMP launch flow must send a port_list on create_target.

    Since gvmd 20.8, create_target without PORT_LIST/PORT_RANGE is rejected
    with a 400, so a launch that omits it silently fails every scan.
    """

    def setUp(self):
        self.host = Host.objects.create(
            hostname="web1", agent_token="t" * 32, status=Host.Status.ONLINE,
            ip_address="10.0.0.9",
        )
        self.scan = VulnScan.objects.create(
            host=self.host, scanner="greenbone", state=VulnScan.State.REQUESTED,
        )

    def _fake_client(self, sent):
        import xml.etree.ElementTree as ET

        class _FakeClient:
            def send(self, xml_str):
                sent.append(xml_str)
                if xml_str.startswith("<create_target"):
                    return ET.fromstring('<create_target_response status="201" id="tgt-1"/>')
                if xml_str.startswith("<create_task"):
                    return ET.fromstring('<create_task_response status="201" id="task-1"/>')
                if xml_str.startswith("<start_task"):
                    return ET.fromstring(
                        '<start_task_response status="202">'
                        "<report_id>rep-1</report_id></start_task_response>"
                    )
                return ET.fromstring('<r status="200"/>')

        return _FakeClient()

    def test_create_target_includes_port_list(self):
        from .scanners.greenbone import GreenboneScanner

        sent = []
        launched = GreenboneScanner()._launch_pending(self._fake_client(sent))

        self.assertEqual(launched, 1)
        create_target = next(x for x in sent if x.startswith("<create_target"))
        self.assertIn("<port_list id=", create_target)

        self.scan.refresh_from_db()
        self.assertEqual(self.scan.state, VulnScan.State.LAUNCHED)
        self.assertEqual(self.scan.external_scan_id, "task-1")


class VulnApiSchemaShapeTests(TestCase):
    """The bodies the vulns endpoints return are the shapes their schema says."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("shape", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(hostname="shape-host", agent_token="s" * 32,
                                        status=Host.Status.ONLINE)

    def test_list_and_score_endpoints_match_their_schema(self):
        from datetime import timedelta
        from unittest import mock

        from vigil.schema_check import assert_shape

        from .api_schema import VulnErrorResponse, VulnFleetScoreSerializer
        from .models import VulnSummary
        from .serializers import (
            VulnScanSerializer,
            VulnScoreHistorySerializer,
            VulnSummarySerializer,
        )

        VulnFinding.objects.create(host=self.host, scanner="trivy",
                                   plugin_id_or_oid="1", severity="critical")
        recompute_summary(self.host)
        self.assertTrue(VulnSummary.objects.filter(host=self.host).exists())
        assert_shape(self, self.client.get("/api/v1/vulns/").json(),
                     VulnSummarySerializer(many=True))
        assert_shape(self, self.client.get("/api/v1/vulns/score/").json(),
                     VulnFleetScoreSerializer)

        VulnScan.objects.filter(host=self.host).delete()
        with mock.patch("apps.accounts.totp.require_totp_confirmation",
                        return_value=None):
            created = self.client.post(f"/api/v1/vulns/scans/{self.host.id}/",
                                       format="json")
        self.assertEqual(created.status_code, 503)
        assert_shape(self, created.json(), VulnErrorResponse)
        with override_settings(NESSUS_URL="https://n", NESSUS_ACCESS_KEY="a",
                               NESSUS_SECRET_KEY="s"), \
                mock.patch("apps.accounts.totp.require_totp_confirmation",
                           return_value=None):
            scanned = self.client.post(f"/api/v1/vulns/scans/{self.host.id}/",
                                       {"totp": "123456"}, format="json")
        self.assertEqual(scanned.status_code, 201, scanned.data)
        assert_shape(self, scanned.json(), VulnScanSerializer)
        scans = self.client.get("/api/v1/vulns/scans/").json()
        self.assertTrue(scans, "the item shape is only checked with a row")
        assert_shape(self, scans, VulnScanSerializer(many=True))

        VulnScoreHistory.objects.create(
            host=self.host, date=(now() - timedelta(days=1)).date(), score=40)
        history = self.client.get(f"/api/v1/vulns/history/{self.host.id}/").json()
        self.assertTrue(history, "the item shape is only checked with a row")
        assert_shape(self, history, VulnScoreHistorySerializer(many=True))

        with mock.patch("apps.accounts.totp.require_totp_confirmation",
                        return_value=None):
            refused = self.client.post(f"/api/v1/vulns/scans/{self.host.id}/",
                                       {"scanner": "greenbone"}, format="json")
        self.assertEqual(refused.status_code, 503)
        assert_shape(self, refused.json(), VulnErrorResponse)


class FindingListSchemaShapeTests(TestCase):
    """The bodies the finding and summary endpoints return, against schema: a
    finding row whose optional nested rows are null included."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("shapes", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(hostname="shape-findings", agent_token="sf" * 8,
                                        status=Host.Status.ONLINE)

    def test_findings_match_their_schema(self):
        from datetime import timedelta

        from django.utils.timezone import localdate

        from vigil.schema_check import assert_shape

        from .evidence import add_evidence
        from .models import VulnException
        from .serializers import VulnFindingSerializer

        plain = VulnFinding.objects.create(host=self.host, scanner="trivy",
                                           plugin_id_or_oid="1", severity="critical",
                                           cve_id="CVE-2024-0001")
        add_evidence(plain, "package", "dpkg:openssl", "openssl installed")
        VulnFinding.objects.create(host=self.host, scanner="trivy",
                                   plugin_id_or_oid="2", severity="high")
        # Both rows carry an exception: a row with a null nested object would
        # hide whether that field is documented as nullable.
        for finding in VulnFinding.objects.all():
            VulnException.objects.create(
                finding=finding, kind=VulnException.Kind.ACCEPTED,
                reason="known and monitored",
                expires_on=localdate() + timedelta(days=30), created_by=self.user)

        body = self.client.get("/api/v1/vulns/findings/").json()
        self.assertEqual(len(body), 2, "the item shape is only checked with rows")
        assert_shape(self, body, VulnFindingSerializer(many=True))


class ScanCreateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        # Launching a scan needs tasks:run (SEC-1); a bare profile is a viewer.
        profile = UserProfile.objects.create(user=self.user, role=Role.OPERATOR)
        self.secret = generate_secret()
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32, status=Host.Status.ONLINE,
            ip_address="10.0.0.5",
        )

    def test_no_scanner_configured_returns_503(self):
        resp = self.client.post(
            f"/api/v1/vulns/scans/{self.host.id}/",
            {"totp": generate_totp(self.secret)}, format="json",
        )
        self.assertEqual(resp.status_code, 503)

    @override_settings(
        NESSUS_URL="https://n", NESSUS_ACCESS_KEY="a", NESSUS_SECRET_KEY="s",
    )
    def test_defaults_to_configured_nessus(self):
        resp = self.client.post(
            f"/api/v1/vulns/scans/{self.host.id}/",
            {"totp": generate_totp(self.secret)}, format="json",
        )
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        self.assertEqual(resp.data["scanner"], "nessus")

    @override_settings(
        NESSUS_URL="https://n", NESSUS_ACCESS_KEY="a", NESSUS_SECRET_KEY="s",
    )
    def test_unconfigured_explicit_scanner_rejected(self):
        resp = self.client.post(
            f"/api/v1/vulns/scans/{self.host.id}/",
            {"totp": generate_totp(self.secret), "scanner": "greenbone"},
            format="json",
        )
        self.assertEqual(resp.status_code, 503)


class ScoreHistoryRetentionTests(TestCase):
    """The model deferred pruning to "a future maintenance task"."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="box", ip_address="10.0.0.77", agent_token="tok-score",
            mode="managed", status=Host.Status.ONLINE)

    def _snapshot(self, days_ago, score=50):
        from datetime import timedelta

        from django.utils.timezone import now

        return VulnScoreHistory.objects.create(
            host=self.host, date=(now() - timedelta(days=days_ago)).date(),
            score=score)

    def test_snapshots_past_the_window_are_pruned(self):
        from apps.vulns.tasks import prune_old_score_history

        self._snapshot(days_ago=900)
        recent = self._snapshot(days_ago=30)
        prune_old_score_history()
        self.assertEqual(list(VulnScoreHistory.objects.all()), [recent])

    def test_the_long_trend_is_deliberately_kept(self):
        """Two years is generous on purpose — the value of this table is the
        trend, so a year-old snapshot must survive."""
        from apps.vulns.tasks import prune_old_score_history

        self._snapshot(days_ago=365)
        prune_old_score_history()
        self.assertEqual(VulnScoreHistory.objects.count(), 1)

    @override_settings(VIGIL_SCORE_HISTORY_RETENTION_DAYS=0)
    def test_retention_can_be_switched_off(self):
        from apps.vulns.tasks import prune_old_score_history

        self._snapshot(days_ago=5000)
        prune_old_score_history()
        self.assertEqual(VulnScoreHistory.objects.count(), 1)
