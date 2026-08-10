"""AI suggestion tests — providers are faked; what's under test is the
untrusted-output handling and the per-provider run/compare contract."""

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host

from .models import AiProvider

GOOD_YAML = """name: Clear apt cache
description: Free disk space by clearing the package cache
risk: low
actions:
  - type: run_command
    params:
      command: apt-get clean
"""

FORBIDDEN_YAML = """name: Sneaky agent swap
risk: low
actions:
  - type: update_agent
    params: {}
"""


def fake_completion(text):
    return mock.patch(
        "apps.aisuggest.views.provider_for",
        return_value=mock.Mock(complete=mock.Mock(return_value=text)),
    )


def make_provider(name="local", model="qwopus", enabled=True):
    return AiProvider.objects.create(name=name, base_url="http://byo.example/v1",
                                     model=model, enabled=enabled)


class SuggestTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.provider = make_provider()
        host = Host.objects.create(hostname="web-01", agent_token=uuid.uuid4().hex)
        rule = AlertRule.objects.create(
            name="disk", category="disk", metric="disk_pct", operator="gt",
            threshold=90, severity="critical")
        self.alert = Alert.objects.create(
            host=host, rule=rule, severity="critical", message="disk 97% full")

    def url(self):
        return f"/api/v1/ai/suggest/alert/{self.alert.id}/"

    def test_valid_yaml_returns_provider_and_timing(self):
        with fake_completion(f"```yaml\n{GOOD_YAML}```\n"):
            resp = self.client.post(self.url(), {"provider_id": self.provider.id})
        self.assertEqual(resp.status_code, 200, resp.content)
        d = resp.json()
        self.assertEqual(d["provider"]["name"], "local")
        self.assertIn("elapsed_ms", d)
        self.assertEqual(len(d["suggestions"]), 1)
        self.assertEqual(d["suggestions"][0]["parsed"]["name"], "Clear apt cache")
        # derived risk reflects the real action registry (run_command is
        # high-risk) — the safety max overrides the model's optimistic label.
        self.assertEqual(d["suggestions"][0]["risk"], "high")

    def test_invalid_and_forbidden_dropped(self):
        text = (f"```yaml\nname: broken\nactions: 'x'\n```\n"
                f"```yaml\n{FORBIDDEN_YAML}```\n"
                f"```yaml\n{GOOD_YAML}```\n")
        with fake_completion(text):
            resp = self.client.post(self.url(), {"provider_id": self.provider.id})
        sug = resp.json()["suggestions"]
        self.assertEqual(len(sug), 1)
        self.assertNotIn("update_agent", sug[0]["yaml"])

    def test_no_providers_is_409(self):
        AiProvider.objects.all().delete()
        resp = self.client.post(self.url(), {"provider_id": 1})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("bring", resp.json()["detail"].lower())

    def test_missing_provider_id_is_400(self):
        resp = self.client.post(self.url())
        self.assertEqual(resp.status_code, 400)

    def test_disabled_provider_is_404(self):
        p = make_provider(name="off", enabled=False)
        resp = self.client.post(self.url(), {"provider_id": p.id})
        self.assertEqual(resp.status_code, 404)

    def test_provider_error_is_502_with_provider_and_timing(self):
        with mock.patch("apps.aisuggest.views.provider_for",
                        return_value=mock.Mock(complete=mock.Mock(
                            side_effect=__import__("apps.aisuggest.providers",
                                                   fromlist=["ProviderError"]).ProviderError("boom")))):
            resp = self.client.post(self.url(), {"provider_id": self.provider.id})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["provider"]["name"], "local")
        self.assertIn("elapsed_ms", resp.json())

    def test_viewer_cannot_suggest(self):
        get_user_model().objects.create_user("v", password="x")
        c = self.client_class()
        c.login(username="v", password="x")
        self.assertEqual(c.post(self.url(), {"provider_id": self.provider.id}).status_code, 403)


class ProviderCrudTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)

    def test_create_list_update_delete_never_leaks_key(self):
        resp = self.client.post("/api/v1/ai/providers/", {
            "name": "qwopus box", "base_url": "http://10.0.0.108:11434/v1",
            "model": "qwopus", "api_key": "sk-secret"})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertNotIn("sk-secret", resp.content.decode())
        self.assertTrue(resp.json()["api_key_set"])
        self.assertTrue(resp.json()["configured"])
        pid = resp.json()["id"]

        listing = self.client.get("/api/v1/ai/providers/").json()
        self.assertEqual(len(listing), 1)

        resp = self.client.patch(f"/api/v1/ai/providers/{pid}/",
                                 {"enabled": False}, content_type="application/json")
        self.assertFalse(resp.json()["enabled"])
        self.assertEqual(AiProvider.objects.get(pk=pid).api_key, "sk-secret")

        self.assertEqual(
            self.client.delete(f"/api/v1/ai/providers/{pid}/").status_code, 204)

    def test_unconfigured_provider_flagged(self):
        resp = self.client.post("/api/v1/ai/providers/", {"name": "empty"})
        self.assertFalse(resp.json()["configured"])

    def test_provider_management_requires_admin(self):
        get_user_model().objects.create_user("v", password="x")
        c = self.client_class()
        c.login(username="v", password="x")
        self.assertEqual(c.get("/api/v1/ai/providers/").status_code, 403)


UPGRADE_YAML = """name: Upgrade openssl
description: Upgrade openssl to the fixed version
risk: standard
actions:
  - type: update_package
    params:
      package_name: openssl
"""


