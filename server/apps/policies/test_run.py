"""Compile and run: the generated task, its schedule, and who gets it."""
from datetime import timedelta
from unittest import mock

import yaml
from django.test import TestCase
from django.utils.timezone import now
from django_celery_beat.models import PeriodicTask

from apps.tasks.models import PatchRollout, PatchWave, Task, TaskRun

from .compile import policy_yaml, window_schedule
from .models import AppRule, UpdatePolicy
from .run import run_policy
from .test_drift import give, make_host
from .test_policy_api import admin_client

_TOTP = "apps.accounts.totp.require_totp_confirmation"


def make_policy(**fields):
    rules = fields.pop("rules", [("curl", "latest")])
    policy = UpdatePolicy.objects.create(name=fields.pop("name", "p"), **fields)
    for n, (app, state) in enumerate(rules):
        AppRule.objects.create(policy=policy, app=app, state=state, order=n)
    return policy


class CompileTests(TestCase):
    def setUp(self):
        self.client, self.user = admin_client()

    def test_saving_writes_the_definition_and_its_beat_task(self):
        resp = self.client.post("/api/v1/policies/", {
            "name": "Desktops", "cron_minute": "30", "cron_hour": "1",
            "app_rules": [{"app": "curl", "state": "latest"},
                          {"app": "openssl", "source": "dpkg", "state": "pinned",
                           "version": "3.0.13-1"}]}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        policy = UpdatePolicy.objects.get(name="Desktops")
        definition = policy.task_definition
        self.assertEqual(definition.name, "Policy: Desktops")
        self.assertEqual(definition.owner, self.user)
        actions = definition.parsed_spec["actions"]
        self.assertEqual([a["id"] for a in actions], ["app-1", "app-2"])
        self.assertEqual(actions[1]["params"], {"app": "openssl", "state": "pinned",
                                                "source": "dpkg", "version": "3.0.13-1"})
        beat = PeriodicTask.objects.get(name=f"policy:{policy.id}")
        self.assertEqual((beat.crontab.minute, beat.crontab.hour), ("30", "1"))
        self.assertEqual(resp.json()["task_definition"], str(definition.id))

        # An edit rewrites the same definition rather than adding another.
        self.client.put(f"/api/v1/policies/{policy.id}/",
                        {"app_rules": [{"app": "git", "state": "present"}]}, format="json")
        policy.refresh_from_db()
        self.assertEqual(policy.task_definition_id, definition.id)
        self.assertEqual(policy.task_definition.parsed_spec["actions"][0]["params"]["app"], "git")

    def test_disabling_or_deleting_removes_the_beat_task(self):
        pid = self.client.post("/api/v1/policies/", {"name": "d"}, format="json").json()["id"]
        self.assertTrue(PeriodicTask.objects.filter(name=f"policy:{pid}").exists())
        self.client.put(f"/api/v1/policies/{pid}/", {"enabled": False}, format="json")
        self.assertFalse(PeriodicTask.objects.filter(name=f"policy:{pid}").exists())
        self.client.put(f"/api/v1/policies/{pid}/", {"enabled": True,
                        "app_rules": [{"app": "curl", "state": "present"}]}, format="json")
        definition = UpdatePolicy.objects.get(pk=pid).task_definition
        self.client.delete(f"/api/v1/policies/{pid}/")
        self.assertFalse(PeriodicTask.objects.filter(name=f"policy:{pid}").exists())
        definition.refresh_from_db()
        self.assertIsNotNone(definition.archived_at)

    def test_a_policy_with_nothing_to_do_compiles_to_nothing(self):
        self.assertIsNone(policy_yaml(make_policy(rules=[])))

    def test_window_schedule(self):
        p = UpdatePolicy(cron_minute="30", cron_hour="22", cron_dow="0,6", window_hours=4)
        self.assertEqual(window_schedule(p), {"window": {
            "start_hour": 22, "start_minute": 30, "end_hour": 1, "end_minute": 30,
            "days": [5, 6]}})
        self.assertIsNone(window_schedule(UpdatePolicy(cron_hour="*/2", cron_minute="0")))
        self.assertIsNone(window_schedule(UpdatePolicy(cron_hour="2", cron_minute="0",
                                                       window_hours=24)))

    def test_only_a_rollout_policy_carries_the_window(self):
        direct = yaml.safe_load(policy_yaml(make_policy(name="a")))
        self.assertNotIn("schedule", direct)
        staged = yaml.safe_load(policy_yaml(make_policy(name="b", wave_group_tag="ring")))
        self.assertIn("schedule", staged)


class RunTests(TestCase):
    def setUp(self):
        self.client, self.user = admin_client()

    def _hosts(self):
        stale = make_host("a-stale")
        give(stale, "curl", "8.4", "8.5")
        fine = make_host("b-fine")
        give(fine, "curl", "8.5", "8.5")
        silent = make_host("c-silent", snapshot=False)
        return stale, fine, silent

    def test_direct_run_reaches_drifted_hosts_only_and_expires_at_window_end(self):
        stale, fine, silent = self._hosts()
        policy = make_policy(window_hours=3)
        before = now()
        result = run_policy(policy, user=self.user)
        self.assertEqual((result["mode"], result["dispatched"]), ("direct", 1))
        task = Task.objects.get()
        self.assertEqual(task.host, stale)
        self.assertEqual(task.run.source, TaskRun.Source.POLICY)
        self.assertEqual(task.params["steps"][0]["action"], "app_ensure")
        self.assertLessEqual(task.expires_at, now() + timedelta(hours=3))
        self.assertGreaterEqual(task.expires_at, before + timedelta(hours=3))

    def test_nothing_drifted_dispatches_nothing(self):
        host = make_host("fine")
        give(host, "curl", "8.5", "8.5")
        result = run_policy(make_policy(), user=self.user)
        self.assertEqual(result["dispatched"], 0)
        self.assertFalse(TaskRun.objects.exists())

    def test_rollout_is_restricted_to_drifted_hosts(self):
        stale, fine, _ = self._hosts()
        wave = PatchWave.objects.create(name="canary", order=1, tags=["ring1"],
                                        group_tags=["desktops"], validation_hours=0)
        for host in (stale, fine):
            host.tags = ["ring1"]
            host.save()
        self.assertTrue(wave.tag_rows.exists())
        result = run_policy(make_policy(wave_group_tag="desktops"), user=self.user)
        self.assertEqual(result["mode"], "rollout", result)
        rollout = PatchRollout.objects.get()
        self.assertEqual(rollout.host_ids, [str(stale.id)])
        self.assertEqual(list(Task.objects.values_list("host__hostname", flat=True)),
                         ["a-stale"])
        self.assertIn("schedule", rollout.definition.parsed_spec)

    def test_run_now_needs_totp(self):
        stale, _, _ = self._hosts()
        policy = make_policy()
        with mock.patch(_TOTP, return_value="code required"):
            resp = self.client.post(f"/api/v1/policies/{policy.id}/run/", {}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(Task.objects.exists())
        with mock.patch(_TOTP, return_value=None):
            resp = self.client.post(f"/api/v1/policies/{policy.id}/run/",
                                    {"totp": "123456"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["dispatched"], 1)

    def test_scheduled_run_skips_a_disabled_policy(self):
        from .tasks import run_scheduled_policy
        self._hosts()
        policy = make_policy(enabled=False)
        self.assertEqual(run_scheduled_policy(str(policy.id)), "skipped")
        policy.enabled = True
        policy.save()
        self.assertEqual(run_scheduled_policy(str(policy.id)), "dispatched:1")
