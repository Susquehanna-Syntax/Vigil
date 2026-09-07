"""Baseline was renamed to Playbook in 2026.11.0.

Content published to the community repo before the rename, and anything an
operator scripted against the old API path, has to keep working.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.tasks.spec import ACTION_REGISTRY, parse_and_validate


class LegacyActionTypeTests(TestCase):
    def test_registry_only_advertises_the_new_name(self):
        self.assertIn("playbook", ACTION_REGISTRY)
        self.assertNotIn("baseline", ACTION_REGISTRY)

    def test_legacy_baseline_action_type_still_parses(self):
        spec = parse_and_validate(
            "name: Legacy\n"
            "actions:\n"
            "  - type: baseline\n"
            "    params:\n"
            "      name: Linux hardening\n"
        )
        self.assertEqual(spec["actions"][0]["type"], "playbook")

    def test_new_playbook_action_type_parses(self):
        spec = parse_and_validate(
            "name: Current\n"
            "actions:\n"
            "  - type: playbook\n"
            "    params:\n"
            "      name: Linux hardening\n"
        )
        self.assertEqual(spec["actions"][0]["type"], "playbook")


class LegacyApiPathTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="op", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def test_legacy_baselines_path_still_answers(self):
        self.assertEqual(self.client.get("/api/v1/baselines/").status_code, 200)

    def test_new_playbooks_path_answers(self):
        self.assertEqual(self.client.get("/api/v1/playbooks/").status_code, 200)

    def test_reverse_emits_the_new_path_not_the_legacy_one(self):
        self.assertEqual(reverse("playbook-index"), "/api/v1/playbooks/")
