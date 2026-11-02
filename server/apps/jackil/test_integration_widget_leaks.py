"""Cross-site leak tests for the widget endpoints.

A separate module because the guard below skips the whole module when the
Business sites app is absent; the widget endpoints' own tests in
test_integration_widgets.py must keep running in Community.

A dashboard is a worse leak than a search box: it is the first thing an agent
sees, it shows counts, and a count of another site's offline hosts names
nothing while still telling them the site exists and is broken.
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

from .test_integration import _authed
from .test_integration_widgets import WIDGET, WIDGETS_URL, _assert_contract

require_business_module()
from apps_business.sites.models import (
    AutomationSiteAssignment,
    HostSiteAssignment,
    PlaybookSiteAssignment,
    Site,
    UserSiteRole,
)


class IntegrationWidgetLeakTests(TestCase):
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

        # One of everything in each site, with the word for the other site in
        # every string a widget can echo back.
        for site, tag in ((self.west, "west"), (self.lab, "lab")):
            host = Host.objects.create(hostname=f"{tag}-host", agent_token=f"tok-{tag}",
                                       os=f"{tag}os", status=Host.Status.OFFLINE,
                                       tags=[tag])
            HostSiteAssignment.objects.create(host=host, site=site)
            setattr(self, f"{tag}_host", host)

            rule = AlertRule.objects.create(name=f"{tag}-rule", category="cpu",
                                            metric="cpu", operator="gt", threshold=90,
                                            severity="critical")
            alert = Alert.objects.create(host=host, rule=rule, severity="critical",
                                         message=f"{tag} alert")
            JackilTicket.objects.create(alert=alert, ticket_id=1)

            run = TaskRun.objects.create(name_snapshot=f"{tag} run", host_count=1,
                                         state="failed")
            Task.objects.create(host=host, run=run, action="check_service",
                                nonce=f"n-{tag}", params={})

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

    def _data(self, client, kind, params=None):
        response = client.get(WIDGET.format(kind), params or {})
        self.assertEqual(response.status_code, 200, response.content)
        envelope = response.json()
        _assert_contract(envelope)
        return envelope["data"]

    def _texts(self, data):
        """Everything a dashboard tile can put on screen, as one string."""
        rows = [str(data.get("label", "")), str(data.get("sub", "")),
                str(data.get("value", ""))]
        for item in data.get("items", []):
            rows += [str(item.get(key, "")) for key in ("title", "meta", "badge", "href")]
        return " | ".join(rows)

    def test_the_fixture_puts_a_row_of_every_kind_in_each_site(self):
        """Otherwise "nothing leaked" would only mean the fixture was empty."""
        self.assertEqual(self._data(self.boss_key, "stat", {"metric": "hosts_total"})["value"], 2)
        self.assertEqual(len(self._data(self.boss_key, "alert_list")["items"]), 2)
        self.assertEqual(len(self._data(self.boss_key, "recent_runs")["items"]), 2)
        self.assertEqual(len(self._data(self.boss_key, "offline_hosts")["items"]), 2)

    def test_dana_never_counts_another_sites_rows(self):
        for metric, want in (("hosts_total", 1), ("hosts_offline", 1),
                             ("alerts_firing", 1), ("alerts_critical", 1),
                             ("runs_failed_24h", 1), ("pending_enrollments", 0)):
            with self.subTest(metric=metric):
                data = self._data(self.dana_key, "stat", {"metric": metric})
                self.assertEqual(data["value"], want,
                                 f"{metric} counted another site: {data}")

    def test_dana_never_lists_another_sites_rows(self):
        for kind in ("alert_list", "recent_runs", "offline_hosts"):
            with self.subTest(kind=kind):
                data = self._data(self.dana_key, kind)
                self.assertEqual(len(data["items"]), 1, f"{kind} listed another site: {data}")
                self.assertNotIn("lab", self._texts(data).lower(),
                                 f"{kind} named a Lab row: {data}")

    def test_danas_gauge_is_her_own_fleet(self):
        data = self._data(self.dana_key, "hosts_online")
        self.assertEqual(data["value"], 0.0, data)
        self.assertEqual(data["sub"], "0 of 1 online")
        self.assertNotIn("lab", self._texts(data).lower())

    def test_the_catalogue_says_nothing_about_a_site(self):
        """The catalogue is static text, but it is served through the same
        key, so it is checked here rather than assumed."""
        body = self.dana_key.get(WIDGETS_URL).json()
        self.assertEqual([row["kind"] for row in body["widgets"]],
                         ["hosts_online", "stat", "alert_list", "recent_runs", "offline_hosts"])

    def test_an_unscoped_admins_key_sees_both_sites(self):
        """The other half: filtering that is over-eager blinds the fleet-wide
        admin's own dashboard."""
        for kind in ("alert_list", "recent_runs", "offline_hosts"):
            with self.subTest(kind=kind):
                texts = self._texts(self._data(self.boss_key, kind)).lower()
                self.assertIn("west", texts, f"{kind} hid West from the unscoped key")
                self.assertIn("lab", texts, f"{kind} hid Lab from the unscoped key")
        self.assertEqual(
            self._data(self.boss_key, "stat", {"metric": "alerts_critical"})["value"], 2)
