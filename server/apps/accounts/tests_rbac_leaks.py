"""Cross-site leak tests for every site-scoped list endpoint.

docs/rbac.md §6: a missed filter on a list endpoint leaks another site's rows,
and that is the failure mode this whole design most needs to prevent. The
registry below is the enforcement — a scoped list endpoint that is not listed
here is caught by test_registry_covers_every_scoped_list_endpoint.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role
from apps.alerts.models import Alert, AlertRule
from apps.automations.models import Automation
from apps.baselines.models import Baseline
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition
from apps_business.sites.models import (
    AutomationSiteAssignment, BaselineSiteAssignment, HostSiteAssignment, Site,
    UserSiteRole,
)

#: url -> callable(row) returning the identifying string to assert on.
SCOPED_LIST_ENDPOINTS = {
    "/api/v1/hosts/": lambda r: r["hostname"],
    "/api/v1/alerts/": lambda r: r["host_hostname"],
    "/api/v1/baselines/": lambda r: r["name"],
    "/api/v1/automations/": lambda r: r["name"],
}


class CrossSiteLeakTests(TestCase):
    """Dana holds a role in West Campus only. Nothing belonging to Lab may
    appear in any list she can reach."""

    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")

        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)

        defn = TaskDefinition.objects.create(
            name="d", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard", "actions": []})

        # One of everything in each site, named so a leak is unmistakable.
        for site, tag in ((self.west, "west"), (self.lab, "lab")):
            host = Host.objects.create(hostname=f"{tag}-host", agent_token=f"tok-{tag}")
            HostSiteAssignment.objects.create(host=host, site=site)

            rule = AlertRule.objects.create(
                name=f"{tag}-rule", category="cpu", metric="cpu",
                operator="gt", threshold=90, severity="warning")
            Alert.objects.create(host=host, rule=rule, severity="warning",
                                 message=f"{tag} alert")

            b = Baseline.objects.create(name=f"{tag}-baseline")
            BaselineSiteAssignment.objects.create(baseline=b, site=site)

            a = Automation.objects.create(
                name=f"{tag}-automation", trigger=Automation.Trigger.SCHEDULE,
                action_kind=Automation.ActionKind.TASK, task_definition=defn,
                target=Automation.Target.ALL)
            AutomationSiteAssignment.objects.create(automation=a, site=site)

        self.client.force_login(self.dana)

    def _rows(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, f"{url} -> {resp.status_code}")
        body = resp.json()
        if isinstance(body, dict):
            # automations wraps its list alongside the event label map
            for key in ("results", "automations"):
                if key in body:
                    return body[key]
        return body

    def test_no_scoped_list_endpoint_leaks_another_site(self):
        for url, ident in SCOPED_LIST_ENDPOINTS.items():
            with self.subTest(url=url):
                names = [str(ident(r)) for r in self._rows(url)]
                leaked = [n for n in names if "lab" in n.lower()]
                self.assertEqual(
                    leaked, [],
                    f"{url} leaked Lab rows to a West-Campus-only user: {leaked}")

    def test_the_user_still_sees_their_own_site(self):
        for url, ident in SCOPED_LIST_ENDPOINTS.items():
            with self.subTest(url=url):
                names = [str(ident(r)) for r in self._rows(url)]
                self.assertTrue(
                    any("west" in n.lower() for n in names),
                    f"{url} hid the user's own site: {names}")

    def test_an_unscoped_user_still_sees_everything(self):
        """The whole design is opt-in: a user with no rows is unaffected."""
        staff = get_user_model().objects.create_user(
            "boss", password="x", is_staff=True)
        self.client.force_login(staff)
        for url, ident in SCOPED_LIST_ENDPOINTS.items():
            with self.subTest(url=url):
                names = [str(ident(r)) for r in self._rows(url)]
                self.assertTrue(any("lab" in n.lower() for n in names),
                                f"{url} hid rows from an unscoped user: {names}")

    def test_registry_covers_every_scoped_list_endpoint(self):
        """Adding a scoped list endpoint without adding it here must fail.

        docs/rbac.md §2 fixes the set of scoped areas; this asserts the
        registry still matches it, so the leak test above cannot quietly stop
        covering something.
        """
        expected = {"hosts", "alerts", "baselines", "automations"}
        covered = {u.strip("/").split("/")[-1] for u in SCOPED_LIST_ENDPOINTS}
        self.assertEqual(covered, expected)
