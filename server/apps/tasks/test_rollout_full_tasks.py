"""Rollouts build tasks the one way every path does (M6 phase 08a2).

A task rollout's wave tasks carry the task's own branches and step ids; a
playbook rollout gives each wave host a chain — one signed task per playbook
step — and a wave counts hosts, not tasks, when it judges its results.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.playbooks.models import Playbook, PlaybookStep
from apps.tasks.models import PatchWave, Task, TaskDefinition
from apps.tasks.rollout import _wave_stats, start_rollout
from apps.tasks.spec import parse_and_validate

BRANCHING = (
    "name: Branching\nrisk: low\nactions:\n"
    "  - id: svc\n    type: hunt_service\n    params:\n      name: cron\n"
    "  - if: steps.svc.result.count > 0\n    then:\n"
    "      - id: a\n        type: check_service\n        params:\n          service_name: cron\n"
)
PLAIN = ("name: Plain\nrisk: low\nactions:\n"
         "  - id: p\n    type: check_service\n    params:\n      service_name: cron\n")


def _definition(user, source):
    spec = parse_and_validate(source)
    return TaskDefinition.objects.create(owner=user, name=spec["name"],
                                         yaml_source=source, parsed_spec=spec)


class RolloutFullTaskTests(TestCase):
    def setUp(self):
        from apps.hosts.models import Host

        self.user = get_user_model().objects.create_user("op", password="pw")
        PatchWave.objects.create(name="All", order=1, tags=["wave:all"],
                                 validation_hours=0)
        self.hosts = [
            Host.objects.create(hostname=f"h{i}", agent_token=f"tok-rollout-{i}-0123",
                                status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
                                tags=["wave:all"])
            for i in range(2)
        ]

    def test_task_rollout_uses_builder(self):
        start_rollout(definition=_definition(self.user, BRANCHING), user=self.user)
        tasks = list(Task.objects.all())
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.assertIn("flow", task.params)
            self.assertEqual([s["id"] for s in task.params["steps"]], ["svc", "a"])

    def _playbook(self):
        pb = Playbook.objects.create(name="PB", created_by=self.user)
        PlaybookStep.objects.create(playbook=pb, definition=_definition(self.user, BRANCHING), order=0)
        PlaybookStep.objects.create(playbook=pb, definition=_definition(self.user, PLAIN), order=1)
        return pb

    def test_playbook_rollout_creates_chains(self):
        start_rollout(playbook=self._playbook(), user=self.user)
        for host in self.hosts:
            chain = list(Task.objects.filter(host=host).order_by("step_order"))
            self.assertEqual([t.state for t in chain], [Task.State.PENDING, Task.State.BLOCKED])
            self.assertIn("flow", chain[0].params)

    def test_wave_stats_count_hosts_not_tasks(self):
        rollout = start_rollout(playbook=self._playbook(), user=self.user)
        a, b = ([*Task.objects.filter(host=h).order_by("step_order")] for h in self.hosts)
        # Host A finished its whole chain; host B only its first step.
        for task in (*a, b[0]):
            task.state = Task.State.COMPLETED
            task.save()
        self.assertEqual(_wave_stats(rollout), {"total": 2, "reported": 1, "failed": 0})
        # B's second step fails: B has now reported, and failed.
        b[1].state = Task.State.FAILED
        b[1].save()
        self.assertEqual(_wave_stats(rollout), {"total": 2, "reported": 2, "failed": 1})
