"""Playbooks and automations flatten a task's actions, which would drop an M6
branch flow (running every branch) and a relevant: block (running the fix on
hosts it does not apply to). Until they learn those constructs, tasks that use
them are refused on every path that flattens."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations.engine import _steps_for
from apps.automations.models import Automation
from apps.playbooks.expansion import PlaybookExpandError, expand_actions
from apps.playbooks.models import Playbook, PlaybookStep, build_agent_steps, eligible
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

BRANCHING = (
    "name: Branching\nrisk: low\nactions:\n"
    "  - id: svc\n    type: hunt_service\n    params:\n      name: cron\n"
    "  - if: steps.svc.result.count > 0\n    then:\n"
    "      - id: a\n        type: check_service\n        params:\n          service_name: cron\n"
    "    else:\n"
    "      - id: b\n        type: check_service\n        params:\n          service_name: ssh\n")
RELEVANT = (
    "name: Relevant\nrisk: low\nrelevant:\n  all:\n    - hunt_process:\n        name: cron\n"
    "actions:\n  - id: a\n    type: check_service\n    params:\n      service_name: cron\n")
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

    def test_eligible_refuses_branches_and_relevant(self):
        for src in (BRANCHING, RELEVANT):
            ok, why = eligible(_definition(self.user, src))
            self.assertFalse(ok)
            self.assertIn("playbooks cannot run yet", why)
        self.assertTrue(eligible(_definition(self.user, PLAIN))[0])

    def test_flattening_paths_refuse(self):
        d = _definition(self.user, BRANCHING)
        pb = Playbook.objects.create(name="PB", created_by=self.user)
        PlaybookStep.objects.create(playbook=pb, definition=d, order=0)
        with self.assertRaises(PlaybookExpandError):
            build_agent_steps(pb)
        with self.assertRaises(PlaybookExpandError):
            expand_actions([{"type": "playbook", "params": {"name": "PB"}}])

    def test_automation_refuses_and_keeps_step_ids(self):
        a = Automation(action_kind=Automation.ActionKind.TASK,
                       task_definition=_definition(self.user, RELEVANT))
        self.assertIsNone(_steps_for(a))
        a.task_definition = _definition(self.user, PLAIN)
        steps, _ = _steps_for(a)
        # The task's own ids survive, so its steps.<id> reference still resolves.
        self.assertEqual([s["id"] for s in steps], ["svc", "again"])
