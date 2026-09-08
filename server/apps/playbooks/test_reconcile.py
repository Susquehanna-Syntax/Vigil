"""The bug this release exists to fix.

A playbook that targeted a tag only ever dispatched from the host_approved
hook, so a host enrolled before the playbook existed, or tagged afterwards,
never ran it and nothing said why.
"""

import secrets

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.playbooks.models import (Playbook, PlaybookStep, hosts_awaiting,
                                   hosts_failing, reconcile)
from apps.tasks.models import Task, TaskDefinition


def _host(name, tags):
    return Host.objects.create(
        hostname=name, ip_address=f"10.40.0.{abs(hash(name)) % 250 + 1}",
        agent_token=f"tok-{name}", tags=tags, mode="managed",
        status=Host.Status.ONLINE)


class ReconcileTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        definition = TaskDefinition.objects.create(
            name="Harden", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        self.playbook = Playbook.objects.create(
            name="Linux hardening", created_by=self.admin,
            target_tags=["prod"], completion_tag="hardened", auto_enroll=True)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=definition, order=0)

    def test_a_host_enrolled_before_the_playbook_existed_still_gets_it(self):
        host = _host("old-box", ["prod"])
        self.assertEqual(hosts_awaiting(self.playbook), [host])
        self.assertEqual(reconcile(), 1)
        self.assertEqual(Task.objects.filter(host=host).count(), 1)

    def test_a_host_without_the_target_tag_is_left_alone(self):
        _host("dev-box", ["dev"])
        self.assertEqual(reconcile(), 0)
        self.assertFalse(Task.objects.exists())

    def test_a_host_carrying_the_completion_tag_is_skipped(self):
        _host("done-box", ["prod", "hardened"])
        self.assertEqual(hosts_awaiting(self.playbook), [])
        self.assertEqual(reconcile(), 0)

    def test_the_completion_tag_is_matched_case_insensitively(self):
        _host("done-box", ["prod", "Hardened"])
        self.assertEqual(reconcile(), 0)

    def test_reconcile_does_not_dispatch_twice_once_the_tag_lands(self):
        host = _host("box", ["prod"])
        self.assertEqual(reconcile(), 1)
        host.tags = host.tags + ["hardened"]
        host.save()
        self.assertEqual(reconcile(), 0)
        self.assertEqual(Task.objects.filter(host=host).count(), 1)

    def test_a_playbook_that_does_not_auto_enrol_is_never_reconciled(self):
        _host("box", ["prod"])
        self.playbook.auto_enroll = False
        self.playbook.save(update_fields=["auto_enroll"])
        self.assertEqual(reconcile(), 0)

    def test_a_playbook_with_no_completion_tag_is_never_reconciled(self):
        _host("box", ["prod"])
        self.playbook.completion_tag = ""
        self.playbook.save(update_fields=["completion_tag"])
        self.assertEqual(reconcile(), 0)

    def test_monitor_mode_hosts_are_never_targeted(self):
        host = _host("watched", ["prod"])
        host.mode = "monitor"
        host.save()
        self.assertEqual(hosts_awaiting(self.playbook), [])

    def test_pending_hosts_are_never_targeted(self):
        host = _host("waiting", ["prod"])
        host.status = Host.Status.PENDING
        host.save()
        self.assertEqual(hosts_awaiting(self.playbook), [])

    def test_the_limit_caps_dispatches_per_pass(self):
        for i in range(4):
            _host(f"box{i}", ["prod"])
        self.assertEqual(reconcile(limit=2), 2)
        self.assertEqual(Task.objects.count(), 2)


class AutoEnrollGateTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.playbook = Playbook.objects.create(
            name="P", created_by=self.admin, target_tags=["prod"])

    def test_auto_enroll_is_off_on_a_newly_created_playbook(self):
        definition = TaskDefinition.objects.create(
            name="Step", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        resp = self.client.post(
            "/api/v1/playbooks/",
            {"name": "Fresh", "definition_ids": [str(definition.id)]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(resp.json()["auto_enroll"])

    def test_turning_auto_enroll_on_without_a_completion_tag_is_refused(self):
        resp = self.client.patch(f"/api/v1/playbooks/{self.playbook.id}/",
                                 {"auto_enroll": True},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("completion_tag", resp.json()["detail"])

    def test_turning_auto_enroll_on_with_a_completion_tag_is_allowed(self):
        resp = self.client.patch(
            f"/api/v1/playbooks/{self.playbook.id}/",
            {"auto_enroll": True, "completion_tag": "done"},
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["auto_enroll"])
        self.assertEqual(resp.json()["completion_tag"], "done")


class CompletionTaggingTests(TestCase):
    """The other half of the loop: a successful run has to mark the host, or
    reconcile dispatches the same playbook again on the next pass."""

    def setUp(self):
        from apps.tasks.models import TaskRun

        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.host = _host("box", ["prod"])
        self.playbook = Playbook.objects.create(
            name="Linux hardening", created_by=self.admin,
            target_tags=["prod"], completion_tag="hardened", auto_enroll=True)
        self.run = TaskRun.objects.create(
            source=TaskRun.Source.PLAYBOOK, playbook=self.playbook,
            name_snapshot=self.playbook.name, host_count=1, step_count=1)
        self.task = Task.objects.create(
            host=self.host, run=self.run, step_label="playbook",
            action="_script", params={"steps": []},
            state=Task.State.COMPLETED, nonce="n" * 64)

    def _apply(self):
        from apps.tasks.views import _maybe_apply_playbook_completion_tag

        _maybe_apply_playbook_completion_tag(self.task)
        self.host.refresh_from_db()

    def test_a_successful_playbook_run_tags_the_host(self):
        self._apply()
        self.assertIn("hardened", self.host.tags)

    def test_the_tagged_host_stops_matching(self):
        self._apply()
        self.assertFalse(self.playbook.matches(self.host))
        self.assertEqual(hosts_awaiting(self.playbook), [])

    def test_the_tag_is_not_duplicated_on_a_second_run(self):
        self._apply()
        self._apply()
        self.assertEqual(self.host.tags.count("hardened"), 1)

    def test_a_run_with_no_playbook_tags_nothing(self):
        self.run.playbook = None
        self.run.save(update_fields=["playbook"])
        before = list(self.host.tags)
        self._apply()
        self.assertEqual(self.host.tags, before)

    def test_a_playbook_with_no_completion_tag_tags_nothing(self):
        self.playbook.completion_tag = ""
        self.playbook.save(update_fields=["completion_tag"])
        before = list(self.host.tags)
        self._apply()
        self.assertEqual(self.host.tags, before)

    def test_the_reserved_agent_namespace_is_refused(self):
        self.playbook.completion_tag = "agent:hardened"
        self.playbook.save(update_fields=["completion_tag"])
        before = list(self.host.tags)
        self._apply()
        self.assertEqual(self.host.tags, before)

    def test_the_tag_reaches_the_rows_the_matcher_reads(self):
        self._apply()
        self.assertIn("hardened",
                      [t.name for t in self.host.tag_rows.all()])


class FailureQuarantineTests(TestCase):
    """A playbook that fails on a host must stop auto-enrolling to it.

    The completion tag only lands on success, so before 2026.11.3 a host that
    could not run the playbook was picked up again by every reconcile pass —
    every five minutes, for good. Nothing converged, and the one failure worth
    reading was buried under a thousand identical ones.
    """

    def setUp(self):
        from apps.tasks.models import TaskRun

        self.TaskRun = TaskRun
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        definition = TaskDefinition.objects.create(
            name="Harden", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        self.playbook = Playbook.objects.create(
            name="Linux hardening", created_by=self.admin,
            target_tags=["prod"], completion_tag="hardened", auto_enroll=True)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=definition, order=0)
        self.host = _host("box", ["prod"])

    def _run(self, state, playbook=None):
        """One dispatch of *playbook* to the host, ending in *state*."""
        run = self.TaskRun.objects.create(
            source=self.TaskRun.Source.PLAYBOOK,
            playbook=playbook or self.playbook,
            name_snapshot="Linux hardening", host_count=1, step_count=1)
        return Task.objects.create(
            host=self.host, run=run, step_label="playbook",
            action="_script", params={"steps": []},
            state=state, nonce=secrets.token_hex(32))

    def test_a_failed_run_stops_the_next_dispatch(self):
        self._run(Task.State.FAILED)
        self.assertEqual(hosts_awaiting(self.playbook), [])
        self.assertEqual(reconcile(), 0)

    def test_a_failed_host_is_reported_as_failing(self):
        self._run(Task.State.FAILED)
        self.assertEqual(hosts_failing(self.playbook), [self.host])

    def test_rejected_and_expired_count_as_failures(self):
        for state in (Task.State.REJECTED, Task.State.EXPIRED):
            with self.subTest(state=state):
                Task.objects.all().delete()
                self.TaskRun.objects.all().delete()
                self._run(state)
                self.assertEqual(hosts_awaiting(self.playbook), [])

    def test_a_run_still_in_flight_stops_the_next_dispatch(self):
        """The pile-up half of the same bug: a host that is merely switched
        off collected a fresh run every five minutes and ran all of them at
        once when it came back."""
        self._run(Task.State.PENDING)
        self.assertEqual(hosts_awaiting(self.playbook), [])
        self.assertEqual(reconcile(), 0)

    def test_a_retry_clears_the_quarantine(self):
        self._run(Task.State.FAILED)
        self.assertEqual(hosts_failing(self.playbook), [self.host])
        # A retry is simply a newer run. Only the newest counts.
        self._run(Task.State.PENDING)
        self.assertEqual(hosts_failing(self.playbook), [])

    def test_a_newer_failure_after_a_success_quarantines_again(self):
        self._run(Task.State.COMPLETED)
        self._run(Task.State.FAILED)
        self.assertEqual(hosts_failing(self.playbook), [self.host])
        self.assertEqual(hosts_awaiting(self.playbook), [])

    def test_another_playbooks_failure_does_not_quarantine_this_one(self):
        other = Playbook.objects.create(
            name="Other", created_by=self.admin, target_tags=["prod"],
            completion_tag="othered", auto_enroll=True)
        self._run(Task.State.FAILED, playbook=other)
        self.assertEqual(hosts_awaiting(self.playbook), [self.host])

    def test_a_failure_on_one_host_does_not_hold_back_another(self):
        healthy = _host("box2", ["prod"])
        self._run(Task.State.FAILED)
        self.assertEqual(hosts_awaiting(self.playbook), [healthy])

    def test_a_skipped_step_is_not_a_failure(self):
        self._run(Task.State.SKIPPED)
        self.assertEqual(hosts_failing(self.playbook), [])


class FailureApiTests(TestCase):
    """The quarantine has to be visible and clearable from the UI, or it is
    just a playbook that silently stopped working."""

    def setUp(self):
        from apps.tasks.models import TaskRun

        self.TaskRun = TaskRun
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        definition = TaskDefinition.objects.create(
            name="Harden", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        self.playbook = Playbook.objects.create(
            name="Linux hardening", created_by=self.admin,
            target_tags=["prod"], completion_tag="hardened", auto_enroll=True)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=definition, order=0)
        self.host = _host("box", ["prod"])
        run = TaskRun.objects.create(
            source=TaskRun.Source.PLAYBOOK, playbook=self.playbook,
            name_snapshot="Linux hardening", host_count=1, step_count=1)
        Task.objects.create(
            host=self.host, run=run, step_label="playbook: Linux hardening",
            action="_script", params={"steps": []}, state=Task.State.FAILED,
            result_output="sshd refused to restart", nonce=secrets.token_hex(32))

    def test_the_list_row_counts_the_held_back_hosts(self):
        resp = self.client.get("/api/v1/playbooks/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()[0]["failing_hosts"], 1)

    def test_the_failures_endpoint_says_which_host_and_why(self):
        resp = self.client.get(f"/api/v1/playbooks/{self.playbook.id}/failures/")
        self.assertEqual(resp.status_code, 200, resp.content)
        row = resp.json()[0]
        self.assertEqual(row["hostname"], "box")
        self.assertEqual(row["state"], "failed")
        self.assertIn("sshd refused", row["output"])

    def test_retrying_dispatches_again_and_clears_the_quarantine(self):
        resp = self.client.post(f"/api/v1/playbooks/{self.playbook.id}/retry/",
                                {}, content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["dispatched"], 1)
        self.assertEqual(hosts_failing(self.playbook), [])
        self.assertEqual(
            Task.objects.filter(host=self.host,
                                state=Task.State.PENDING).count(), 1)

    def test_retrying_a_host_that_is_not_held_back_is_a_404(self):
        other = _host("box2", ["prod"])
        resp = self.client.post(f"/api/v1/playbooks/{self.playbook.id}/retry/",
                                {"host": str(other.id)},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 404)

    def test_an_archived_playbook_does_not_retry(self):
        from django.utils.timezone import now

        self.playbook.archived_at = now()
        self.playbook.save(update_fields=["archived_at"])
        resp = self.client.post(f"/api/v1/playbooks/{self.playbook.id}/retry/",
                                {}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_the_endpoints_are_admin_only(self):
        self.client.logout()
        operator = get_user_model().objects.create_user(
            username="op", password="pw")
        self.client.force_login(operator)
        self.assertEqual(self.client.get(
            f"/api/v1/playbooks/{self.playbook.id}/failures/").status_code, 403)
        self.assertEqual(self.client.post(
            f"/api/v1/playbooks/{self.playbook.id}/retry/", {},
            content_type="application/json").status_code, 403)
