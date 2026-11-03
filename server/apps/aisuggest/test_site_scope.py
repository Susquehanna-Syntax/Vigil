"""Site scoping of the three AI suggestion endpoints.

A user confined to one site must not be able to feed another site's
hostnames, evidence and CVEs into an AI prompt and read them back. Out-of-site
objects answer 404 — a 403 would confirm they exist.
"""
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role
from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.vulns.models import VulnFinding
from vigil.testsupport import require_business_module

require_business_module()
from apps_business.sites.models import (
    HostSiteAssignment,
    Site,
    UserSiteRole,
)

MITIGATION = "Stop the service that loads the jar."


class AiSuggestSiteScopeTests(TestCase):
    """Dana administers West only. Lab must not appear in any prompt she can
    ask for, and a Lab alert or finding must not look like a Vigil object at
    all from her side."""

    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")

        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)

        self.hosts = {}
        self.findings = {}
        self.alerts = {}
        rule = AlertRule.objects.create(
            name="disk", category="disk", metric="disk_pct", operator="gt",
            threshold=90, severity="critical")
        for site, tag in ((self.west, "west"), (self.lab, "zzlab")):
            host = Host.objects.create(hostname=f"{tag}-host", agent_token=uuid.uuid4().hex)
            HostSiteAssignment.objects.create(host=host, site=site)
            self.hosts[tag] = host
            self.alerts[tag] = Alert.objects.create(
                host=host, rule=rule, severity="critical", message=f"{tag} disk 97% full")
            # Same package and scanner on both hosts, so both share one fix_key.
            self.findings[tag] = VulnFinding.objects.create(
                host=host, scanner="trivy", plugin_id_or_oid="log4j:CVE-2021-44228",
                cve_id="CVE-2021-44228", severity="critical", package_name="log4j-core",
                installed_version="2.14.1", affected_path="opt/app/log4j-core-2.14.1.jar",
                vendor_status="affected", cvss_score=10.0, title="Log4Shell",
                description="Set log4j2.formatMsgNoLookups=true or remove JndiLookup.class.")

        # The group assertions below are only meaningful if both findings are
        # one group; a fixture that silently split them would pass by accident.
        self.assertEqual(self.findings["west"].fix_key, self.findings["zzlab"].fix_key)

        self.client.force_login(self.dana)

    def provider(self, name="local"):
        from .tests import make_provider

        return make_provider(name=name)

    def _capture(self, provider=None):
        """Patch the provider seam and record the prompt it was handed."""
        captured = {}

        def complete(system, prompt):
            captured["system"], captured["prompt"] = system, prompt
            return MITIGATION

        return captured, mock.patch(
            "apps.aisuggest.views.provider_for",
            return_value=mock.Mock(complete=mock.Mock(side_effect=complete)))

    def _post_fix_group(self, provider):
        captured, patcher = self._capture()
        with patcher:
            resp = self.client.post("/api/v1/ai/suggest/fix-group/",
                                    {"provider_id": provider.id,
                                     "fix_key": self.findings["west"].fix_key},
                                    content_type="application/json")
        return resp, captured

    def test_out_of_site_alert_is_404(self):
        provider = self.provider("off-alert")
        captured, patcher = self._capture()
        with patcher as mocked:
            resp = self.client.post(f"/api/v1/ai/suggest/alert/{self.alerts['zzlab'].id}/",
                                    {"provider_id": provider.id})
        self.assertEqual(resp.status_code, 404, resp.content)
        mocked.complete.assert_not_called()
        self.assertEqual(captured, {})
        self.assertTrue(
            Alert.objects.filter(pk=self.alerts['zzlab'].pk).exists(),
            "a 404 must not touch the other site's row")

    def test_out_of_site_finding_is_404(self):
        provider = self.provider("off-finding")
        captured, patcher = self._capture()
        with patcher as mocked:
            resp = self.client.post(f"/api/v1/ai/suggest/vuln/{self.findings['zzlab'].id}/",
                                    {"provider_id": provider.id})
        self.assertEqual(resp.status_code, 404, resp.content)
        mocked.complete.assert_not_called()
        self.assertEqual(captured, {})
        self.assertTrue(
            VulnFinding.objects.filter(pk=self.findings['zzlab'].pk).exists(),
            "a 404 must not touch the other site's row")

    def test_fix_group_only_names_own_hosts(self):
        resp, captured = self._post_fix_group(self.provider("group"))
        self.assertEqual(resp.status_code, 200, resp.content)
        prompt = captured["prompt"]
        self.assertIn("west-host", prompt)
        self.assertNotIn("zzlab-host", prompt)

    def test_fix_group_only_in_other_site_is_404(self):
        self.findings["west"].delete()
        resp, captured = self._post_fix_group(self.provider("labonly"))
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(captured, {}, "the model is never asked")

    def test_own_site_still_works(self):
        provider = self.provider("own")
        for name, url in (("alert", f"/api/v1/ai/suggest/alert/{self.alerts['west'].id}/"),
                          ("finding", f"/api/v1/ai/suggest/vuln/{self.findings['west'].id}/")):
            with self.subTest(name=name):
                captured, patcher = self._capture()
                with patcher:
                    resp = self.client.post(url, {"provider_id": provider.id})
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertIn("west-host", captured["prompt"])
                self.assertNotIn("zzlab-host", captured["prompt"])

    def test_unscoped_staff_sees_every_site(self):
        boss = get_user_model().objects.create_user("boss", password="x", is_staff=True)
        self.client.force_login(boss)
        resp, captured = self._post_fix_group(self.provider("all"))
        self.assertEqual(resp.status_code, 200, resp.content)
        prompt = captured["prompt"]
        self.assertIn("west-host", prompt)
        self.assertIn("zzlab-host", prompt)
