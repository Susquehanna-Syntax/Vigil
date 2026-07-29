"""Scope resolution: own + global - suppressed."""
from unittest import mock

from django.test import TestCase

from apps.baselines.models import Baseline
from apps_business.sites.models import BaselineSiteAssignment, Site
from vigil import scoping


class ResourcesForTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.glob = Site.objects.global_site()

        self.everywhere = Baseline.objects.create(name="Nightly patch scan")
        self.west_only = Baseline.objects.create(name="Edge hardening")
        BaselineSiteAssignment.objects.create(baseline=self.west_only, site=self.west)

    def test_site_sees_its_own_and_the_global_one(self):
        names = set(scoping.resources_for(Baseline, self.west)
                    .values_list("name", flat=True))
        self.assertEqual(names, {"Nightly patch scan", "Edge hardening"})

    def test_site_does_not_see_another_sites_resources(self):
        names = set(scoping.resources_for(Baseline, self.lab)
                    .values_list("name", flat=True))
        self.assertEqual(names, {"Nightly patch scan"})

    def test_suppression_hides_a_global_resource_from_one_site_only(self):
        scoping.suppress(self.lab, self.everywhere)

        lab = set(scoping.resources_for(Baseline, self.lab).values_list("name", flat=True))
        west = set(scoping.resources_for(Baseline, self.west).values_list("name", flat=True))

        self.assertEqual(lab, set())
        self.assertIn("Nightly patch scan", west)

    def test_suppression_does_not_delete_the_resource(self):
        scoping.suppress(self.lab, self.everywhere)
        self.everywhere.refresh_from_db()
        self.assertTrue(Baseline.objects.filter(pk=self.everywhere.pk).exists())

    def test_suppress_is_idempotent(self):
        scoping.suppress(self.lab, self.everywhere)
        scoping.suppress(self.lab, self.everywhere)   # must not raise
        scoping.unsuppress(self.lab, self.everywhere)
        self.assertIn("Nightly patch scan",
                      scoping.resources_for(Baseline, self.lab).values_list("name", flat=True))

    def test_unassigned_resource_is_global(self):
        self.assertTrue(scoping.scope_of(self.everywhere).is_global)

    def test_assigned_resource_reports_its_site(self):
        self.assertEqual(scoping.scope_of(self.west_only), self.west)

    def test_a_resource_explicitly_assigned_to_the_global_site_cascades(self):
        """Assigning to Global is the same as leaving it unassigned."""
        explicit = Baseline.objects.create(name="Explicitly global")
        BaselineSiteAssignment.objects.create(baseline=explicit, site=self.glob)
        for site in (self.west, self.lab):
            self.assertIn("Explicitly global",
                          scoping.resources_for(Baseline, site).values_list("name", flat=True))


class LapseBehaviorTests(TestCase):
    """On lapse we stop fixing it for you; we never stop telling you."""

    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.scoped = Baseline.objects.create(name="Edge hardening")
        BaselineSiteAssignment.objects.create(baseline=self.scoped, site=self.west)
        self.global_baseline = Baseline.objects.create(name="Nightly patch scan")

    def test_site_scoped_baseline_pauses_when_unlicensed(self):
        self.assertFalse(scoping.execution_allowed(self.scoped))

    def test_global_baseline_still_runs_when_unlicensed(self):
        self.assertTrue(scoping.execution_allowed(self.global_baseline))

    def test_notification_channels_never_pause(self):
        from apps.alerts.models import NotificationChannel
        ch = NotificationChannel.objects.create(
            name="oncall", kind=NotificationChannel.Kind.WEBHOOK,
            config={"url": "https://example.com/hook"})
        self.assertTrue(
            scoping.execution_allowed(ch),
            "Alert delivery must never be gated by license state (SQSY-LICENSING §6)")

    def test_a_site_scoped_channel_also_never_pauses(self):
        """Even scoped, a channel is delivery — not a licensable behavior."""
        from apps.alerts.models import NotificationChannel
        from apps_business.sites.models import ChannelSiteAssignment
        ch = NotificationChannel.objects.create(
            name="west-oncall", kind=NotificationChannel.Kind.WEBHOOK,
            config={"url": "https://example.com/hook"})
        ChannelSiteAssignment.objects.create(channel=ch, site=self.west)
        self.assertTrue(scoping.execution_allowed(ch))


class NoBusinessAppTests(TestCase):
    """An AGPL-only build has no apps_business.sites. Scoping must then be a
    no-op rather than an error — core never depends on the commercial side."""

    def setUp(self):
        self.baseline = Baseline.objects.create(name="Nightly patch scan")
        self.site = Site.objects.create(name="West Campus", slug="west-campus")

    def test_everything_is_visible_and_nothing_raises(self):
        with mock.patch.object(scoping, "_sites_models", return_value=None):
            self.assertEqual(
                list(scoping.resources_for(Baseline, self.site)), [self.baseline])
            self.assertIsNone(scoping.scope_of(self.baseline))
            scoping.suppress(self.site, self.baseline)      # no-op, must not raise
            scoping.unsuppress(self.site, self.baseline)    # no-op, must not raise
            self.assertEqual(
                list(scoping.resources_for(Baseline, self.site)), [self.baseline])

    def test_execution_is_never_blocked_without_the_business_app(self):
        with mock.patch.object(scoping, "_sites_models", return_value=None):
            self.assertTrue(scoping.execution_allowed(self.baseline))
