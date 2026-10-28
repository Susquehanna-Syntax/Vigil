"""Cross-site leak tests for the Jackil integration key.

A separate module because the guard below skips the whole module when the
Business sites app is absent; the key's own tests in test_integration.py
must keep running in Community.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert, AlertRule
from apps.automations.models import Automation
from apps.hosts.models import Host
from apps.jackil.models import JackilTicket
from apps.playbooks.models import Playbook
from apps.tasks.models import Task, TaskDefinition, TaskRun
from vigil.testsupport import require_business_module

from .integration_views import _FINDERS, TYPES
from .test_integration import RESOLVE, SEARCH, UNKNOWN_HOST, _authed, _detail

# ── cross-site ────────────────────────────────────────────────────────────────
# Without the Business sites app there are no per-site roles, so nothing to be
# scoped out of; the module skips rather than passing vacuously.

require_business_module()
from apps_business.sites.models import (
    AutomationSiteAssignment,
    HostSiteAssignment,
    PlaybookSiteAssignment,
    Site,
    UserSiteRole,
)

#: The kinds whose visibility *is* site membership. ``task`` is absent on
#: purpose: a task definition is not a row on a host and has never been
#: site-scoped — the library's rule is "yours plus community ones", asserted
#: separately in `test_task_visibility_is_the_librarys_rule_not_a_site`.
#:
#: What this file exists to catch is an integration endpoint that forgets a
#: filter, and the worst version of it: search hiding a Lab row that detail
#: then hands over. So all three are checked against the same rows.
_SITE_MEMBERSHIP_TYPES = ("host", "playbook", "automation", "run")


class IntegrationLeakTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")

        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.boss = get_user_model().objects.create_user("boss", password="x")
        UserProfile.objects.create(user=self.boss, role=Role.ADMIN)

        definition = TaskDefinition.objects.create(
            name="d", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard", "actions": []})

        # One of everything in each site, named so a leak is unmistakable.
        for site, tag in ((self.west, "west"), (self.lab, "lab")):
            host = Host.objects.create(hostname=f"{tag}-host", agent_token=f"tok-{tag}",
                                       os=f"{tag}os", status=Host.Status.ONLINE,
                                       tags=[tag])
            HostSiteAssignment.objects.create(host=host, site=site)
            setattr(self, f"{tag}_host", host)

            rule = AlertRule.objects.create(name=f"{tag}-rule", category="cpu",
                                            metric="cpu", operator="gt", threshold=90,
                                            severity="warning")
            alert = Alert.objects.create(host=host, rule=rule, severity="warning",
                                         message=f"{tag} alert")
            run = TaskRun.objects.create(name_snapshot=f"{tag} run", host_count=1)
            Task.objects.create(host=host, run=run, action="check_service",
                                nonce=f"n-{tag}", params={})
            JackilTicket.objects.create(alert=alert, ticket_id=1)

            playbook = Playbook.objects.create(name=f"{tag}-playbook",
                                               description=f"the {tag} one")
            PlaybookSiteAssignment.objects.create(playbook=playbook, site=site)

            automation = Automation.objects.create(
                name=f"{tag}-automation", trigger=Automation.Trigger.SCHEDULE,
                action_kind=Automation.ActionKind.TASK, task_definition=definition,
                target=Automation.Target.ALL)
            AutomationSiteAssignment.objects.create(automation=automation, site=site)

        self.dana_key = _authed(user=self.dana)
        self.boss_key = _authed(user=self.boss)

    def _labels(self, client, q, kind=None):
        params = {"q": q} if kind is None else {"q": q, "type": kind}
        return [row["label"] for row in client.get(SEARCH, params).json()["results"]]

    def _lab_ids(self, kind):
        """The Lab rows' ids, taken through the unscoped key's own eyes."""
        return [row["id"] for row in self.boss_key.get(SEARCH, {"q": "lab"}).json()["results"]
                if row["type"] == kind]

    def _ids(self, client, q, kind):
        return [row["id"] for row in
                client.get(SEARCH, {"q": q, "type": kind}).json()["results"]]

    def test_the_fixture_puts_a_row_of_every_kind_in_each_site(self):
        """The leak assertions below take their ids from search, so an empty
        result would otherwise read as "nothing leaked" rather than as the
        fixture having nothing to leak."""
        for kind in _SITE_MEMBERSHIP_TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(len(self._lab_ids(kind)), 1,
                                 f"the unscoped key should find exactly one Lab {kind}")
                self.assertEqual(len(self._ids(self.boss_key, "west", kind)), 1,
                                 f"the unscoped key should find exactly one West {kind}")

    def test_search_hides_another_sites_rows_for_every_kind(self):
        """The search page must not name a Lab row to a West-only key."""
        for kind in _SITE_MEMBERSHIP_TYPES:
            with self.subTest(kind=kind):
                labels = self._labels(self.dana_key, "lab", kind)
                self.assertEqual(labels, [], f"{kind} leaked Lab rows: {labels}")

    def test_search_still_finds_the_callers_own_site(self):
        for kind, q in (("host", "west"), ("playbook", "west"), ("automation", "west"),
                        ("run", "west")):
            with self.subTest(kind=kind):
                labels = self._labels(self.dana_key, q, kind)
                self.assertTrue(any("west" in x.lower() for x in labels),
                                f"{kind} hid the caller's own site: {labels}")

    def test_resolve_omits_another_sites_ids(self):
        """Asking with Lab's ids must not buy Lab's rows — and must not say
        anything either, so a probe cannot tell them from an id that never
        existed."""
        for kind in TYPES:
            with self.subTest(kind=kind):
                ids = self._lab_ids(kind)
                if kind == "task":
                    # Definitions are the library's own visibility rule, not a
                    # site membership: this asserts that, not a leak.
                    continue
                self.assertTrue(ids, f"no Lab {kind} was found by the unscoped key")
                body = self.dana_key.get(RESOLVE, {"type": kind,
                                                   "ids": ",".join(ids)}).json()
                self.assertEqual(body, {"items": {}}, f"{kind} resolved out-of-site rows")

    def test_detail_is_404_for_another_sites_row(self):
        for kind in ("host", "playbook", "automation", "run"):
            with self.subTest(kind=kind):
                pk = self._lab_ids(kind)[0]
                response = self.dana_key.get(_detail(kind, pk))
                self.assertEqual(response.status_code, 404, response.content)
                self.assertEqual(response.json(), {"error": "Not found"})

    def test_detail_of_another_sites_row_is_unknowable(self):
        """The 404 a scoped key gets is byte-identical to the one for an id
        that never existed, so probing ids cannot discover a Lab row."""
        pk = self._lab_ids("host")[0]
        hidden = self.dana_key.get(_detail("host", pk))
        unknown = self.dana_key.get(_detail("host", UNKNOWN_HOST))
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.content, unknown.content)

    def test_the_finders_are_the_pages_own_filters(self):
        """A guard against a finder drifting from the page it copies: search,
        resolve and detail share one queryset per kind, so the leak tests are
        only as good as that queryset matching what the page itself shows."""
        from apps.hosts.search import _hosts as page_hosts

        # The hosts page and the key's finder must answer with the same rows,
        # in the same order, for the same query.
        request = type("R", (), {"user": self.dana})()
        self.assertEqual([row["id"] for row in page_hosts(request, "host")],
                         [str(pk) for pk in
                          _FINDERS["host"][0](self.dana).values_list("pk", flat=True)])
        self.assertEqual([row["id"] for row in page_hosts(request, "west")],
                         [str(self.west_host.id)])

    def test_an_unscoped_admins_key_sees_both_sites(self):
        """The other half: filtering must not be over-eager, or the fleet-wide
        admin's own helpdesk goes blind."""
        for kind in _SITE_MEMBERSHIP_TYPES:
            with self.subTest(kind=kind):
                labels = self._labels(self.boss_key, "lab", kind)
                self.assertTrue(any("lab" in x.lower() for x in labels),
                                f"{kind}: the unscoped key could not find Lab: {labels}")

    def test_task_visibility_is_the_librarys_rule_not_a_site(self):
        """A private definition belongs to its owner alone, whoever holds a
        site; a community one is browsable by every key. That is the task
        library's own rule (``search._tasks``), unchanged here."""
        TaskDefinition.objects.create(
            owner=self.dana, name="west private task", visibility="private",
            yaml_source="", parsed_spec={"risk": "standard", "actions": []})
        TaskDefinition.objects.create(
            owner=self.boss, name="lab community task", visibility="community",
            yaml_source="", parsed_spec={"risk": "standard", "actions": []})

        dana = _authed(user=self.dana)
        self.assertEqual(self._labels(dana, "private", "task"), ["west private task"])
        self.assertEqual(self._labels(self.boss_key, "private", "task"), [])
        self.assertEqual(self._labels(dana, "community", "task"), ["lab community task"])
        self.assertEqual(self._labels(self.boss_key, "community", "task"),
                         ["lab community task"])
