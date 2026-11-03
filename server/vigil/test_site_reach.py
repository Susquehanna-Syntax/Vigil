"""A site's playbooks and automations act only on that site's hosts.

Before this, an automation assigned to Lab with target "all hosts" (or
tags) dispatched to every site's machines, a Lab event automation fired on
West's events, and a Lab playbook matched West hosts. Global ones still
reach every host.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations import engine
from apps.automations.models import Automation
from apps.hosts.models import Host
from apps.playbooks.models import Playbook
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import (  # noqa: E402
    AutomationSiteAssignment,
    HostSiteAssignment,
    PlaybookSiteAssignment,
    Site,
)


class SiteReachTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.west = Site.objects.create(name="West", slug="west")
        cls.lab = Site.objects.create(name="Lab", slug="lab")
        cls.user = get_user_model().objects.create_user("root", password="x", is_staff=True)
        cls.hosts = {}
        for name, site in (("west-1", cls.west), ("lab-1", cls.lab)):
            h = Host.objects.create(hostname=name, agent_token=f"tok-{name}",
                                    status=Host.Status.ONLINE, mode="managed")
            HostSiteAssignment.objects.create(host=h, site=site)
            cls.hosts[name] = h

    def automation(self, site=None, **kw):
        a = Automation.objects.create(
            name=f"a-{Automation.objects.count()}", trigger=kw.pop("trigger", Automation.Trigger.SCHEDULE),
            action_kind=Automation.ActionKind.TASK, target=kw.pop("target", Automation.Target.ALL), **kw)
        if site is not None:
            AutomationSiteAssignment.objects.create(automation=a, site=site)
        return Automation.objects.get(pk=a.pk)

    def names(self, hosts):
        return sorted(h.hostname for h in hosts)

    def test_site_automation_targeting_all_reaches_only_its_site(self):
        lab_auto = self.automation(self.lab)
        self.assertEqual(self.names(engine._resolve_hosts(lab_auto, None)), ["lab-1"])
        glob_auto = self.automation()
        self.assertEqual(self.names(engine._resolve_hosts(glob_auto, None)), ["lab-1", "west-1"])

    def test_site_automation_never_targets_another_sites_host(self):
        auto = self.automation(self.lab, target=Automation.Target.HOST,
                               target_host=self.hosts["west-1"])
        self.assertEqual(engine._resolve_hosts(auto, None), [])
        auto = self.automation(self.lab, target=Automation.Target.EVENT_HOST)
        self.assertEqual(engine._resolve_hosts(auto, self.hosts["west-1"]), [])
        self.assertEqual(self.names(engine._resolve_hosts(auto, self.hosts["lab-1"])), ["lab-1"])

    def test_site_event_automation_ignores_another_sites_events(self):
        self.automation(self.lab, trigger=Automation.Trigger.EVENT, event="host_approved",
                        target=Automation.Target.EVENT_HOST)
        with mock.patch.object(engine, "run_automation") as run:
            engine.handle_event("host_approved", {"host": self.hosts["west-1"]})
            run.assert_not_called()
            engine.handle_event("host_approved", {"host": self.hosts["lab-1"]})
            run.assert_called_once()

    def test_events_fire_whoever_raised_them(self):
        # Raised inside a West-only user's request, Lab's automation for a
        # Lab host still fires, though dana cannot see it: dispatch is
        # install-wide.
        from apps.accounts.models import Role
        from apps_business.sites.models import UserSiteRole
        dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=dana, site=self.west, role=Role.ADMIN)
        self.automation(self.lab, trigger=Automation.Trigger.EVENT, event="host_approved",
                        target=Automation.Target.EVENT_HOST)
        with mock.patch.object(engine, "run_automation") as run, scoping.as_user(dana):
            engine.handle_event("host_approved", {"host": self.hosts["lab-1"]})
            run.assert_called_once()

    def test_site_playbook_matches_only_its_sites_hosts(self):
        lab_pb = Playbook.objects.create(name="lab pb", created_by=self.user)
        PlaybookSiteAssignment.objects.create(playbook=lab_pb, site=self.lab)
        lab_pb = Playbook.objects.get(pk=lab_pb.pk)
        glob_pb = Playbook.objects.create(name="global pb", created_by=self.user)
        self.assertTrue(lab_pb.matches(self.hosts["lab-1"]))
        self.assertFalse(lab_pb.matches(self.hosts["west-1"]))
        self.assertTrue(glob_pb.matches(self.hosts["west-1"]))

    def test_fleet_reach_costs_one_site_query(self):
        hosts = list(Host.objects.all())
        with self.assertNumQueries(2):  # the global site id, then every assignment
            scoping.prime_site_ids(hosts)
        lab_pb = Playbook.objects.create(name="lab pb2", created_by=self.user)
        PlaybookSiteAssignment.objects.create(playbook=lab_pb, site=self.lab)
        scoping.site_id_of(lab_pb)
        with self.assertNumQueries(0):
            [scoping.within_site(lab_pb, h) for h in hosts]

    def test_site_automation_rollout_is_limited_to_its_site(self):
        from apps.tasks.models import TaskDefinition
        definition = TaskDefinition.objects.create(
            name="noop", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard", "actions": []})
        lab_auto = self.automation(self.lab, dispatch_mode=Automation.DispatchMode.ROLLOUT,
                                   task_definition=definition)
        glob_auto = self.automation(dispatch_mode=Automation.DispatchMode.ROLLOUT,
                                    task_definition=definition)
        with mock.patch("apps.tasks.rollout.start_rollout") as start:
            engine._start_rollout_for(lab_auto)
            self.assertEqual(start.call_args.kwargs["host_ids"], [str(self.hosts["lab-1"].pk)])
            engine._start_rollout_for(glob_auto)
            self.assertIsNone(start.call_args.kwargs["host_ids"])


class RolloutSiteRecheckTests(TestCase):
    """A host moved to another site mid-rollout drops out of the next wave:
    the rollout's site is re-applied at every wave, not only the allow-list
    captured when it started."""

    def test_moved_host_is_not_dispatched(self):
        from apps.tasks.models import PatchRollout, Task
        from apps.tasks.rollout import start_rollout
        from apps.tasks.test_rollout import make_definition, make_host, make_wave

        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        user = get_user_model().objects.create_user("op", password="pw")
        definition = make_definition(user)
        make_wave("Only", 1, ["role:web"], validation_hours=0)
        stays = make_host("lab-stays", ["role:web"])
        moves = make_host("lab-moves", ["role:web"])
        for h in (stays, moves):
            HostSiteAssignment.objects.create(host=h, site=lab)
        # The allow-list was captured while both were Lab's; one has since moved.
        HostSiteAssignment.objects.filter(host=moves).update(site=west)
        rollout = start_rollout(definition, user=user, host_ids=[stays.pk, moves.pk],
                                site_id=lab.pk)
        self.assertEqual(PatchRollout.objects.get(pk=rollout.pk).site_id, lab.pk)
        self.assertEqual(sorted(Task.objects.values_list("host__hostname", flat=True)),
                         ["lab-stays"])


class PlaybookBoundsAutomationTests(TestCase):
    """A global automation that runs West's playbook reaches West only: West's
    admins edit those steps, so it must not carry them to Lab. A Lab
    automation running West's playbook reaches nothing."""

    def setUp(self):
        from apps.automations.models import Automation
        from apps.hosts.models import Host
        from apps.playbooks.models import Playbook
        from apps_business.sites.models import HostSiteAssignment, Site
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        for name, site in (("west-1", self.west), ("lab-1", self.lab)):
            h = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                    status=Host.Status.ONLINE, mode="managed")
            HostSiteAssignment.objects.create(host=h, site=site)
        self.west_pb = Playbook.objects.create(name="West pb")
        scoping.place(self.west_pb, self.west.pk)
        self.auto = Automation.objects.create(name="a", trigger="schedule", target="all",
                                              action_kind="playbook", playbook=self.west_pb)

    def names(self):
        from apps.automations.engine import _resolve_hosts
        from apps.automations.models import Automation
        return sorted(h.hostname for h in _resolve_hosts(Automation.objects.get(pk=self.auto.pk), None))

    def test_global_automation_with_a_sites_playbook_reaches_that_site(self):
        self.assertEqual(self.names(), ["west-1"])

    def test_another_sites_automation_with_it_reaches_nothing(self):
        scoping.place(self.auto, self.lab.pk)
        self.assertEqual(self.names(), [])
        from apps.automations.engine import _rollout_host_ids
        from apps.automations.models import Automation
        self.assertEqual(_rollout_host_ids(Automation.objects.get(pk=self.auto.pk)), [])


