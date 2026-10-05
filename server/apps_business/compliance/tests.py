"""Patch compliance: the Free numbers, and the Business report behind its licence."""
from datetime import timedelta

from apps_business.sites.models import HostSiteAssignment, Site
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.policies.compliance import compliance, sla_days
from apps.software.models import PendingUpdate, UpdateDecision
from apps.vulns.models import RemediationPolicy
from apps_business.audits.tests import PUB, make_blob
from vigil import licensing


def _host(name):
    return Host.objects.create(hostname=name, agent_token=f"tok-{name}",
                               status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)


def _pending(host, key, days, severity="critical", kind="windows"):
    return PendingUpdate.objects.create(
        host=host, kind=kind, key=key, title=key, severity=severity,
        first_seen=now() - timedelta(days=days), last_seen=now())


class ComplianceNumbersTests(TestCase):
    def setUp(self):
        RemediationPolicy.get_active()   # critical 15, high 30, medium 90
        self.ok, self.late, self.declined = _host("a-ok"), _host("b-late"), _host("c-decl")
        _pending(self.ok, "KB1", 3)                       # within 15 days
        _pending(self.late, "KB2", 20)                    # critical, overdue
        _pending(self.late, "KB3", 20, severity="important")  # within 30 days
        _pending(self.declined, "KB9", 400)               # declined: not counted
        UpdateDecision.objects.create(kind="windows", key="KB9", decision="declined")
        _pending(self.ok, "openssl", 95, severity="", kind="linux")  # medium tier: overdue

    def test_sla_tiers(self):
        policy = RemediationPolicy.get_active()
        self.assertEqual(sla_days(policy, "critical"), policy.critical_days)
        self.assertEqual(sla_days(policy, "important"), policy.high_days)
        self.assertEqual(sla_days(policy, ""), policy.medium_days)

    def test_numbers(self):
        data = compliance()
        by_host = {h["hostname"]: h for h in data["hosts"]}
        self.assertEqual(by_host["b-late"]["overdue"], 1)
        self.assertEqual(by_host["a-ok"]["overdue"], 1, "a 95-day Linux package is overdue")
        self.assertEqual(by_host["c-decl"]["pending"], 0)
        self.assertEqual((data["hosts_total"], data["hosts_patched"]), (3, 1))
        self.assertEqual(data["patched_pct"], 33.3)
        self.assertEqual(data["missing_by_age"]["critical"],
                         {"0-7": 1, "8-30": 1, "31-90": 0, "90+": 0})
        self.assertEqual(data["missing_by_age"]["important"]["8-30"], 1)

    def test_free_summary_has_no_per_host_detail(self):
        user = get_user_model().objects.create_user("v", password="x")
        UserProfile.objects.create(user=user, role=Role.VIEWER)
        client = APIClient()
        client.force_authenticate(user)
        body = client.get("/api/v1/policies/compliance/").json()
        self.assertEqual(body["hosts_total"], 3)
        self.assertNotIn("hosts", body)
        self.assertNotIn("sites", body)


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class ComplianceReportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("boss", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        licensing.reload()
        west = Site.objects.create(name="West", slug="west")
        late = _host("west-1")
        HostSiteAssignment.objects.create(host=late, site=west)
        _pending(late, "KB2", 40)
        _host("free-1")

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def _license(self):
        licensing.set_license(make_blob())

    def test_unlicensed_gets_402_everywhere(self):
        for url in ("/api/v1/compliance/report/", "/api/v1/compliance/report.csv",
                    "/api/v1/compliance/report.html"):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 402)
                self.assertEqual(resp.json()["feature"], "compliance_reports")

    def test_report_passes_and_fails_per_site(self):
        self._license()
        body = self.client.get("/api/v1/compliance/report/").json()
        self.assertEqual({s["site"]: s["pass"] for s in body["sites"]},
                         {"Global": True, "West": False})
        self.assertEqual(body["licensed_to"], "t")

    def test_csv_and_branded_html(self):
        self._license()
        from apps_business.branding.models import BrandingConfig
        brand = BrandingConfig.load()
        brand.product_name, brand.accent = "Acme IT", "#123456"
        brand.save()
        csv_body = self.client.get("/api/v1/compliance/report.csv").content.decode()
        self.assertIn("west-1,1,1,40,no", csv_body)
        html = self.client.get("/api/v1/compliance/report.html").content.decode()
        self.assertIn("Licensed to t", html)
        self.assertIn("Acme IT", html)
        self.assertIn("#123456", html)
        self.assertIn("Fail", html)
