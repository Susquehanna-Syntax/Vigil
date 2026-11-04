"""Company-wide module grants (S1 04f, decisions-06 item 6).

An instance admin gives one user one verb of one module across every site.
The grant widens that module and nothing else: a host's own pages stay
bounded by the user's sites, global items stay read-only, and a site-scoped
API key never carries a grant.
"""
import json
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import can, visible_site_ids
from apps.hosts.models import Host
from apps.vulns.models import VulnFinding
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.audits.models import AuditEvent  # noqa: E402
from apps_business.sites.models import (HostSiteAssignment, ModuleGrant, Site,  # noqa: E402
                                        UserSiteRole)


class ModuleGrantTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.glob = Site.objects.get(is_global=True)
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = User.objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.gwen = User.objects.create_user("gwen", password="x")
        UserSiteRole.objects.create(user=self.gwen, site=self.west, role=Role.ADMIN)
        UserSiteRole.objects.create(user=self.gwen, site=self.glob, role=Role.VIEWER)
        self.ida = User.objects.create_user("ida", password="x")
        UserProfile.objects.create(user=self.ida, role=Role.ADMIN)
        self.hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab)):
            self.hosts[name] = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                                   status=Host.Status.ONLINE, mode="managed")
            HostSiteAssignment.objects.create(host=self.hosts[name], site=site)
            VulnFinding.objects.create(host=self.hosts[name], title=f"{name} finding", scanner="trivy",
                                       plugin_id_or_oid=f"{name}:CVE-1", cve_id="CVE-2024-1",
                                       severity="high", package_name="openssl")

    def grant(self, app, verb, user=None):
        return ModuleGrant.objects.create(user=user or self.dana, app=app, verb=verb, granted_by=self.ida)

    def get(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)

    def test_inventory_view_widens_the_inventory_list_only(self):
        # The spec's acceptance case.
        self.assertNotIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/inventory/").content)
        self.grant("inventory", "view")
        self.assertIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/inventory/").content)
        lab = self.hosts["lab-1"].pk
        self.assertEqual(self.get(self.dana, f"/api/v1/hosts/{lab}/").status_code, 404)
        self.assertEqual(self.get(self.dana, f"/api/v1/hosts/{lab}/inventory/").status_code, 404)
        self.assertNotIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/").content)

    def test_vulns_view_widens_findings_not_hosts(self):
        self.grant("vulns", "view")
        self.assertIn(b"lab-1 finding", self.get(self.dana, "/api/v1/vulns/findings/").content)
        self.assertNotIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/").content)

    def test_tasks_run_lets_them_run_on_other_sites_hosts(self):
        from apps.tasks.authz import run_denied
        lab = self.hosts["lab-1"]
        with scoping.as_user(self.dana):
            self.assertEqual(run_denied(self.dana, [lab]).status_code, 404)
        self.grant("tasks", "run")
        with scoping.as_user(self.dana):
            self.assertIsNone(run_denied(self.dana, [lab]))

    def test_a_grant_never_reaches_global_items(self):
        self.grant("playbooks", "run")
        with scoping.as_user(self.dana):
            self.assertTrue(can(self.dana, self.lab, "playbooks", "run"))
            self.assertFalse(can(self.dana, self.glob, "playbooks", "run"))

    def test_revoking_narrows_at_once(self):
        g = self.grant("inventory", "view")
        self.assertIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/inventory/").content)
        g.delete()
        self.assertNotIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/inventory/").content)

    def test_a_scoped_api_key_never_carries_a_grant(self):
        self.grant("inventory", "view")
        keyed = get_user_model().objects.get(pk=self.dana.pk)
        keyed.key_site_ids = {self.west.pk}
        with scoping.as_user(keyed):
            self.assertEqual(visible_site_ids(keyed, app="inventory"), {self.west.pk})
        self.assertIsNone(visible_site_ids(self.dana, app="inventory"))

    def test_only_instance_admins_grant_and_it_is_audited(self):
        body = {"user_id": self.dana.pk, "app": "inventory", "verb": "view"}

        def post(user):
            self.client.force_login(user)
            return self.client.post("/api/v1/sites/grants/", data=json.dumps(body),
                                    content_type="application/json").status_code

        self.assertEqual(post(self.dana), 403)
        self.assertEqual(post(self.gwen), 403)
        self.assertEqual(post(self.ida), 201)
        self.assertTrue(AuditEvent.objects.filter(action="module_grant.added", target="dana").exists())
        body["verb"] = "edit"
        self.assertEqual(post(self.ida), 400)
        grant = ModuleGrant.objects.get(user=self.dana)
        self.client.force_login(self.dana)
        self.assertEqual(self.client.delete(f"/api/v1/sites/grants/{grant.pk}/").status_code, 403)
        self.client.force_login(self.ida)
        self.assertEqual(self.client.delete(f"/api/v1/sites/grants/{grant.pk}/").status_code, 204)
        self.assertTrue(AuditEvent.objects.filter(action="module_grant.removed").exists())

    def test_a_module_view_never_widens_a_write(self):
        self.grant("stacks", "view")

        @scoping.module_view("stacks")
        def view(request):
            return sorted(Host.objects.values_list("hostname", flat=True))

        with scoping.as_user(self.dana):
            self.assertEqual(view(SimpleNamespace(method="GET")), ["lab-1", "west-1"])
            self.assertEqual(view(SimpleNamespace(method="POST")), ["west-1"])

    def test_a_grant_never_widens_outside_a_module_view(self):
        # Security review of 04f: the model's own module must not widen every
        # query, or a POST handler (or any helper) would see other sites' rows.
        self.grant("hosts", "view")
        with scoping.as_user(self.dana):
            self.assertEqual(sorted(Host.objects.values_list("hostname", flat=True)), ["west-1"])

    def test_secret_bearing_rows_never_widen(self):
        from apps.stacks.models import EnvTicket
        self.grant("stacks", "view")

        @scoping.module_view("stacks")
        def view(request):
            return EnvTicket.objects.all().query

        with scoping.as_user(self.dana):
            self.assertIn("siteassignment", str(view(SimpleNamespace(method="GET"))))

    def test_the_walk_stays_clean_without_grants(self):
        # A grant on another module widens nothing here.
        self.grant("vulns", "view")
        self.assertNotIn(b"lab-1", self.get(self.dana, "/api/v1/hosts/inventory/").content)
        self.assertFalse(can(SimpleNamespace(is_authenticated=False), self.lab, "tasks", "run"))

