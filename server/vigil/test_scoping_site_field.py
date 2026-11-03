"""filter_by_site_field: scoping rows that carry a plain site_id column.

UpdatePolicy and StatusPage record their site as a UUID column rather than a
SiteAssignment row, so they need their own helper. These tests pin its four
branches and the API leak the helper closes.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role
from apps.hosts.models import Host
from apps.policies.models import PolicyChange, UpdatePolicy
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()
from apps_business.sites.models import (
    HostSiteAssignment,
    Site,
    UserSiteRole,
)


def _user(username, **extra):
    return get_user_model().objects.create_user(
        username=username, password="x", **extra)


class FilterBySiteFieldTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.everywhere = UpdatePolicy.objects.create(name="zzeverywhere")
        UpdatePolicy.objects.create(name="zzzwest", site_id=self.west.id)
        UpdatePolicy.objects.create(name="zzlab", site_id=self.lab.id)
        self.dana = _user("dana@w.io", is_staff=True)
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)

    def _names(self, user):
        return set(scoping.filter_by_site_field(
            UpdatePolicy.objects.all(), user).values_list("name", flat=True))

    def test_unscoped_user_sees_every_row(self):
        carol = _user("carol@w.io", is_staff=True)
        self.assertEqual(self._names(carol), {"zzeverywhere", "zzzwest", "zzlab"})

    def test_scoped_user_sees_own_and_unrestricted(self):
        self.assertEqual(self._names(self.dana), {"zzeverywhere", "zzzwest"})

    def test_global_site_id_counts_as_unrestricted(self):
        glob = Site.objects.global_site()
        UpdatePolicy.objects.create(name="zzglobal", site_id=glob.id)
        self.assertEqual(self._names(self.dana),
                         {"zzeverywhere", "zzzwest", "zzglobal"})

    def test_scoped_to_nothing_sees_nothing(self):
        with mock.patch("apps.accounts.permissions.visible_site_ids",
                        return_value=set()):
            self.assertEqual(list(scoping.filter_by_site_field(
                UpdatePolicy.objects.all(), self.dana)), [])


class ChangeDecisionScopeTests(TestCase):
    def setUp(self):
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = _user("dana@w.io", is_staff=True)
        UserSiteRole.objects.create(
            user=self.dana,
            site=Site.objects.create(name="West Campus", slug="west-campus"),
            role=Role.ADMIN)

        host = Host.objects.create(hostname="lab-01", agent_token="t-lab")
        HostSiteAssignment.objects.create(host=host, site=self.lab)
        self.change = PolicyChange.objects.create(
            policy=UpdatePolicy.objects.create(name="zzlab-policy"),
            host=host, changes=[{"app": "x"}])

    def test_out_of_site_change_cannot_be_approved(self):
        api = APIClient()
        api.force_authenticate(self.dana)
        resp = api.post(
            "/api/v1/policies/changes/reject/",
            {"ids": [str(self.change.pk)]}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.change.refresh_from_db()
        self.assertEqual(self.change.state, PolicyChange.State.PENDING)
