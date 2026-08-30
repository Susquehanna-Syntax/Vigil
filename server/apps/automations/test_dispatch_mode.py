"""Setting an automation's dispatch mode over the API.

This path raised ``NameError: name 'automation' is not defined`` — the setter
assigned to a name that does not exist in the function, so every request
carrying ``dispatch_mode`` returned a 500. Nothing exercised it: the field was
added with the wave work and the UI sent it only from a branch that the
existing tests did not reach.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

from .models import Automation

YAML = "name: T\nrisk: low\nactions:\n  - type: clear_temp_files\n"


class DispatchModeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "dm", "dm@example.com", "x")
        self.client.force_login(self.user)
        self.definition = TaskDefinition.objects.create(
            name="T", owner=self.user, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")

    def _create(self, **extra):
        return self.client.post("/api/v1/automations/", {
            "name": "A", "trigger": "event", "event": "alert_fired",
            "action_kind": "task", "task_definition": str(self.definition.id),
            "target": "event_host", **extra,
        }, content_type="application/json")

    def test_creating_with_a_dispatch_mode_does_not_500(self):
        response = self._create(dispatch_mode="rollout")
        self.assertEqual(response.status_code, 201, response.content[:400])
        self.assertEqual(
            Automation.objects.get(name="A").dispatch_mode, "rollout")

    def test_the_default_is_still_direct(self):
        self.assertEqual(self._create().status_code, 201)
        self.assertEqual(Automation.objects.get(name="A").dispatch_mode, "direct")

    def test_an_invalid_dispatch_mode_is_a_400_not_a_500(self):
        response = self._create(dispatch_mode="sideways")
        self.assertEqual(response.status_code, 400)

    def test_patching_the_dispatch_mode_sticks(self):
        self.assertEqual(self._create().status_code, 201)
        automation = Automation.objects.get(name="A")
        response = self.client.patch(
            f"/api/v1/automations/{automation.id}/",
            {"dispatch_mode": "rollout"}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:400])
        automation.refresh_from_db()
        self.assertEqual(automation.dispatch_mode, "rollout")
