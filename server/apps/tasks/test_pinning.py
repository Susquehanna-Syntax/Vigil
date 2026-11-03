"""Pinned task versions (S1 decisions-06 item 16): an automation, a playbook
step and a rollout run the task version they were given until someone syncs,
unless they auto-sync; the editor is told when the task has changed."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.automations.models import Automation
from apps.playbooks.models import Playbook, PlaybookStep
from apps.tasks import pinning
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate


def _yaml(service):
    return (f"name: Check {service}\nactions:\n  - id: c\n    type: check_service\n"
            f"    params:\n      service_name: {service}\n")


class PinnedVersionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("wes", password="x")
        self.task = TaskDefinition.objects.create(owner=self.owner, name="Check", yaml_source=_yaml("cron"),
                                                  parsed_spec=parse_and_validate(_yaml("cron")))

    def edit_task(self, service):
        self.task.yaml_source = _yaml(service)
        self.task.parsed_spec = parse_and_validate(_yaml(service))
        self.task.save()

    def built(self, automation):
        from apps.automations.engine import _build_for
        kind, built, _risk = _build_for(Automation.objects.get(pk=automation.pk), [])
        return json.dumps(built, default=str)

    def test_an_automation_runs_its_version_until_synced(self):
        auto = Automation.objects.create(name="a", trigger="schedule", target="all", task_definition=self.task,
                                         task_snapshot=pinning.snapshot(self.task))
        self.edit_task("nginx")
        self.assertIn("cron", self.built(auto))
        self.assertNotIn("nginx", self.built(auto))
        self.assertTrue(Automation.objects.get(pk=auto.pk).task_stale)

        admin = get_user_model().objects.create_user("ida", password="x")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_login(admin)
        resp = self.client.post(f"/api/v1/automations/{auto.pk}/sync-task/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["task_stale"])
        self.assertIn("nginx", self.built(auto))

    def test_auto_sync_follows_edits(self):
        auto = Automation.objects.create(name="a", trigger="schedule", target="all", task_definition=self.task,
                                         task_snapshot=pinning.snapshot(self.task), task_auto_sync=True)
        self.edit_task("nginx")
        self.assertIn("nginx", self.built(auto))
        self.assertFalse(Automation.objects.get(pk=auto.pk).task_stale)

    def test_a_playbook_step_runs_its_version(self):
        from apps.tasks.dispatch import build_playbook_steps
        pb = Playbook.objects.create(name="pb")
        PlaybookStep.objects.create(playbook=pb, definition=self.task, order=0,
                                    definition_snapshot=pinning.snapshot(self.task))
        self.edit_task("nginx")
        built = json.dumps(build_playbook_steps(Playbook.objects.get(pk=pb.pk), user=None), default=str)
        self.assertIn("cron", built)
        self.assertNotIn("nginx", built)
        Playbook.objects.filter(pk=pb.pk).update(tasks_auto_sync=True)
        built = json.dumps(build_playbook_steps(Playbook.objects.get(pk=pb.pk), user=None), default=str)
        self.assertIn("nginx", built)

    def test_a_rollout_keeps_its_starting_version(self):
        from apps.tasks.models import PatchWave
        from apps.tasks.rollout import rollout_spec, start_rollout
        PatchWave.objects.create(name="all", order=1, enabled=True)
        rollout = start_rollout(self.task)
        self.edit_task("nginx")
        self.assertIn("cron", json.dumps(rollout_spec(rollout)))

    def test_a_pinned_copy_cannot_be_saved_over_the_task(self):
        old = pinning.effective(self.task, pinning.snapshot(self.task), False)
        self.edit_task("nginx")
        with self.assertRaises(RuntimeError):
            old.save()
        self.task.refresh_from_db()
        self.assertIn("nginx", self.task.yaml_source)

    def test_upgrade_leaves_existing_references_following_edits(self):
        import importlib

        from django.apps import apps
        auto = Automation.objects.create(name="a", trigger="schedule", target="all", task_definition=self.task)
        pb = Playbook.objects.create(name="pb")
        step = PlaybookStep.objects.create(playbook=pb, definition=self.task, order=0)
        importlib.import_module("apps.automations.migrations.0017_pinned_task_versions").existing_follow_edits(apps, None)
        importlib.import_module("apps.playbooks.migrations.0018_pinned_task_versions").existing_follow_edits(apps, None)
        auto.refresh_from_db(); pb.refresh_from_db(); step.refresh_from_db()
        self.assertTrue(auto.task_auto_sync and pb.tasks_auto_sync)
        self.assertEqual(auto.task_snapshot["hash"], pinning.version_hash(self.task))
        self.assertEqual(step.definition_snapshot["hash"], pinning.version_hash(self.task))

    def test_new_references_are_pinned_by_the_api(self):
        admin = get_user_model().objects.create_user("ida", password="x")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_login(admin)
        resp = self.client.post("/api/v1/automations/", data=json.dumps({
            "name": "n", "trigger": "schedule", "cron": {"minute": "0", "hour": "4"}, "target": "all",
            "task_definition": str(self.task.pk)}), content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(resp.json()["task_auto_sync"])
        self.edit_task("nginx")
        self.assertTrue(Automation.objects.get(pk=resp.json()["id"]).task_stale)
