"""The fleet view by update: grouping, ages, scoping and decisions."""
from datetime import timedelta

from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from .models import PendingUpdate, UpdateDecision


def _client(username, role=Role.ADMIN):
    user = get_user_model().objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=role)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _host(name):
    return Host.objects.create(hostname=name, agent_token=f"tok-{name}",
                               status=Host.Status.ONLINE, mode="managed")


def _pending(host, key, days_ago, kind="windows", severity="critical"):
    stamp = now() - timedelta(days=days_ago)
    return PendingUpdate.objects.create(host=host, kind=kind, key=key, title=f"t {key}",
                                        severity=severity, first_seen=stamp, last_seen=now())


class UpdateListTests(TestCase):
    def setUp(self):
        self.client = _client("upd-admin")
        self.a, self.b = _host("a"), _host("b")
        _pending(self.a, "KB1", 40)
        _pending(self.b, "KB1", 3)
        _pending(self.a, "KB2", 1, severity="important")
        _pending(self.b, "curl", 9, kind="linux", severity="")

    def test_grouped_by_update_oldest_first(self):
        rows = self.client.get("/api/v1/software/updates/").json()["results"]
        self.assertEqual([(r["key"], r["hosts"]) for r in rows],
                         [("KB1", 2), ("curl", 1), ("KB2", 1)])
        self.assertEqual(rows[0]["age_days"], 40)
        self.assertEqual(rows[0]["decision"], "undecided")

    def test_filters(self):
        get = lambda qs: [r["key"] for r in  # noqa: E731
                          self.client.get(f"/api/v1/software/updates/?{qs}").json()["results"]]
        self.assertEqual(get("kind=linux"), ["curl"])
        self.assertEqual(get("severity=important"), ["KB2"])
        self.assertEqual(get("q=kb2"), ["KB2"])
        UpdateDecision.objects.create(kind="windows", key="KB1", decision="declined")
        self.assertEqual(get("decision=declined"), ["KB1"])
        self.assertEqual(get("decision=undecided"), ["curl", "KB2"])

    def test_drill_down_to_hosts(self):
        rows = self.client.get("/api/v1/software/updates/hosts/?kind=windows&key=KB1").json()
        self.assertEqual([r["hostname"] for r in rows["results"]], ["a", "b"])

    def test_decide_and_clear(self):
        resp = self.client.post("/api/v1/software/updates/decide/",
                                {"kind": "windows", "keys": ["KB1", "KB2"],
                                 "decision": "declined"}, format="json")
        self.assertEqual(resp.json(), {"declined": 2})
        self.client.post("/api/v1/software/updates/decide/",
                         {"kind": "windows", "keys": ["KB1"], "decision": "approved"},
                         format="json")
        self.assertEqual(UpdateDecision.objects.get(key="KB1").decision, "approved")
        self.assertEqual(UpdateDecision.objects.count(), 2, "one row per update, replaced")
        self.client.post("/api/v1/software/updates/decide/",
                         {"kind": "windows", "keys": ["KB1"], "decision": "clear"},
                         format="json")
        self.assertEqual(list(UpdateDecision.objects.values_list("key", flat=True)), ["KB2"])

    def test_decide_refusals_and_permissions(self):
        for body in ({"kind": "mac", "keys": ["x"], "decision": "approved"},
                     {"kind": "windows", "keys": [], "decision": "approved"},
                     {"kind": "windows", "keys": [""], "decision": "approved"},
                     {"kind": "windows", "keys": ["KB1"], "decision": "maybe"}):
            with self.subTest(body=body):
                resp = self.client.post("/api/v1/software/updates/decide/", body,
                                        format="json")
                self.assertEqual(resp.status_code, 400)
        viewer = _client("upd-viewer", role=Role.VIEWER)
        self.assertEqual(viewer.get("/api/v1/software/updates/").status_code, 200)
        resp = viewer.post("/api/v1/software/updates/decide/",
                           {"kind": "windows", "keys": ["KB1"], "decision": "declined"},
                           format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(UpdateDecision.objects.exists())

    def test_site_scoping(self):
        west = Site.objects.create(name="West", slug="west")
        HostSiteAssignment.objects.create(host=self.a, site=west)
        dana = get_user_model().objects.create_user("dana", password="pw")
        UserProfile.objects.create(user=dana, role=Role.VIEWER)
        UserSiteRole.objects.create(user=dana, site=west, role=Role.ADMIN)
        scoped = APIClient()
        scoped.force_authenticate(user=dana)
        rows = scoped.get("/api/v1/software/updates/").json()["results"]
        self.assertEqual({r["key"]: r["hosts"] for r in rows}, {"KB1": 1, "KB2": 1})


class ByUpdateTabWiringTests(TestCase):
    """No JS runner: the ids and calls the tab depends on, pinned by source scan."""

    def test_template_and_script_agree(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        html = (root / "templates/pages/_apps.html").read_text(encoding="utf-8")
        js = (root / "static/js/vigil-apps.js").read_text(encoding="utf-8")
        self.assertIn('id="apps-by-update"', html)
        for element_id in ("apps-updates-body", "upd-q", "upd-kind",
                           "upd-decision", "upd-bulk", "upd-all"):
            self.assertIn(f'id="{element_id}"', html)
            self.assertTrue(f"'{element_id}'" in js, element_id)
        self.assertIn('data-tab="apps-by-update"', html)
        for call in ("/api/v1/software/updates/", "/api/v1/software/updates/hosts/",
                     "/api/v1/software/updates/decide/"):
            self.assertIn(call, js)
