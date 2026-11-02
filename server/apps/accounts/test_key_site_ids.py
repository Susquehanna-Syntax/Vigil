"""A scoped API key narrows its creator to the key's sites and never widens them.

Separate module: the guard skips it when the Business sites app is absent.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import visible_site_ids
from apps.hosts.models import Host
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()
from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole  # noqa: E402


class KeySiteIdsTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.north = Site.objects.create(name="North", slug="north")
        users = get_user_model().objects
        self.boss = users.create_user("boss", password="x")
        UserProfile.objects.create(user=self.boss, role=Role.ADMIN)
        self.dana = users.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        UserSiteRole.objects.create(user=self.dana, site=self.lab, role=Role.ADMIN)

    def test_without_a_key_nothing_changes(self):
        self.assertIsNone(visible_site_ids(self.boss))
        self.assertEqual(visible_site_ids(self.dana), {self.west.id, self.lab.id})

    def test_a_key_narrows_an_unscoped_creator_to_its_sites(self):
        self.boss.key_site_ids = {self.lab.id}
        self.assertEqual(visible_site_ids(self.boss), {self.lab.id})

    def test_a_key_narrows_a_superuser_too(self):
        root = get_user_model().objects.create_superuser("root", password="x")
        root.key_site_ids = {self.north.id}
        self.assertEqual(visible_site_ids(root), {self.north.id})

    def test_a_key_never_widens_a_scoped_creator(self):
        self.dana.key_site_ids = {self.lab.id, self.north.id}
        self.assertEqual(visible_site_ids(self.dana), {self.lab.id})

    def test_a_key_for_sites_the_creator_cannot_see_sees_nothing(self):
        self.dana.key_site_ids = {self.north.id}
        self.assertEqual(visible_site_ids(self.dana), set())

    def test_host_lists_follow_the_key(self):
        west = Host.objects.create(hostname="west-host", agent_token="tok-w")
        lab = Host.objects.create(hostname="lab-host", agent_token="tok-l")
        HostSiteAssignment.objects.create(host=west, site=self.west)
        HostSiteAssignment.objects.create(host=lab, site=self.lab)
        self.boss.key_site_ids = {self.lab.id}
        names = set(scoping.filter_by_site(Host.objects.all(), self.boss)
                    .values_list("hostname", flat=True))
        self.assertEqual(names, {"lab-host"})
        self.assertFalse(scoping.host_in_scope(self.boss, west))
        self.assertTrue(scoping.host_in_scope(self.boss, lab))
