"""AI suggestion tests — the provider is faked; what's under test is the
untrusted-output handling: only valid task YAML survives, update_agent never
does, and misconfiguration/system failure degrade to clear API errors."""

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host

from .models import AiSettings
from .providers import ProviderError

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


class SuggestTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)
        row = AiSettings.get()
        row.base_url = "http://byo.example:11434/v1"
        row.model = "some-local-model"
        row.enabled = True
        row.save()
        host = Host.objects.create(hostname="web-01", agent_token=uuid.uuid4().hex)
        rule = AlertRule.objects.create(
            name="disk", category="disk", metric="disk_pct", operator="gt",
            threshold=90, severity="critical")
        self.alert = Alert.objects.create(
            host=host, rule=rule, severity="critical", message="disk 97% full")

    def url(self):
        return f"/api/v1/ai/suggest/alert/{self.alert.id}/"

    def test_valid_yaml_suggestions_come_back_parsed(self):
        with fake_completion(f"```yaml\n{GOOD_YAML}```\n"):
            resp = self.client.post(self.url())
        self.assertEqual(resp.status_code, 200, resp.content)
        sug = resp.json()["suggestions"]
        self.assertEqual(len(sug), 1)
        self.assertEqual(sug[0]["parsed"]["name"], "Clear apt cache")

    def test_invalid_and_forbidden_suggestions_are_dropped(self):
        text = (f"```yaml\nname: broken\nactions: 'not-a-list'\n```\n"
                f"```yaml\n{FORBIDDEN_YAML}```\n"
                f"```yaml\n{GOOD_YAML}```\n")
        with fake_completion(text):
            resp = self.client.post(self.url())
        sug = resp.json()["suggestions"]
        self.assertEqual(len(sug), 1)
        self.assertNotIn("update_agent", sug[0]["yaml"])

    def test_unconfigured_is_409_with_byo_hint(self):
        row = AiSettings.get()
        row.enabled = False
        row.save()
        resp = self.client.post(self.url())
        self.assertEqual(resp.status_code, 409)
        self.assertIn("bring your own", resp.json()["detail"].lower())

    def test_provider_failure_is_502_not_500(self):
        with mock.patch(
            "apps.aisuggest.views.provider_for",
            return_value=mock.Mock(
                complete=mock.Mock(side_effect=ProviderError("boom"))),
        ):
            resp = self.client.post(self.url())
        self.assertEqual(resp.status_code, 502)

    def test_settings_roundtrip_never_leaks_key(self):
        resp = self.client.post("/api/v1/ai/settings/", {
            "provider": "openai", "base_url": "http://byo.example:11434/v1",
            "model": "m", "api_key": "sk-secret", "enabled": True,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("sk-secret", resp.content.decode())
        self.assertTrue(resp.json()["api_key_set"])
        self.assertEqual(AiSettings.get().api_key, "sk-secret")

    def test_viewer_cannot_call_suggest(self):
        get_user_model().objects.create_user("v", password="x")
        c = self.client_class()
        c.login(username="v", password="x")
        self.assertEqual(c.post(self.url()).status_code, 403)
