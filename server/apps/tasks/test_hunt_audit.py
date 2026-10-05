"""hunt_text_requested: deploying a hunt_content step with `return: text`
emits the hook, and the Business audit app records who asked (M5 phase 05)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate
from vigil import hooks

User = get_user_model()


def content_yaml(return_value):
    return (
        "name: Hunt leaked token\n"
        "risk: standard\n"
        "actions:\n"
        "  - id: token\n"
        "    type: hunt_content\n"
        "    params:\n"
        "      pattern: 'AKIA[0-9A-Z]{16}'\n"
        f"      return: {return_value}\n"
    )


class HuntTextHookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hunter", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="h1", agent_token="tok-hunt-audit",
            status=Host.Status.ONLINE, mode=Host.Mode.FULL_CONTROL)

    def _deploy(self, yaml_source):
        d = TaskDefinition.objects.create(
            owner=self.user, name="Hunt leaked token", yaml_source=yaml_source,
            parsed_spec=parse_and_validate(yaml_source))
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{d.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456"},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()["id"]

    def test_text_request_emits_hook(self):
        seen = []
        with patch.object(hooks, "emit",
                          side_effect=lambda event, **kw: seen.append((event, kw))):
            run_id = self._deploy(content_yaml("text"))
            self._deploy(content_yaml("lines"))
        texts = [kw for event, kw in seen if event == "hunt_text_requested"]
        self.assertEqual(len(texts), 1)
        self.assertEqual(str(texts[0]["run"].id), run_id)
        self.assertEqual(texts[0]["actor"], self.user)
        self.assertEqual(texts[0]["step_ids"], ["token"])

    def test_business_audit_records_text_request(self):
        from apps_business.audits.apps import wire
        from apps_business.audits.models import AuditEvent
        wire()  # other tests may have hooks.clear()ed; re-wiring is idempotent

        run_id = self._deploy(content_yaml("text"))
        event = AuditEvent.objects.get(action="hunt.text_requested")
        self.assertEqual(event.username, "hunter")
        self.assertEqual(event.detail["run_id"], run_id)
        self.assertEqual(event.detail["step_ids"], ["token"])

        self._deploy(content_yaml("lines"))
        self.assertEqual(
            AuditEvent.objects.filter(action="hunt.text_requested").count(), 1)
