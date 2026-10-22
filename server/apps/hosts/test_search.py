"""Search everything (QA-25): each kind only as far as its own page shows it."""
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert
from apps.hosts.models import DockerContainer, Host
from apps.policies.models import UpdatePolicy
from apps.software.models import SoftwareItem
from apps.stacks.models import ManagedStack
from apps.tasks.models import TaskDefinition
from apps.vulns.models import VulnFinding

from vigil.testsupport import requires_business

ROOT = Path(__file__).resolve().parents[2]


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class SearchApiTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.admin = users.create_user("admin", password="x")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.viewer = users.create_user("viewer", password="x")
        UserProfile.objects.create(user=self.viewer, role=Role.VIEWER)
        self.web = Host.objects.create(hostname="web-01", agent_token="t1", status=Host.Status.ONLINE,
                                       os="Ubuntu 24.04", ip_address="10.0.0.21", tags=["canary"])
        self.nas = Host.objects.create(hostname="nas", agent_token="t2", status=Host.Status.ONLINE)
        Host.objects.create(hostname="web-rejected", agent_token="t3", status="rejected")

    def _search(self, q, user=None):
        resp = _client(user or self.admin).get("/api/v1/search/", {"q": q})
        self.assertEqual(resp.status_code, 200, resp.content)
        return {g["kind"]: g["results"] for g in resp.json()["groups"]}

    def test_short_queries_return_nothing(self):
        resp = _client(self.admin).get("/api/v1/search/", {"q": "w"})
        self.assertEqual(resp.json()["groups"], [])

    def test_hosts_by_name_tag_and_ip_never_rejected(self):
        self.assertEqual([r["title"] for r in self._search("web")["host"]], ["web-01"])
        self.assertEqual([r["title"] for r in self._search("canary")["host"]], ["web-01"])
        self.assertEqual([r["title"] for r in self._search("10.0.0")["host"]], ["web-01"])

    def test_a_container_carries_its_host(self):
        DockerContainer.objects.create(host=self.nas, container_id="c1", name="vault", image="vaultwarden/server")
        [hit] = self._search("vaultwarden")["container"]
        self.assertEqual((hit["id"], hit["host_id"]), ("vault", str(self.nas.id)))

    def test_stacks_and_policies_are_for_admins_only(self):
        ManagedStack.objects.create(host=self.nas, name="mediabox", compose_yaml="services: {}")
        UpdatePolicy.objects.create(name="mediabox nightly")
        found = self._search("mediabox")
        self.assertIn("stack", found)
        self.assertIn("policy", found)
        found = self._search("mediabox", self.viewer)
        self.assertNotIn("stack", found)
        self.assertNotIn("policy", found)

    def test_apps_are_one_result_per_name(self):
        for host in (self.web, self.nas):
            SoftwareItem.objects.create(host=host, source="dpkg", package_id="firefox", name="Firefox",
                                        name_key="firefox", first_seen=now())
        [hit] = self._search("firefox")["app"]
        self.assertEqual(hit["subtitle"], "on 2 hosts")

    def test_cves_are_grouped_and_open_only(self):
        for host in (self.web, self.nas):
            VulnFinding.objects.create(host=host, scanner="trivy", plugin_id_or_oid="p", cve_id="CVE-2026-1111",
                                       severity="high", state="open", title="openssl overflow")
        VulnFinding.objects.create(host=self.web, scanner="trivy", plugin_id_or_oid="q", cve_id="CVE-2026-2222",
                                   severity="critical", state="fixed")
        [hit] = self._search("CVE-2026")["cve"]
        self.assertEqual(hit["id"], "CVE-2026-1111")
        self.assertTrue(hit["subtitle"].startswith("high · 2 hosts"), hit["subtitle"])

    def test_tasks_mine_and_community_not_others_or_archived(self):
        other = get_user_model().objects.create_user("other", password="x")
        TaskDefinition.objects.create(owner=self.admin, name="restart nginx mine", yaml_source="x")
        TaskDefinition.objects.create(owner=other, name="restart nginx shared", yaml_source="x",
                                      visibility="community")
        TaskDefinition.objects.create(owner=other, name="restart nginx private", yaml_source="x")
        TaskDefinition.objects.create(owner=self.admin, name="restart nginx old", yaml_source="x",
                                      archived_at=now() - timedelta(days=1))
        titles = sorted(r["title"] for r in self._search("restart nginx")["task"])
        self.assertEqual(titles, ["restart nginx mine", "restart nginx shared"])

    def test_only_firing_alerts(self):
        Alert.objects.create(host=self.web, severity="critical", message="disk nearly full", state="firing")
        Alert.objects.create(host=self.web, severity="critical", message="disk was full", state="resolved")
        self.assertEqual([r["title"] for r in self._search("disk")["alert"]], ["disk nearly full"])

    def test_at_most_eight_per_kind(self):
        for i in range(10):
            Host.objects.create(hostname=f"fleet-{i}", agent_token=f"f{i}", status=Host.Status.ONLINE)
        self.assertEqual(len(self._search("fleet")["host"]), 8)


@requires_business
class SearchCrossSiteTests(TestCase):
    """An admin of one site finds nothing that lives in another."""

    def setUp(self):
        from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole
        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=west, role=Role.ADMIN)
        for site in (west, lab):
            host = Host.objects.create(hostname=f"probe-{site.slug}", agent_token=f"t-{site.slug}",
                                       status=Host.Status.ONLINE)
            HostSiteAssignment.objects.create(host=host, site=site)
            ManagedStack.objects.create(host=host, name=f"probe-{site.slug}", compose_yaml="services: {}")
            DockerContainer.objects.create(host=host, container_id=site.slug, name=f"probe-{site.slug}")
            Alert.objects.create(host=host, severity="critical", message=f"probe {site.slug}", state="firing")
            UpdatePolicy.objects.create(name=f"probe {site.slug}", site_id=site.id)

    def test_nothing_from_the_other_site(self):
        resp = _client(self.dana).get("/api/v1/search/", {"q": "probe"})
        found = {g["kind"]: [r["title"] for r in g["results"]] for g in resp.json()["groups"]}
        for kind in ("host", "container", "stack", "alert", "policy"):
            self.assertTrue(found.get(kind), f"{kind}: West's own row is found")
            self.assertFalse([t for t in found[kind] if "lab" in t], f"{kind} leaked Lab: {found[kind]}")


class SearchWiringTests(SimpleTestCase):
    def test_page_sidebar_and_script(self):
        base = (ROOT / "templates/base.html").read_text(encoding="utf-8")
        dash = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
        js = (ROOT / "static/js/vigil-search.js").read_text(encoding="utf-8")
        self.assertIn('data-page="search"', base)
        self.assertIn("js/vigil-search.js", base)
        self.assertIn('{% include "pages/_search.html" %}', dash)
        for needle in ("delegateClick('[data-search-kind]'", "/api/v1/search/", "escHtml(r.title)",
                       "seq !== searchState.seq"):
            self.assertIn(needle, js)
        self.assertNotIn("onclick=", js)
