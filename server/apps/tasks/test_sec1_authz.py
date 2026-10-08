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
from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole

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
