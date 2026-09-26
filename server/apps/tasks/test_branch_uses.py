"""``use: <task>`` inside an if/then/else branch (M6 phase 05b).

A branch may name another task; at deploy the server copies that task's
current steps in and signs the composite, so editing the used task later never
changes a run already deployed.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from apps.tasks.spec import SpecError, parse_and_validate
from apps.tasks.uses import UseError, expand_uses
from apps.tasks.views import _save_definition_from_yaml

User = get_user_model()


def _using(used_then, used_else=None, name="Outer"):
    else_part = f"    else:\n      - use: {used_else}\n" if used_else else ""
    return (
        f"name: {name}\nrisk: low\nactions:\n"
        "  - id: svc\n    type: hunt_service\n    params:\n      name: cron\n"
        "  - if: steps.svc.result.count > 0\n    then:\n"
        f"      - use: {used_then}\n" + else_part
    )


def _plain(name, step_id="fix", action="check_service", risk="low"):
    return (
        f"name: {name}\nrisk: {risk}\nactions:\n"
        f"  - id: {step_id}\n    type: {action}\n    params:\n      service_name: cron\n"
    )


class UseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="h1", agent_token="tok-uses", status=Host.Status.ONLINE,
            mode=Host.Mode.FULL_CONTROL)

    def _define(self, yaml_source, owner="me"):
        definition = TaskDefinition(
            owner=self.user if owner == "me" else None,
            visibility=(TaskDefinition.Visibility.COMMUNITY if owner is None
                        else TaskDefinition.Visibility.PRIVATE))
        _save_definition_from_yaml(definition, yaml_source)
        definition.save()
        return definition

    def _deploy(self, definition):
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{definition.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456"},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Task.objects.filter(host=self.host).order_by("-created_at").first()

    def test_use_only_inside_branches(self):
        with self.assertRaisesMessage(SpecError, "use: belongs inside a then/else branch"):
            parse_and_validate("name: t\nactions:\n  - use: Other\n")
        spec = parse_and_validate(_using("Fix it"))
        self.assertEqual(spec["uses"], ["Fix it"])

    def test_save_expands_and_validates(self):
        self._define(_plain("Risky fix", step_id="boom", action="run_command", risk="high")
                     .replace("service_name: cron", "command: echo hi"))
        outer = self._define(_using("Risky fix"))
        self.assertEqual(outer.risk_level, "high")
        # The used task reuses an id the outer task already has.
        self._define(_plain("Clashing", step_id="svc"))
        with self.assertRaisesMessage(SpecError, "duplicate action id 'svc'"):
            self._define(_using("Clashing", name="Outer 2"))

    def test_deploy_copies_current_steps(self):
        used = self._define(_plain("Fix it", step_id="fix"))
        outer = self._define(_using("Fix it"))
        first = self._deploy(outer)
        self.assertEqual([s["id"] for s in first.params["steps"]], ["svc", "fix"])

        _save_definition_from_yaml(used, _plain("Fix it", step_id="fix2"))
        used.save()
        second = self._deploy(outer)
        self.assertEqual([s["id"] for s in second.params["steps"]], ["svc", "fix2"])
        first.refresh_from_db()
        self.assertEqual([s["id"] for s in first.params["steps"]], ["svc", "fix"])

    def test_cycle_refused(self):
        a = TaskDefinition.objects.create(
            owner=self.user, name="A", yaml_source=_using("B", name="A"),
            parsed_spec=parse_and_validate(_using("B", name="A")))
        TaskDefinition.objects.create(
            owner=self.user, name="B",
            yaml_source=_using("A", name="B").replace("id: svc", "id: svc2")
            .replace("steps.svc.", "steps.svc2."),
            parsed_spec={})
        with self.assertRaisesMessage(UseError, "use cycle"):
            expand_uses(a.yaml_source, self.user)

    def test_unknown_and_ambiguous_names(self):
        with self.assertRaisesMessage(UseError, "no task named"):
            expand_uses(_using("Nope"), self.user)
        for _ in range(2):
            TaskDefinition.objects.create(
                owner=None, visibility=TaskDefinition.Visibility.COMMUNITY,
                name="Shared", yaml_source=_plain("Shared"), parsed_spec={})
        with self.assertRaisesMessage(UseError, "ambiguous"):
            expand_uses(_using("Shared"), self.user)

    def test_inputs_or_relevant_in_used_task_refused(self):
        self._define(
            "name: With input\ninputs:\n  - id: svc\n    type: text\n    label: S\n"
            "actions:\n  - id: fix\n    type: check_service\n    params:\n"
            "      service_name: \"${{ inputs.svc }}\"\n")
        with self.assertRaisesMessage(UseError, "declares inputs"):
            expand_uses(_using("With input"), self.user)
        self._define(
            "name: With relevant\nrelevant:\n  all:\n    - hunt_process:\n        name: cron\n"
            "actions:\n  - id: fix\n    type: check_service\n    params:\n      service_name: cron\n")
        with self.assertRaisesMessage(UseError, "declares relevant"):
            expand_uses(_using("With relevant"), self.user)

    def test_own_task_wins_over_community(self):
        TaskDefinition.objects.create(
            owner=None, visibility=TaskDefinition.Visibility.COMMUNITY,
            name="Fix it", yaml_source=_plain("Fix it", step_id="community_fix"),
            parsed_spec={})
        self._define(_plain("Fix it", step_id="my_fix"))
        spec = parse_and_validate(expand_uses(_using("Fix it"), self.user))
        self.assertEqual([a["id"] for a in spec["actions"]], ["svc", "my_fix"])

    def test_params_record_uses(self):
        used = self._define(_plain("Fix it"))
        task = self._deploy(self._define(_using("Fix it")))
        self.assertEqual([u["id"] for u in task.params["uses"]], [str(used.id)])
        self.assertEqual(task.params["uses"][0]["name"], "Fix it")
        self.assertTrue(task.params["uses"][0]["updated_at"])
