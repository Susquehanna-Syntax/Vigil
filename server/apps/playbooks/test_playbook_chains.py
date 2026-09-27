"""A playbook runs each of its tasks as itself.

Phase 08a: playbook dispatch creates one signed task per playbook step,
chained per host with step_order, each carrying all of that task's own
logic (flow, relevant:, use:) exactly as a direct deploy would. Each step
chooses what "not applicable" means: skip goes on to the next step, stop
ends the host's playbook there.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.playbooks.community_yaml import ContentYamlError, parse, to_yaml
from apps.playbooks.expansion import PlaybookExpandError, expand_actions
from apps.playbooks.models import (
    Playbook,
    PlaybookStep,
    build_agent_steps,
    dispatch_to_host,
    eligible,
)
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate


def _definition(user, yaml_source):
    spec = parse_and_validate(yaml_source)
    return TaskDefinition.objects.create(
        owner=user, name=spec["name"], yaml_source=yaml_source, parsed_spec=spec
    )


def _host(name):
    return Host.objects.create(
        hostname=name,
        ip_address=f"10.41.0.{abs(hash(name)) % 250 + 1}",
        agent_token=f"tok-{name}",
        tags=["prod"],
        mode="managed",
        status=Host.Status.ONLINE,
    )


BRANCHING = (
    "name: Branching\nrisk: low\nactions:\n"
    "  - id: svc\n    type: hunt_service\n    params:\n      name: cron\n"
    "  - if: steps.svc.result.count > 0\n    then:\n"
    "      - id: a\n        type: check_service\n        params:\n          service_name: cron\n"
    "    else:\n"
    "      - id: b\n        type: check_service\n        params:\n          service_name: ssh\n"
)
RELEVANT = (
    "name: Relevant\nrisk: low\nrelevant:\n  all:\n    - hunt_process:\n        name: cron\n"
    "actions:\n  - id: a\n    type: check_service\n    params:\n      service_name: cron\n"
)
PLAIN = (
    "name: Plain\nrisk: low\nactions:\n"
    "  - id: svc\n    type: check_service\n    params:\n      service_name: cron\n"
    "  - id: again\n    type: check_service\n    when: steps.svc.result.active == True\n"
    "    params:\n      service_name: cron\n"
)
OVERRIDABLE = (
    "name: Override\nrisk: low\nactions:\n"
    "  - type: restart_service\n    params:\n      service_name: cron\n"
)


class PlaybookChainDispatchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pb", password="pw")
        self.host = _host("box")
        self.playbook = Playbook.objects.create(
            name="PB", created_by=self.user, completion_tag="hardened"
        )

    def _dispatch(self):
        dispatch_to_host(self.host, playbooks=[self.playbook])
        self.host.refresh_from_db()

    def _states(self):
        return [
            t.state for t in Task.objects.filter(host=self.host).order_by("step_order")
        ]

    def test_dispatch_creates_one_task_per_step_chained(self):
        d1 = _definition(self.user, PLAIN)
        d2 = _definition(self.user, BRANCHING)
        PlaybookStep.objects.create(playbook=self.playbook, definition=d1, order=0)
        PlaybookStep.objects.create(playbook=self.playbook, definition=d2, order=1)
        self._dispatch()
        tasks = list(Task.objects.filter(host=self.host).order_by("step_order"))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].state, Task.State.PENDING)
        self.assertEqual(tasks[1].state, Task.State.BLOCKED)
        self.assertEqual(tasks[0].step_order, 0)
        self.assertEqual(tasks[1].step_order, 1)
        self.assertEqual(tasks[0].step_label, "playbook: PB → Plain")
        self.assertEqual(tasks[1].step_label, "playbook: PB → Branching")
        self.assertEqual(tasks[0].on_not_applicable, "stop")
        run = tasks[0].run
        self.assertIsNotNone(run)
        self.assertEqual(run.step_count, 2)
        self.assertEqual(run.playbook_id, self.playbook.id)

    def test_each_task_keeps_its_own_logic(self):
        d_branch = _definition(self.user, BRANCHING)
        d_rel = _definition(self.user, RELEVANT)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=d_branch, order=0
        )
        PlaybookStep.objects.create(playbook=self.playbook, definition=d_rel, order=1)
        self._dispatch()
        tasks = list(Task.objects.filter(host=self.host).order_by("step_order"))

        # The branching task's params carry its flow tree and its original
        # step ids — steps.svc still names a real step.
        branch_steps = tasks[0].params["steps"]
        self.assertEqual([s["id"] for s in branch_steps], ["svc", "a", "b"])
        self.assertIn("flow", tasks[0].params)
        # The flow tree: one if/then/else branch over the task's own steps.
        flow = tasks[0].params["flow"]
        self.assertEqual(flow[0], {"step": "svc"})
        root = flow[1]
        self.assertEqual(root["if"], "steps.svc.result.count > 0")
        self.assertEqual([n["step"] for n in root["then"]], ["a"])
        self.assertEqual([n["step"] for n in root["else"]], ["b"])

        # The relevant: task's params carry its resolved relevant: tree.
        self.assertIn("relevant", tasks[1].params)
        self.assertEqual(tasks[1].params["steps"][0]["id"], "a")

    def test_params_override_still_applies(self):
        d = _definition(self.user, OVERRIDABLE)
        PlaybookStep.objects.create(
            playbook=self.playbook,
            definition=d,
            order=0,
            params_override={"0": {"service_name": "ssh"}},
        )
        self._dispatch()
        task = Task.objects.get(host=self.host)
        self.assertEqual(task.params["steps"][0]["params"]["service_name"], "ssh")

    def _drive(self, states):
        """Post results for the host's tasks, in step order — each one only
        once the chain itself has made it pending, as a check-in would."""
        for task, state in zip(
            Task.objects.filter(host=self.host).order_by("step_order"), states
        ):
            task.refresh_from_db()
            self.assertEqual(task.state, Task.State.PENDING,
                             f"step {task.step_order} was never unblocked")
            task.state = Task.State.DISPATCHED
            task.save(update_fields=["state"])
            resp = self.client.post(
                "/api/v1/tasks/result/",
                {"task_id": str(task.id), "state": state, "output": "ok",
                 "result_output": "ok"},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
            )
            self.assertEqual(resp.status_code, 200, resp.content)
            task.refresh_from_db()

    def test_not_applicable_skip_continues(self):
        d1 = _definition(self.user, RELEVANT)
        d2 = _definition(self.user, PLAIN)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=d1, order=0, on_not_applicable="skip"
        )
        PlaybookStep.objects.create(playbook=self.playbook, definition=d2, order=1)
        self._dispatch()
        self._drive(["not_applicable", "completed"])
        self.assertEqual(
            self._states(), [Task.State.NOT_APPLICABLE, Task.State.COMPLETED]
        )
        run = Task.objects.filter(host=self.host).order_by("step_order").first().run
        run.refresh_from_db()
        self.assertEqual(run.state, TaskRun.State.COMPLETED)

    def test_not_applicable_stop_ends_the_host(self):
        d1 = _definition(self.user, RELEVANT)
        d2 = _definition(self.user, PLAIN)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=d1, order=0, on_not_applicable="stop"
        )
        PlaybookStep.objects.create(playbook=self.playbook, definition=d2, order=1)
        self._dispatch()
        self._drive(["not_applicable"])
        self.assertEqual(
            self._states(), [Task.State.NOT_APPLICABLE, Task.State.NOT_APPLICABLE]
        )
        run = Task.objects.filter(host=self.host).order_by("step_order").first().run
        run.refresh_from_db()
        self.assertEqual(run.state, TaskRun.State.NOT_APPLICABLE)

    def test_completion_tag_only_when_the_chain_finishes(self):
        def setup(on_not_applicable="stop", step_count=2):
            self.playbook.steps.all().delete()
            d1 = _definition(self.user, RELEVANT)
            d2 = _definition(self.user, PLAIN)
            PlaybookStep.objects.create(
                playbook=self.playbook,
                definition=d1,
                order=0,
                on_not_applicable=on_not_applicable,
            )
            if step_count > 1:
                PlaybookStep.objects.create(
                    playbook=self.playbook, definition=d2, order=1
                )
            self._dispatch()
            return list(Task.objects.filter(host=self.host).order_by("step_order"))

        # Not after step 1: the chain still has a blocked sibling.
        tasks = setup(on_not_applicable="skip")
        self.host.tags = []
        self.host.save()
        first = tasks[0]
        from apps.tasks.views import _maybe_apply_playbook_completion_tag

        first.state = Task.State.COMPLETED
        first.save()
        _maybe_apply_playbook_completion_tag(first)
        self.host.refresh_from_db()
        self.assertNotIn("hardened", self.host.tags)

        # Yes after the last step completes.
        last = tasks[-1]
        last.state = Task.State.COMPLETED
        last.save()
        _maybe_apply_playbook_completion_tag(last)
        self.host.refresh_from_db()
        self.assertIn("hardened", self.host.tags)

        # Yes after a stop-not-applicable: a chain that stopped as not
        # applicable is finished and must not be redispatched.
        self.host.tags = []
        self.host.save()
        Task.objects.all().delete()
        TaskRun.objects.all().delete()
        tasks = setup(on_not_applicable="stop")
        stopped = tasks[0]
        stopped.state = Task.State.NOT_APPLICABLE
        stopped.save()
        # As task_result does: advance the chain (stop closes the rest), then tag.
        from apps.tasks.views import _advance_run_sequence
        _advance_run_sequence(stopped)
        _maybe_apply_playbook_completion_tag(stopped)
        self.host.refresh_from_db()
        self.assertIn("hardened", self.host.tags)

        # No after a failure: the host is quarantined, not tagged.
        self.host.tags = []
        self.host.save()
        Task.objects.all().delete()
        TaskRun.objects.all().delete()
        tasks = setup()
        failed = tasks[0]
        failed.state = Task.State.FAILED
        failed.save()
        _maybe_apply_playbook_completion_tag(failed)
        self.host.refresh_from_db()
        self.assertNotIn("hardened", self.host.tags)


class PlaybookChainYamlTests(TestCase):
    def test_yaml_round_trips_on_not_applicable(self):
        user = get_user_model().objects.create_user("pb", password="pw")
        d1 = _definition(user, RELEVANT)
        d2 = _definition(user, PLAIN)
        pb = Playbook.objects.create(
            name="PB", created_by=user, completion_tag="hardened"
        )
        PlaybookStep.objects.create(
            playbook=pb, definition=d1, order=0, on_not_applicable="skip"
        )
        PlaybookStep.objects.create(playbook=pb, definition=d2, order=1)

        text = to_yaml(pb)
        parsed = parse(text)
        self.assertEqual(parsed["steps"][0]["on_not_applicable"], "skip")
        self.assertEqual(parsed["steps"][1]["on_not_applicable"], "stop")

    def test_yaml_refuses_an_invalid_on_not_applicable(self):
        from vigil.contentyaml import dump

        text = dump(
            {
                "uid": "019f2c0a-0000-7000-8000-000000000001",
                "name": "PB",
                "description": "",
                "definition_ids": [
                    {"task": "Plain", "order": 1, "on_not_applicable": "abort"}
                ],
            }
        )
        with self.assertRaises(ContentYamlError):
            parse(text)

    def test_api_returns_on_not_applicable_per_step(self):
        user = get_user_model().objects.create_user(
            "admin", password="pw", is_staff=True, is_superuser=True
        )
        d1 = _definition(user, RELEVANT)
        d2 = _definition(user, PLAIN)
        pb = Playbook.objects.create(name="PB", created_by=user)
        PlaybookStep.objects.create(
            playbook=pb, definition=d1, order=0, on_not_applicable="skip"
        )
        PlaybookStep.objects.create(playbook=pb, definition=d2, order=1)
        self.client.force_login(user)
        resp = self.client.get(f"/api/v1/playbooks/{pb.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["steps"][0]["on_not_applicable"], "skip")

        resp = self.client.patch(
            f"/api/v1/playbooks/{pb.id}/",
            {
                "definition_ids": [
                    {"definition_id": str(d1.id), "on_not_applicable": "skip"},
                    {"definition_id": str(d2.id)},
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        pb.refresh_from_db()
        self.assertEqual(pb.steps.filter(order=0).get().on_not_applicable, "skip")

        resp = self.client.patch(
            f"/api/v1/playbooks/{pb.id}/",
            {
                "definition_ids": [
                    {"definition_id": str(d1.id), "on_not_applicable": "abort"},
                    {"definition_id": str(d2.id)},
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)


class PlaybookChainEligibilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pb", password="pw")

    def test_eligible_accepts_m6_tasks(self):
        for src in (BRANCHING, RELEVANT):
            ok, why = eligible(_definition(self.user, src))
            self.assertTrue(ok, why)

    def test_flatten_paths_still_refuse(self):
        d = _definition(self.user, BRANCHING)
        pb = Playbook.objects.create(name="PB", created_by=self.user)
        PlaybookStep.objects.create(playbook=pb, definition=d, order=0)
        with self.assertRaises(PlaybookExpandError):
            build_agent_steps(pb)
        with self.assertRaises(PlaybookExpandError):
            expand_actions([{"type": "playbook", "params": {"name": "PB"}}])

    def test_automation_still_refuses_m6_tasks(self):
        from apps.automations.engine import _steps_for
        from apps.automations.models import Automation

        a = Automation(
            action_kind=Automation.ActionKind.TASK,
            task_definition=_definition(self.user, RELEVANT),
        )
        self.assertIsNone(_steps_for(a))