class VulnSuggestTests(TestCase):
    """Remediating a vulnerability is a package operation, not a CVE
    operation. One `update_package openssl` clears every open openssl CVE on
    the host, so a prompt scoped to the single CVE the operator clicked invites
    a suggestion that leaves the rest behind."""

    def setUp(self):
        from apps.vulns.models import VulnFinding

        self.VulnFinding = VulnFinding
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.provider = make_provider()
        self.host = Host.objects.create(
            hostname="web-01", agent_token=uuid.uuid4().hex,
            os="Ubuntu 24.04", tags=["prod"])
        self.finding = self._finding("CVE-2024-0001")

    def _finding(self, cve, package="openssl", fixed="3.0.3", **kw):
        return self.VulnFinding.objects.create(
            host=self.host, scanner="trivy", plugin_id_or_oid=f"{package}:{cve}",
            cve_id=cve, title="heap overflow", severity="critical",
            package_name=package, installed_version="3.0.2",
            fixed_version=fixed, **kw)

    def url(self, finding=None):
        return f"/api/v1/ai/suggest/vuln/{(finding or self.finding).id}/"

    def _prompt(self):
        """Run a suggestion and return the prompt the provider actually saw."""
        captured = {}

        def _complete(system, prompt):
            captured["prompt"] = prompt
            return f"```yaml\n{UPGRADE_YAML}```\n"

        with mock.patch("apps.aisuggest.views.provider_for",
                        return_value=mock.Mock(
                            complete=mock.Mock(side_effect=_complete))):
            self.client.post(self.url(), {"provider_id": self.provider.id})
        return captured["prompt"]

    def test_a_suggestion_comes_back(self):
        with fake_completion(f"```yaml\n{UPGRADE_YAML}```\n"):
            resp = self.client.post(self.url(), {"provider_id": self.provider.id})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.json()["suggestions"]), 1)

    def test_the_prompt_carries_the_package_and_versions(self):
        prompt = self._prompt()
        for expected in ("openssl", "3.0.2", "3.0.3"):
            self.assertIn(expected, prompt, expected)

    def test_the_prompt_carries_the_host_and_cve(self):
        prompt = self._prompt()
        self.assertIn("web-01", prompt)
        self.assertIn("Ubuntu 24.04", prompt)
        self.assertIn("CVE-2024-0001", prompt)

    def test_siblings_on_the_same_package_are_named(self):
        self._finding("CVE-2024-0002")
        self._finding("CVE-2024-0003")
        prompt = self._prompt()
        self.assertIn("CVE-2024-0002", prompt)
        self.assertIn("CVE-2024-0003", prompt)
        self.assertIn("not one per CVE", prompt)

    def test_a_different_package_is_not_pulled_in(self):
        self._finding("CVE-2024-0009", package="libxml2")
        self.assertNotIn("CVE-2024-0009", self._prompt())

    def test_fixed_findings_are_not_named_as_siblings(self):
        self._finding("CVE-2024-0004", state=self.VulnFinding.State.FIXED)
        self.assertNotIn("CVE-2024-0004", self._prompt())

    def test_a_long_sibling_list_is_summarised(self):
        for i in range(20):
            self._finding(f"CVE-2025-{i:04d}")
        prompt = self._prompt()
        self.assertIn("more)", prompt, "must not dump every CVE into the prompt")

    def test_a_finding_with_no_fixed_version_says_so(self):
        f = self._finding("CVE-2024-0100", package="zlib", fixed="")
        captured = {}

        def _complete(system, prompt):
            captured["prompt"] = prompt
            return f"```yaml\n{UPGRADE_YAML}```\n"

        with mock.patch("apps.aisuggest.views.provider_for",
                        return_value=mock.Mock(
                            complete=mock.Mock(side_effect=_complete))):
            self.client.post(self.url(f), {"provider_id": self.provider.id})
        self.assertIn("no upgrade target", captured["prompt"].lower())

    def test_a_finding_with_no_package_asks_for_a_diagnostic(self):
        """Nessus network findings frequently have no package — the prompt
        must not imply a package upgrade that cannot be written."""
        f = self.VulnFinding.objects.create(
            host=self.host, scanner="nessus", plugin_id_or_oid="12345",
            title="TLS 1.0 enabled", severity="medium")
        captured = {}

        def _complete(system, prompt):
            captured["prompt"] = prompt
            return f"```yaml\n{UPGRADE_YAML}```\n"

        with mock.patch("apps.aisuggest.views.provider_for",
                        return_value=mock.Mock(
                            complete=mock.Mock(side_effect=_complete))):
            self.client.post(self.url(f), {"provider_id": self.provider.id})
        self.assertIn("no package", captured["prompt"].lower())
        self.assertIn("diagnostic", captured["prompt"].lower())

    def test_forbidden_actions_are_dropped_here_too(self):
        with fake_completion(f"```yaml\n{FORBIDDEN_YAML}```\n"):
            resp = self.client.post(self.url(), {"provider_id": self.provider.id})
        self.assertEqual(resp.json()["suggestions"], [])

    def test_provider_id_is_required(self):
        self.assertEqual(self.client.post(self.url()).status_code, 400)

    def test_an_unknown_finding_is_404(self):
        resp = self.client.post(f"/api/v1/ai/suggest/vuln/{uuid.uuid4()}/",
                                {"provider_id": self.provider.id})
        self.assertEqual(resp.status_code, 404)

    def test_no_providers_configured_is_409(self):
        AiProvider.objects.all().delete()
        resp = self.client.post(self.url(), {"provider_id": 1})
        self.assertEqual(resp.status_code, 409)
