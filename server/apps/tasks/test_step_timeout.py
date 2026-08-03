"""Per-step timeout must actually take effect (GitHub #16).

runtime.py has documented a step-level `timeout:` since it was written, and
`execute_action` accepts a timeout kwarg — but then called
`handler(params, config)` and dropped it on the floor. The server never
emitted the key either, so a long build was killed at the 120s default with
no way to raise it.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from apps.tasks.spec import SpecError, parse_and_validate

User = get_user_model()


def yaml_with(timeout_line):
    return f"""
name: Long build
risk: high
actions:
  - id: build
    type: run_command
    {timeout_line}
    params: {{ command: "make -j8 all" }}
"""


class SpecTimeoutTests(TestCase):
    def test_step_timeout_is_parsed(self):
        spec = parse_and_validate(yaml_with("timeout: 1800"))
        self.assertEqual(spec["actions"][0]["timeout"], 1800)

    def test_absent_timeout_is_none(self):
        spec = parse_and_validate(yaml_with("params: { command: x }").replace(
            "params: { command: x }", ""))
        self.assertIsNone(spec["actions"][0]["timeout"])

    def test_zero_is_rejected(self):
        with self.assertRaises(SpecError):
            parse_and_validate(yaml_with("timeout: 0"))

    def test_negative_is_rejected(self):
        with self.assertRaises(SpecError):
            parse_and_validate(yaml_with("timeout: -5"))

    def test_above_the_cap_is_rejected(self):
        # Matches the agent's own 1..3600 validation — a limit the server
        # accepts but the agent refuses is just a confusing failure later.
        with self.assertRaises(SpecError):
            parse_and_validate(yaml_with("timeout: 7200"))

    def test_cap_boundary_is_accepted(self):
        spec = parse_and_validate(yaml_with("timeout: 3600"))
        self.assertEqual(spec["actions"][0]["timeout"], 3600)

    def test_non_integer_is_rejected(self):
        with self.assertRaises(SpecError):
            parse_and_validate(yaml_with('timeout: "soon"'))


class DeployShipsTimeoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="t", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="h1", agent_token="tok-t1", status=Host.Status.ONLINE,
            mode=Host.Mode.FULL_CONTROL)

    def _deploy(self, yaml_source):
        d = TaskDefinition.objects.create(
            owner=self.user, name="Long build", yaml_source=yaml_source,
            parsed_spec=parse_and_validate(yaml_source))
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{d.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456"},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Task.objects.filter(host=self.host).order_by("-created_at").first()

    def test_timeout_reaches_the_step_payload(self):
        task = self._deploy(yaml_with("timeout: 1800"))
        self.assertEqual(task.params["steps"][0]["timeout"], 1800)

    def test_absent_timeout_is_omitted_not_null(self):
        # The agent falls back to its own default on a missing key; sending
        # an explicit null would be a second thing to reason about.
        task = self._deploy(yaml_with("").replace("    \n", ""))
        self.assertNotIn("timeout", task.params["steps"][0])


# Agent-side enforcement lives in agent/tests/test_step_timeout.py:
# vigil_agent.executor imports psutil, which is in the agent venv only.
