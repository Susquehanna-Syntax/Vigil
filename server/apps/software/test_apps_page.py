"""The Apps page: the per-host summary endpoint and the UI wiring.

The UI has no JS runner (the architect drives it in Chromium), so its wiring is
pinned by source scan: the ids and calls the page depends on.
"""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from .test_software_ingest import digest_of, ingest_software, item, payload

SERVER_ROOT = Path(__file__).resolve().parents[2]


def _source(*parts):
    return SERVER_ROOT.joinpath(*parts).read_text(encoding="utf-8")


class HostSummariesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("apps-admin", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _host(self, hostname, token, items=None):
        host = Host.objects.create(hostname=hostname, agent_token=token,
                                   status=Host.Status.ONLINE)
        if items is not None:
            ingest_software(host, payload(digest_of(token), items))
        return host

    def test_hosts_summary_counts_and_scoping(self):
        reported = self._host("bravo", "tok-b", [
            item("dpkg", "one", "One App", "1.0", "2.0"),               # outdated
            item("registry", "two", "Two App", "1.0", managed=False),   # unmanaged
            item("dpkg", "three", "Three App", "1.0", "1.0"),
        ])
        silent = self._host("alpha", "tok-a", items=None)
        rejected = self._host("zulu", "tok-z", [item("dpkg", "four", "Four App", "1.0")])
        rejected.status = Host.Status.REJECTED
        rejected.save(update_fields=["status"])

        body = self.client.get("/api/v1/software/hosts/").json()
        self.assertEqual(body["count"], 2)
        rows = body["results"]
        self.assertEqual([r["hostname"] for r in rows], ["alpha", "bravo"])

        empty, full = rows
        self.assertEqual(empty["host_id"], str(silent.id))
        self.assertEqual((empty["item_count"], empty["outdated"], empty["unmanaged"]),
                         (0, 0, 0))
        self.assertIsNone(empty["received_at"], "a host with no snapshot reports null")
        self.assertEqual(empty["errors"], {})

        self.assertEqual(full["host_id"], str(reported.id))
        self.assertEqual((full["item_count"], full["outdated"], full["unmanaged"]),
                         (3, 1, 1))
        self.assertIsNotNone(full["received_at"])
        self.assertEqual(full["errors"], {})
        self.assertNotIn(str(rejected.id), [r["host_id"] for r in rows],
                         "a rejected host must not appear")

    def test_hosts_summary_query_count(self):
        for index in range(5):
            self._host(f"host-{index}", f"tok-{index}",
                       [item("dpkg", f"p{index}", f"P {index}", "1.0", "2.0")])
        with self.assertNumQueries(4):
            body = self.client.get("/api/v1/software/hosts/").json()
        self.assertEqual(body["count"], 5)
        self.assertTrue(all(r["outdated"] == 1 for r in body["results"]))

    def test_hosts_summary_requires_authentication(self):
        response = APIClient().get("/api/v1/software/hosts/")
        self.assertIn(response.status_code, (401, 403))


class AppsPageWiringTests(TestCase):
    """The Apps page exists and is reachable, pinned from source."""

    def test_page_is_wired(self):
        self.assertIn('{% include "pages/_apps.html" %}', _source("templates", "dashboard.html"))

        base = _source("templates", "base.html")
        self.assertIn('data-page="apps"', base)
        self.assertLess(base.index("vigil-inventory.js"), base.index("vigil-apps.js"),
                        "vigil-apps.js must load after vigil-inventory.js wraps navigateTo")

        monitor_page = _source("templates", "pages", "_monitor.html")
        for element_id in ('id="software-list"', 'id="software-count"', 'id="software-q"'):
            self.assertIn(element_id, monitor_page, element_id)

        apps_page = _source("templates", "pages", "_apps.html")
        for element_id in ("page-apps", "apps-q", "apps-outdated", "apps-unmanaged",
                           "apps-source", "apps-table", "apps-more", "apps-by-app",
                           "apps-by-host", "apps-hosts-table"):
            self.assertIn(element_id, apps_page, element_id)

        self.assertIn("renderHostSoftware", _source("static", "js", "vigil-monitor.js"))

    def test_apps_js_escapes_api_values(self):
        js = _source("static", "js", "vigil-apps.js")
        for field in ("name", "version", "hostname", "publisher", "package_id", "user"):
            self.assertRegex(js, r"escHtml\([A-Za-z_$][\w$.]*\." + field + r"\b",
                             f"{field} reaches innerHTML unescaped")
        self.assertRegex(js, r"/software/apps/\$\{encodeURIComponent\(",
                         "name_key must be URL-encoded in the app detail URL")
