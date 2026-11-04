"""A tag is install-wide, so the delete guard counts every site's use of it.

Host is scoped by default since S1 phase 04b. Counting ``tag.hosts`` in a
West admin's request saw no Lab hosts, and the guard let them delete a tag
that Lab's hosts still carried.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role
from apps.hosts.models import Host, Tag
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole  # noqa: E402


class TagDeleteCountsEverySiteTests(TestCase):
    def setUp(self):
        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=west, role=Role.ADMIN)
        # West's own tag (S1 04h), still on a Lab host from before tags were
        # per site: the guard must count that use too.
        self.tag = Tag.objects.create(name="gpu", key="gpu", site_id=west.pk)
        host = Host.objects.create(hostname="lab-1", agent_token="tok-lab-1")
        HostSiteAssignment.objects.create(host=host, site=lab)
        host.tag_rows.add(self.tag)
        self.client.force_login(self.dana)

    def test_a_tag_only_another_site_uses_is_not_deleted(self):
        r = self.client.delete(f"/api/v1/tags/{self.tag.id}/")
        self.assertEqual(r.status_code, 409, r.content)
        self.assertIn("1 host(s)", r.json()["detail"])
        self.assertTrue(Tag.objects.filter(pk=self.tag.pk).exists())
