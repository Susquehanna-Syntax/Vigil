"""M6 tasks (branches, relevant:, use:) run whole on every dispatch path.

Playbooks, automations and rollouts build each task as its own signed task
(apps/tasks/dispatch.py). The one place a task is still flattened — an inline
`type: playbook` action inside a task body — refuses them instead of dropping
their logic.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations.engine import _build_for
from apps.automations.models import Automation
from apps.playbooks.expansion import PlaybookExpandError, expand_actions
from apps.playbooks.models import Playbook, PlaybookStep, eligible
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

BRANCHING = (
    "name: Branching\nrisk: low\nactions:\n"
    "  - id: svc\n    type: hunt_service\n    params:\n      name: cron\n"
    "  - if: steps.svc.result.count > 0\n    then:\n"
    "      - id: a\n        type: check_service\n        params:\n          service_name: cron\n"
    "    else:\n"
    "      - id: b\n        type: check_service\n        params:\n          service_name: ssh\n")
PLAIN = (
    "name: Plain\nrisk: low\nactions:\n"
    "  - id: svc\n    type: check_service\n    params:\n      service_name: cron\n"
    "  - id: again\n    type: check_service\n    when: steps.svc.result.active == True\n"
    "    params:\n      service_name: cron\n")


def _definition(user, yaml_source):
    spec = parse_and_validate(yaml_source)
    return TaskDefinition.objects.create(owner=user, name=spec["name"],
                                         yaml_source=yaml_source, parsed_spec=spec)


class M6ConstructsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pb", password="pw")

    def test_eligible_accepts_m6_tasks(self):
        self.assertTrue(eligible(_definition(self.user, BRANCHING))[0])

    def test_inline_playbook_expansion_still_refuses(self):
        d = _definition(self.user, BRANCHING)
        pb = Playbook.objects.create(name="PB", created_by=self.user)
        PlaybookStep.objects.create(playbook=pb, definition=d, order=0)
        with self.assertRaises(PlaybookExpandError):
            expand_actions([{"type": "playbook", "params": {"name": "PB"}}])

    def test_automation_carries_flow_and_keeps_step_ids(self):
        a = Automation(action_kind=Automation.ActionKind.TASK,
                       task_definition=_definition(self.user, BRANCHING),
                       created_by=self.user)
        _kind, (params, _risk, _expires), _ = _build_for(a)
        self.assertEqual([s["id"] for s in params["steps"]], ["svc", "a", "b"])
        self.assertIn("flow", params)
        a.task_definition = _definition(self.user, PLAIN)
        _kind, (params, _risk, _expires), _ = _build_for(a)
        # The task's own ids survive, so its steps.<id> reference still resolves.
        self.assertEqual([s["id"] for s in params["steps"]], ["svc", "again"])