class IncludeByNameBoundTests(TestCase):
    """A task's `type: playbook` include resolves global playbooks, plus the
    one site the dispatch is bounded to; another site's playbook reads as
    missing, so its admins never decide what runs elsewhere."""

    def setUp(self):
        from apps_business.sites.models import Site
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        Playbook.objects.create(name="Shared")
        scoping.place(Playbook.objects.create(name="West only"), self.west.pk)

    def _expand(self, name, site):
        from apps.playbooks.expansion import PlaybookExpandError, expand_actions, including_from
        with including_from(site):
            try:
                expand_actions([{"type": "playbook", "params": {"name": name}}])
                return True
            except PlaybookExpandError as exc:
                return str(exc)

    def test_bounds(self):
        self.assertIs(self._expand("Shared", None), True)
        self.assertIs(self._expand("Shared", self.lab.pk), True)
        self.assertIs(self._expand("West only", self.west.pk), True)
        self.assertEqual(self._expand("West only", self.lab.pk), "no playbook named 'West only'")
        self.assertEqual(self._expand("West only", None), "no playbook named 'West only'")

    def test_a_rollout_of_a_sites_playbook_is_that_sites(self):
        from apps.tasks.models import PatchWave
        from apps.tasks.rollout import start_rollout
        from apps.playbooks.models import PlaybookStep
        from apps.tasks.models import TaskDefinition
        from apps.tasks.spec import parse_and_validate
        check = ("name: Check cron\nactions:\n  - id: c\n    type: check_service\n"
                 "    params:\n      service_name: cron\n")
        definition = TaskDefinition.objects.create(owner=None, name="Check cron", yaml_source=check,
                                                   parsed_spec=parse_and_validate(check))
        PatchWave.objects.create(name="all", order=1, enabled=True)
        pb = Playbook.objects.get(name="West only")
        PlaybookStep.objects.create(playbook=pb, definition=definition, order=0)
        rollout = start_rollout(playbook=pb)
        self.assertEqual(rollout.site_id, self.west.pk)
        with self.assertRaises(ValueError):
            start_rollout(playbook=pb, site_id=self.lab.pk)
