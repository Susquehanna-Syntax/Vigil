"""M9: the assistant mitigates only what cannot be fixed, from what Vigil stored."""
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.vulns.evidence import add_evidence
from apps.vulns.models import VulnFinding

from .tests import make_provider


class MitigationTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_user("root", password="x", is_staff=True)
        self.client.force_login(admin)
        self.provider = make_provider()
        host = Host.objects.create(hostname="app-01", agent_token=uuid.uuid4().hex)
        self.jar = VulnFinding.objects.create(
            host=host, scanner="trivy", plugin_id_or_oid="log4j:CVE-2021-44228",
            cve_id="CVE-2021-44228", severity="critical", package_name="log4j-core",
            installed_version="2.14.1", affected_path="opt/app/log4j-core-2.14.1.jar",
            vendor_status="affected", cvss_score=10.0, title="Log4Shell",
            description="Set log4j2.formatMsgNoLookups=true or remove JndiLookup.class.",
            references=["https://logging.apache.org/log4j/2.x/security.html"])
        add_evidence(self.jar, "process", "9:java", "running as java (pid 9)")
        self.fixable = VulnFinding.objects.create(
            host=host, scanner="vigil", plugin_id_or_oid="pkg:openssl:CVE-1", cve_id="CVE-1",
            severity="high", package_name="openssl", fixed_version="3.0.13")

    def _post(self, finding, captured):
        def complete(system, prompt):
            captured["system"], captured["prompt"] = system, prompt
            return "Remove JndiLookup.class."
        with mock.patch("apps.aisuggest.views.provider_for",
                        return_value=mock.Mock(complete=mock.Mock(side_effect=complete))):
            return self.client.post("/api/v1/ai/suggest/fix-group/",
                                    {"provider_id": self.provider.id, "fix_key": finding.fix_key},
                                    content_type="application/json")

    def test_prompt_is_grounded_and_forbids_upgrades(self):
        captured = {}
        resp = self._post(self.jar, captured)
        self.assertEqual(resp.status_code, 200, resp.content)
        prompt = captured["prompt"]
        for expected in ("NO FIX IS AVAILABLE", "Never propose upgrading", "formatMsgNoLookups",
                         "opt/app/log4j-core-2.14.1.jar", "Vendor status: affected",
                         "CVE-2021-44228", "running as java", "logging.apache.org"):
            self.assertIn(expected, prompt)

    def test_a_group_with_a_fix_is_refused(self):
        captured = {}
        resp = self._post(self.fixable, captured)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(captured, {}, "the model is never asked")

    def test_unknown_group_and_missing_provider(self):
        resp = self.client.post("/api/v1/ai/suggest/fix-group/", {"fix_key": "nope@1"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/api/v1/ai/suggest/fix-group/", {"fix_key": self.jar.fix_key},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
