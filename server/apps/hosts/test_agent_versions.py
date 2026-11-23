"""GET /api/v1/hosts/agent-versions/: the version-drift report (sprint M8)."""
from datetime import timedelta
from unittest import skipUnless

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

URL = "/api/v1/hosts/agent-versions/"
SHIPPED = "2026.17.0"


def recent(days=1, **fields):
    return {"at": (now() - timedelta(days=days)).isoformat(), **fields}


class AgentVersionsTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_user("admin", password="x")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(admin)
        self.count = 0

    def host(self, version, status=Host.Status.ONLINE, agent_update=None, hostname=None):
        self.count += 1
        return Host.objects.create(
            hostname=hostname or f"h-{self.count}", agent_token=f"tok-{self.count}",
            status=status, agent_version=version, agent_update=agent_update)

    def get(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    @override_settings(VIGIL_AGENT_VERSION=SHIPPED)
    def test_counts_order_and_flags(self):
        self.host(SHIPPED)
        self.host(SHIPPED, status=Host.Status.OFFLINE)
        self.host("2026.16.0")
        self.host("2026.9.0")
        self.host("")

        data = self.get()

        self.assertEqual(data["expected"], SHIPPED)
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["versions"], [
            {"version": SHIPPED, "hosts": 2, "current": True, "outdated": False},
            {"version": "2026.16.0", "hosts": 1, "current": False, "outdated": True},
            {"version": "2026.9.0", "hosts": 1, "current": False, "outdated": True},
            {"version": "", "hosts": 1, "current": False, "outdated": False},
        ])
        self.assertEqual(data["outdated_hosts"], 2)

    @override_settings(VIGIL_AGENT_VERSION=SHIPPED)
    def test_unparseable_version_sorts_last_and_is_never_outdated(self):
        self.host("2026.16.0")
        self.host("nightly")

        versions = self.get()["versions"]

        self.assertEqual(versions[0]["version"], "2026.16.0")
        self.assertEqual(versions[1], {"version": "nightly", "hosts": 1,
                                       "current": False, "outdated": False})

    @override_settings(VIGIL_AGENT_VERSION=SHIPPED)
    def test_pending_and_rejected_hosts_are_not_counted(self):
        self.host(SHIPPED)
        self.host("2026.9.0", status=Host.Status.PENDING)
        self.host("2025.1.0", status=Host.Status.REJECTED)

        data = self.get()

        self.assertEqual(data["total"], 1)
        self.assertEqual([v["version"] for v in data["versions"]], [SHIPPED])
        self.assertEqual(data["outdated_hosts"], 0)

    @override_settings(VIGIL_AGENT_VERSION=SHIPPED)
    def test_updates_count_recent_reports_and_list_rolled_back_newest_first(self):
        self.host(SHIPPED, agent_update=recent(state="confirmed", from_version="2026.16.0",
                                               to_version=SHIPPED, running=SHIPPED))
        self.host("2026.16.0", agent_update=recent(
            days=2, state="rolled_back", from_version="2026.16.0", to_version=SHIPPED,
            running="2026.16.0", reason="3 check-ins failed"),
            hostname="old-web")
        self.host("2026.16.0", agent_update=recent(
            days=5, state="rolled_back", from_version="2026.16.0", to_version=SHIPPED,
            running="2026.16.0", reason="older one"), hostname="web-02")
        self.host("2026.16.0", agent_update=recent(state="not_applied", from_version="2026.16.0",
                                                   to_version=SHIPPED, running="2026.16.0",
                                                   reason="disk full"))
        # Forty days old: outside the 30-day window, so neither counted nor listed.
        self.host("2026.16.0", agent_update=recent(
            days=40, state="rolled_back", from_version="2026.16.0", to_version=SHIPPED,
            running="2026.16.0", reason="ancient"), hostname="ancient-host")
        self.host("2026.16.0", agent_update=recent(days=40, state="confirmed"))
        # No readable time: ignored.
        self.host("2026.16.0", agent_update={"state": "confirmed", "at": "not a date"})

        data = self.get()

        self.assertEqual(data["updates"], {"confirmed": 1, "rolled_back": 2, "not_applied": 1})
        rows = data["rolled_back"]
        self.assertEqual([r["hostname"] for r in rows], ["old-web", "web-02"])
        self.assertEqual(rows[0]["reason"], "3 check-ins failed")
        self.assertEqual(rows[0]["from_version"], "2026.16.0")
        self.assertEqual(rows[0]["to_version"], SHIPPED)
        self.assertTrue(rows[0]["host_id"])

    @override_settings(VIGIL_AGENT_VERSION="")
    def test_without_a_shipped_version_nothing_is_outdated(self):
        self.host("2026.9.0")
        self.host("2026.16.0")

        data = self.get()

        self.assertEqual(data["expected"], "")
        self.assertEqual(data["outdated_hosts"], 0)
        self.assertTrue(all(not v["current"] and not v["outdated"] for v in data["versions"]))

    def test_anonymous_is_refused(self):
        resp = APIClient().get(URL)
        self.assertIn(resp.status_code, (401, 403))

    @skipUnless(apps.is_installed("apps_business.sites"), "needs the Business sites app")
    @override_settings(VIGIL_AGENT_VERSION=SHIPPED)
    def test_a_site_scoped_user_counts_only_their_own_sites(self):
        from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole

        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=dana, site=west, role=Role.ADMIN)
        west_host = self.host("2026.16.0", hostname="west-1")
        lab_host = self.host("2026.9.0", hostname="lab-1")
        HostSiteAssignment.objects.create(host=west_host, site=west)
        HostSiteAssignment.objects.create(host=lab_host, site=lab)

        client = APIClient()
        client.force_authenticate(dana)
        data = client.get(URL).json()

        self.assertEqual(data["total"], 1)
        self.assertEqual([v["version"] for v in data["versions"]], ["2026.16.0"])
