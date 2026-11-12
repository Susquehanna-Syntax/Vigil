"""SEC-1: only someone allowed to run tasks on a host can make the server sign one for it.

The audit (QA-15 F4) found that a viewer could create a task definition and deploy it to
any host, and that a user scoped to one site could deploy to another. Every path that
creates a signed task now checks scope (404, like an unknown id) and tasks:run (403).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from apps.tasks.spec import parse_and_validate

from vigil.testsupport import require_business_module

# Exercises core with the Business apps; a source checkout has none.
require_business_module()
from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole  # noqa: E402

CHECK = ("name: Check cron\nactions:\n  - id: c\n    type: check_service\n"
         "    params:\n      service_name: cron\n")


def _user(name, role=None):
    user = get_user_model().objects.create_user(username=name, password="pw")
    if role is not None:
        UserProfile.objects.create(user=user, role=role)
    return user


def _host(name, site=None, tags=()):
    host = Host.objects.create(hostname=name, agent_token=f"tok-{name}",
                               status=Host.Status.ONLINE, mode="managed", tags=list(tags))
    if site is not None:
        HostSiteAssignment.objects.create(host=host, site=site)
    return host


class Sec1Base(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.west_host = _host("west-1", self.west, tags=["web"])
        self.lab_host = _host("lab-1", self.lab, tags=["web"])

    def _definition(self, owner):
        return TaskDefinition.objects.create(owner=owner, name="Check cron", yaml_source=CHECK,
                                             parsed_spec=parse_and_validate(CHECK))

    def _deploy(self, user, definition, **body):
        self.client.force_login(user)
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            return self.client.post(f"/api/v1/tasks/definitions/{definition.id}/deploy/",
                                    {"totp": "123456", **body}, content_type="application/json")


class ViewerCannotRunTests(Sec1Base):
    def test_viewer_cannot_create_a_definition(self):
        viewer = _user("viewer", Role.VIEWER)
        self.client.force_login(viewer)
        resp = self.client.post("/api/v1/tasks/definitions/", {"yaml_source": CHECK},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(TaskDefinition.objects.filter(owner=viewer).exists())

    def test_viewer_cannot_deploy_even_their_own_definition(self):
        viewer = _user("viewer", Role.VIEWER)
        resp = self._deploy(viewer, self._definition(viewer), host_ids=[str(self.west_host.id)])
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Task.objects.exists())

    def test_viewer_cannot_fork_community_content(self):
        admin = _user("adm", Role.ADMIN)
        community = self._definition(admin)
        community.visibility = TaskDefinition.Visibility.COMMUNITY
        community.save()
        viewer = _user("viewer", Role.VIEWER)
        self.client.force_login(viewer)
        resp = self.client.post(f"/api/v1/tasks/definitions/{community.id}/fork/")
        self.assertIn(resp.status_code, (403, 405))
        self.assertFalse(TaskDefinition.objects.filter(owner=viewer).exists())

    def test_viewer_cannot_fork_from_the_community_catalog(self):
        from unittest import mock
        self.client.force_login(_user("viewer2", Role.VIEWER))
        with mock.patch("apps.tasks.views.catalog._plan_fork", side_effect=AssertionError("reached the fork")):
            for kind in ("tasks", "playbooks", "automations"):
                resp = self.client.post(f"/api/v1/tasks/community/{kind}/x.yaml/fork/")
                self.assertEqual(resp.status_code, 403, (kind, resp.content[:200]))


class SiteScopeTests(Sec1Base):
    def setUp(self):
        super().setUp()
        self.dana = _user("dana")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)

    def test_scoped_admin_deploys_in_their_site(self):
        resp = self._deploy(self.dana, self._definition(self.dana), host_ids=[str(self.west_host.id)])
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Task.objects.filter(host=self.west_host).count(), 1)

    def test_scoped_admin_cannot_deploy_to_another_site(self):
        resp = self._deploy(self.dana, self._definition(self.dana),
                            host_ids=[str(self.west_host.id), str(self.lab_host.id)])
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Task.objects.exists(), "nothing may be created when any target is refused")

    def test_tag_targeting_only_reaches_hosts_in_scope(self):
        resp = self._deploy(self.dana, self._definition(self.dana), tags=["web"])
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(set(Task.objects.values_list("host__hostname", flat=True)), {"west-1"})


class OperatorCapabilityTests(Sec1Base):
    def test_operator_without_tasks_run_in_site_is_refused(self):
        from apps_business.sites.models import SiteCapability
        oper = _user("oper")
        row = UserSiteRole.objects.create(user=oper, site=self.west, role=Role.OPERATOR)
        SiteCapability.objects.create(user_site_role=row, app="tasks", verb="view")
        admin = _user("adm", Role.ADMIN)
        definition = self._definition(admin)
        definition.visibility = TaskDefinition.Visibility.COMMUNITY
        definition.save()
        resp = self._deploy(oper, definition, host_ids=[str(self.west_host.id)])
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Task.objects.exists())

    def test_global_admin_still_deploys_anywhere(self):
        admin = _user("adm", Role.ADMIN)
        resp = self._deploy(admin, self._definition(admin),
                            host_ids=[str(self.west_host.id), str(self.lab_host.id)])
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Task.objects.count(), 2)


class HostScopeOfTests(Sec1Base):
    """scope_of(host) used to be None for every host, so per-site operator
    capabilities never applied to the host endpoints that check them."""

    def test_scope_of_a_host_is_its_site(self):
        from vigil import scoping
        self.assertEqual(scoping.scope_of(self.west_host), self.west)

    def test_capped_operator_cannot_update_the_agent(self):
        from apps_business.sites.models import SiteCapability
        oper = _user("oper2")
        row = UserSiteRole.objects.create(user=oper, site=self.west, role=Role.OPERATOR)
        SiteCapability.objects.create(user_site_role=row, app="hosts", verb="view")
        self.client.force_login(oper)
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            resp = self.client.post(f"/api/v1/hosts/{self.west_host.id}/update-agent/",
                                    {"totp": "123456"}, content_type="application/json")
        self.assertEqual(resp.status_code, 403, resp.content)


class FleetAndStackTests(Sec1Base):
    def _post(self, user, url, body=None):
        self.client.force_login(user)
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            return self.client.post(url, {"totp": "123456", **(body or {})}, content_type="application/json")

    def test_rollout_needs_fleet_wide_permission_to_run(self):
        admin = _user("adm", Role.ADMIN)
        definition = self._definition(admin)
        definition.visibility = TaskDefinition.Visibility.COMMUNITY
        definition.save()
        resp = self._post(_user("viewer", Role.VIEWER), "/api/v1/rollouts/",
                          {"definition_id": str(definition.id)})
        self.assertEqual(resp.status_code, 403, resp.content)
        # A fleet-wide operator is allowed past the permission check.
        resp = self._post(_user("oper", Role.OPERATOR), "/api/v1/rollouts/",
                          {"definition_id": str(definition.id)})
        self.assertNotEqual(resp.status_code, 403, resp.content)
        dana = _user("dana")
        UserSiteRole.objects.create(user=dana, site=self.west, role=Role.ADMIN)
        resp = self._post(dana, "/api/v1/rollouts/", {"definition_id": str(definition.id)})
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_site_admin_cannot_deploy_a_stack_where_they_are_a_viewer(self):
        from apps.stacks.models import ManagedStack
        stack = ManagedStack.objects.create(host=self.lab_host, name="web", compose_yaml="services: {}\n")
        mixed = _user("mixed")
        UserSiteRole.objects.create(user=mixed, site=self.west, role=Role.ADMIN)
        UserSiteRole.objects.create(user=mixed, site=self.lab, role=Role.VIEWER)
        resp = self._post(mixed, f"/api/v1/stacks/{stack.id}/deploy/")
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(Task.objects.exists())

    def test_check_pending_is_admin_only(self):
        resp = self._post(_user("viewer", Role.VIEWER), "/api/v1/hosts/check-pending/",
                          {"token": "tok-west-1"})
        self.assertEqual(resp.status_code, 403)


class ReadScopeTests(Sec1Base):
    """Task output can carry anything a host printed, so history and run
    detail are scoped by site like the hosts they ran on."""

    def setUp(self):
        super().setUp()
        from apps.tasks.models import TaskRun
        admin = _user("adm", Role.ADMIN)
        self.lab_run = TaskRun.objects.create(name_snapshot="lab run", requested_by=admin)
        self.lab_task = Task.objects.create(host=self.lab_host, run=self.lab_run, action="check_service", nonce="n-lab",
                                            params={}, requested_by=admin, result_output="lab secret")
        self.west_run = TaskRun.objects.create(name_snapshot="west run", requested_by=admin)
        Task.objects.create(host=self.west_host, run=self.west_run, action="check_service", nonce="n-west",
                            params={}, requested_by=admin, result_output="west output")
        self.dana = _user("dana")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.client.force_login(self.dana)

    def test_history_shows_only_tasks_in_scope(self):
        body = self.client.get("/api/v1/tasks/history/").json()
        hosts = {row["host_hostname"] for row in body["results"]}
        self.assertEqual(hosts, {"west-1"})

    def test_task_detail_out_of_scope_is_not_found(self):
        self.assertEqual(self.client.get(f"/api/v1/tasks/{self.lab_task.id}/").status_code, 404)

    def test_run_detail_out_of_scope_is_not_found(self):
        self.assertEqual(self.client.get(f"/api/v1/tasks/runs/{self.lab_run.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/tasks/runs/{self.west_run.id}/").status_code, 200)

    def test_run_history_hides_other_sites(self):
        body = self.client.get("/api/v1/tasks/runs/").json()
        rows = body["results"] if isinstance(body, dict) else body
        names = {r["name_snapshot"] for r in rows}
        self.assertIn("west run", names)
        self.assertNotIn("lab run", names)


class SweepTests(Sec1Base):
    def _post(self, user, url, body=None):
        self.client.force_login(user)
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            return self.client.post(url, {"totp": "123456", **(body or {})}, content_type="application/json")

    def test_a_viewer_cannot_launch_a_vulnerability_scan(self):
        resp = self._post(_user("viewer", Role.VIEWER), f"/api/v1/vulns/scans/{self.west_host.id}/")
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_a_viewer_cannot_add_an_unmanaged_device(self):
        resp = self._post(_user("viewer", Role.VIEWER), "/api/v1/hosts/devices/",
                          {"name": "printer", "ip_address": "10.0.0.9"})
        self.assertEqual(resp.status_code, 403, resp.content)


class MixedRunTrimTests(Sec1Base):
    """decisions-06 item 5: a run that touched West and Lab is visible to a West
    admin, cut down to West. Lab's task, its output, its host and its failure
    never show; the run's state is what it was on West."""

    def setUp(self):
        super().setUp()
        from apps.tasks.models import TaskRun
        admin = _user("adm", Role.ADMIN)
        self.run = TaskRun.objects.create(name_snapshot="mixed run", requested_by=admin,
                                          host_count=2, step_count=1, state=TaskRun.State.PARTIAL)
        Task.objects.create(host=self.west_host, run=self.run, action="check_service", nonce="m-west",
                            params={}, requested_by=admin, state=Task.State.COMPLETED,
                            result_output="west output")
        Task.objects.create(host=self.lab_host, run=self.run, action="check_service", nonce="m-lab",
                            params={}, requested_by=admin, state=Task.State.FAILED,
                            result_output="lab secret")
        self.dana = _user("dana")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.client.force_login(self.dana)

    def test_detail_shows_only_west(self):
        resp = self.client.get(f"/api/v1/tasks/runs/{self.run.id}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["host_count"], 1)
        self.assertEqual(body["state"], "completed")
        self.assertEqual([t["result_output"] for t in body["tasks"]], ["west output"])
        for marker in (b"lab secret", b"lab-1", str(self.lab_host.id).encode()):
            self.assertNotIn(marker, resp.content)

    def test_finished_at_is_the_callers_part(self):
        from datetime import timedelta

        from django.utils import timezone
        west_done = timezone.now() - timedelta(hours=1)
        Task.objects.filter(nonce="m-west").update(completed_at=west_done)
        Task.objects.filter(nonce="m-lab").update(state=Task.State.EXECUTING)
        type(self.run).objects.filter(pk=self.run.pk).update(state="running", finished_at=None)
        body = self.client.get(f"/api/v1/tasks/runs/{self.run.id}/").json()
        self.assertEqual(body["state"], "completed")
        self.assertIsNotNone(body["finished_at"])
        self.assertTrue(body["finished_at"].startswith(west_done.strftime("%Y-%m-%dT%H:%M")))

    def test_history_row_is_trimmed(self):
        rows = self.client.get("/api/v1/tasks/runs/").json()["results"]
        row = next(r for r in rows if r["name_snapshot"] == "mixed run")
        self.assertEqual((row["host_count"], row["state"]), (1, "completed"))

    def test_fleet_admin_sees_the_whole_run(self):
        self.client.force_login(_user("root2", Role.ADMIN))
        body = self.client.get(f"/api/v1/tasks/runs/{self.run.id}/").json()
        self.assertEqual((body["host_count"], body["state"], len(body["tasks"])), (2, "partial", 2))

    def test_a_trimmed_run_cannot_be_saved(self):
        from apps.tasks.authz import trim_run
        from apps.tasks.models import TaskRun
        run = trim_run(TaskRun.objects.get(pk=self.run.pk), self.dana)
        with self.assertRaises(RuntimeError):
            run.save()
        self.run.refresh_from_db()
        self.assertEqual((self.run.host_count, self.run.state), (2, "partial"))

    def test_jackil_failed_runs_tile_counts_only_west_failures(self):
        from apps.jackil.integration_widgets import _failed_runs
        from apps.tasks.models import TaskRun
        TaskRun.objects.filter(pk=self.run.pk).update(state=TaskRun.State.FAILED)
        Task.objects.filter(nonce="m-west").update(state=Task.State.COMPLETED)
        self.assertEqual(_failed_runs(self.dana), 0)
        Task.objects.filter(nonce="m-west").update(state=Task.State.FAILED)
        self.assertEqual(_failed_runs(self.dana), 1)


class RunOutcomeTests(TestCase):
    """run_outcome is the rule _finalize_run_if_done applied inline before."""

    def test_rules(self):
        from apps.tasks.summary import run_outcome
        cases = [
            ([("pending", "abort")], "running"),
            ([("completed", "abort"), ("skipped", "abort")], "completed"),
            ([("not_applicable", "abort")], "not_applicable"),
            ([("completed", "abort"), ("failed", "abort")], "partial"),
            ([("failed", "abort"), ("expired", "abort")], "failed"),
            ([("failed", "continue"), ("completed", "abort")], "completed"),
            ([("not_applicable", "abort"), ("failed", "abort")], "failed"),
        ]
        for rows, want in cases:
            self.assertEqual(run_outcome(rows), want, rows)


class RolloutScopeTests(Sec1Base):
    """decisions-06 items 1, 4, 5 for rollouts: a site's rollout is its site's;
    a fleet-wide one is seen trimmed and acted on only by a fleet-wide runner,
    or by someone who may run tasks on every host it names."""

    def setUp(self):
        super().setUp()
        from apps.tasks.models import PatchRollout
        self.definition = self._definition(None)
        self.dana = _user("dana")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)

        def rollout(**kw):
            return PatchRollout.objects.create(definition=self.definition,
                                               state=PatchRollout.State.RUNNING, **kw)
        self.west_rollout = rollout(site_id=self.west.id, host_ids=[str(self.west_host.id)])
        self.lab_rollout = rollout(site_id=self.lab.id, host_ids=[str(self.lab_host.id)])
        self.fleet_rollout = rollout()
        self.west_named = rollout(host_ids=[str(self.west_host.id)])
        self.lab_named = rollout(host_ids=[str(self.lab_host.id)])
        self.mixed = rollout(host_ids=[str(self.west_host.id), str(self.lab_host.id)])

    def _halt(self, rollout):
        self.client.force_login(self.dana)
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            return self.client.post(f"/api/v1/rollouts/{rollout.id}/halt/", {"totp": "123456"},
                                    content_type="application/json")

    def test_list_shows_only_what_dana_may_see(self):
        self.client.force_login(self.dana)
        ids = {r["id"] for r in self.client.get("/api/v1/rollouts/").json()}
        self.assertEqual(ids, {str(r.id) for r in (self.west_rollout, self.fleet_rollout,
                                                    self.west_named, self.mixed)})

    def test_write_rules(self):
        self.assertEqual(self._halt(self.lab_rollout).status_code, 404)
        self.assertEqual(self._halt(self.lab_named).status_code, 404)
        self.assertEqual(self._halt(self.fleet_rollout).status_code, 403)
        self.assertEqual(self._halt(self.mixed).status_code, 403)
        self.assertEqual(self._halt(self.west_rollout).status_code, 200)
        self.assertEqual(self._halt(self.west_named).status_code, 200)

    def test_batch_halt_needs_every_member(self):
        import uuid

        from apps.tasks.models import PatchRollout
        def halt_batch(*members):
            batch = uuid.uuid4()
            PatchRollout.objects.filter(pk__in=[m.pk for m in members]).update(batch=batch)
            self.client.force_login(self.dana)
            with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
                return self.client.post(f"/api/v1/rollouts/batch/{batch}/halt/", {"totp": "123456"},
                                        content_type="application/json").status_code

        self.assertEqual(halt_batch(self.lab_rollout), 404)
        # Seen but not dana's to act on (fleet-wide), and not seen at all (Lab).
        self.assertEqual(halt_batch(self.west_rollout, self.fleet_rollout), 403)
        self.assertEqual(halt_batch(self.west_named, self.lab_named), 403)
        for rollout in (self.west_rollout, self.fleet_rollout, self.west_named, self.lab_named):
            rollout.refresh_from_db()
            self.assertEqual(rollout.state, "running")

    def test_a_mixed_rollout_is_trimmed_to_west(self):
        from apps.tasks.models import PatchWave, TaskRun
        wave = PatchWave.objects.create(name="all", order=1)
        admin = _user("adm", Role.ADMIN)
        run = TaskRun.objects.create(name_snapshot="wave 1", requested_by=admin, rollout=self.mixed,
                                     wave=wave, host_count=2, state=TaskRun.State.PARTIAL)
        Task.objects.create(host=self.west_host, run=run, action="check_service", nonce="r-west",
                            params={}, requested_by=admin, state=Task.State.COMPLETED,
                            result_output="west output")
        Task.objects.create(host=self.lab_host, run=run, action="check_service", nonce="r-lab",
                            params={}, requested_by=admin, state=Task.State.FAILED,
                            result_output="lab secret")
        self.client.force_login(self.dana)
        detail = self.client.get(f"/api/v1/rollouts/{self.mixed.id}/").json()
        progress = next(w for w in detail["waves"] if w["id"] == str(wave.id))
        self.assertEqual(progress["hosts"], 1)
        self.assertEqual(progress["tasks_failed"], 0)
        resp = self.client.get(f"/api/v1/rollouts/{self.mixed.id}/waves/{wave.id}/hosts/")
        self.assertEqual([h["hostname"] for h in resp.json()["hosts"]], ["west-1"])
        self.assertNotIn(b"lab secret", resp.content)
