"""The header site switcher (S1 04g, decisions-06 items 9 and 18).

Someone in more than one site ticks the sites they want to look at, and every
page narrows to them. The selection is a view, not a permission: it only ever
narrows, it never changes what a user may do, and it applies to browser
sessions only.
"""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import visible_site_ids
from apps.hosts.models import Host
from vigil import scoping
from vigil import test_site_isolation as walk
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole  # noqa: E402

URL = "/api/v1/scope/selection/"


class SiteSwitcherTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.glob = Site.objects.get(is_global=True)
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = User.objects.create_user("dana", password="x")
        UserProfile.objects.create(user=self.dana, role=Role.VIEWER)
        for site in (self.west, self.lab):
            UserSiteRole.objects.create(user=self.dana, site=site, role=Role.ADMIN)
        self.wes = User.objects.create_user("wes", password="x")
        UserProfile.objects.create(user=self.wes, role=Role.VIEWER)
        UserSiteRole.objects.create(user=self.wes, site=self.west, role=Role.ADMIN)
        self.ida = User.objects.create_user("ida", password="x")
        UserProfile.objects.create(user=self.ida, role=Role.ADMIN)
        self.hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab), ("glob-1", None)):
            host = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                       status=Host.Status.ONLINE, mode="managed")
            if site is not None:
                HostSiteAssignment.objects.create(host=host, site=site)
            self.hosts[name] = host

    def tick(self, user, *sites, expect=200):
        self.client.force_login(user)
        response = self.client.put(URL, {"selected": [str(s.pk) for s in sites]},
                                   content_type="application/json")
        self.assertEqual(response.status_code, expect, response.content)
        return response

    def hostnames(self, user):
        self.client.force_login(user)
        return sorted(h["hostname"] for h in self.client.get("/api/v1/hosts/").json())

    def test_ticking_one_site_narrows_every_page_to_it(self):
        self.assertEqual(self.hostnames(self.dana), ["lab-1", "west-1"])
        self.tick(self.dana, self.west)
        self.assertEqual(self.hostnames(self.dana), ["west-1"])
        lab = self.hosts["lab-1"].pk
        self.assertEqual(self.client.get(f"/api/v1/hosts/{lab}/").status_code, 404)
        self.tick(self.dana, self.west, self.lab)
        self.assertEqual(self.hostnames(self.dana), ["lab-1", "west-1"])
        # Every site ticked is stored as "all", so a new site shows up too.
        self.assertEqual(UserProfile.objects.get(user=self.dana).site_selection, [])

    def test_get_lists_the_sites_and_the_ticks(self):
        self.tick(self.dana, self.lab)
        self.client.force_login(self.dana)
        body = self.client.get(URL).json()
        self.assertTrue(body["enabled"])
        self.assertEqual([s["name"] for s in body["sites"]], ["Lab", "West"])
        self.assertEqual(body["selected"], [str(self.lab.pk)])

    def test_one_site_users_get_no_switcher(self):
        self.client.force_login(self.wes)
        body = self.client.get(URL).json()
        self.assertFalse(body["enabled"])
        self.assertEqual([s["name"] for s in body["sites"]], ["West"])

    def test_a_forged_selection_is_refused_and_a_stored_one_ignored(self):
        self.tick(self.wes, self.lab, expect=400)
        UserProfile.objects.filter(user=self.wes).update(site_selection=[str(self.lab.pk), "junk"])
        # Lab is not hers, so nothing is left to narrow to and the tick is ignored.
        self.assertEqual(self.hostnames(self.wes), ["west-1"])

    def test_an_empty_selection_is_refused_and_all_clears_it(self):
        self.tick(self.dana, expect=400)
        self.tick(self.dana, self.west)
        self.client.force_login(self.dana)
        self.client.put(URL, {"all": True}, content_type="application/json")
        self.assertEqual(self.hostnames(self.dana), ["lab-1", "west-1"])

    def test_an_instance_admin_narrows_the_view_and_keeps_their_authority(self):
        self.assertEqual(self.hostnames(self.ida), ["glob-1", "lab-1", "west-1"])
        self.tick(self.ida, self.west)
        self.assertEqual(self.hostnames(self.ida), ["west-1"])
        self.tick(self.ida, self.glob, self.west)
        self.assertEqual(self.hostnames(self.ida), ["glob-1", "west-1"])
        # What they may do is unchanged: the switcher is a view.
        request = RequestFactory().get("/")
        request.user = self.ida
        with scoping.request_scope(request):
            self.assertIsNone(visible_site_ids(self.ida))
            self.assertTrue(scoping.is_instance_admin(self.ida))

    def test_it_applies_to_browser_sessions_only(self):
        UserProfile.objects.filter(user=self.dana).update(site_selection=[str(self.west.pk)])
        keyed = get_user_model().objects.get(pk=self.dana.pk)
        keyed.key_site_ids = {self.west.pk, self.lab.pk}
        request = RequestFactory().get("/")
        request.user = keyed
        with scoping.request_scope(request):
            self.assertEqual(sorted(Host.objects.values_list("hostname", flat=True)), ["lab-1", "west-1"])
        # Jobs that act as the user (reports, as_user) see their whole reach.
        with scoping.as_user(self.dana):
            self.assertEqual(sorted(Host.objects.values_list("hostname", flat=True)), ["lab-1", "west-1"])
        request.user = get_user_model().objects.get(pk=self.dana.pk)  # as a request loads it
        with scoping.request_scope(request):
            self.assertEqual(list(Host.objects.values_list("hostname", flat=True)), ["west-1"])

    def test_a_scoped_api_key_cannot_set_it(self):
        self.client.force_login(self.dana)
        keyed = get_user_model().objects.get(pk=self.dana.pk)
        keyed.key_site_ids = {self.west.pk}
        from rest_framework.test import APIRequestFactory, force_authenticate

        from vigil.views_scope import site_selection
        request = APIRequestFactory().put(URL, {"all": True}, format="json")
        force_authenticate(request, user=keyed)
        self.assertEqual(site_selection(request).status_code, 403)

    def test_create_forms_offer_the_ticked_sites_first(self):
        self.tick(self.dana, self.west)
        self.client.force_login(self.dana)
        sites = self.client.get("/api/v1/scope/writable-sites/").json()["sites"]
        self.assertEqual([s["name"] for s in sites], ["West", "Lab"])

    def test_52_responses_match_the_api_schema(self):
        from vigil.api_schema import (
            VigilSiteSelectionSerializer,
            VigilWritableSitesSerializer,
        )
        from vigil.schema_check import assert_shape

        self.tick(self.dana, self.lab)
        self.client.force_login(self.dana)
        chosen = self.client.get(URL)
        self.assertEqual(chosen.status_code, 200, chosen.content)
        assert_shape(self, chosen.json(), VigilSiteSelectionSerializer)
        # The list is never empty for a user with sites: an empty one would
        # leave the item shape unchecked.
        self.assertTrue(chosen.json()["sites"])

        cleared = self.client.put(URL, {"all": True}, content_type="application/json")
        self.assertEqual(cleared.status_code, 200, cleared.content)
        assert_shape(self, cleared.json(), VigilSiteSelectionSerializer)

        # Ida is an instance admin: "no site" is one of her choices.
        self.client.force_login(self.ida)
        offered = self.client.get("/api/v1/scope/writable-sites/")
        self.assertEqual(offered.status_code, 200, offered.content)
        assert_shape(self, offered.json(), VigilWritableSitesSerializer)
        self.assertTrue(offered.json()["sites"])


class SelectionIsolationWalkTests(walk.SiteIsolationTests):
    """The isolation walk, with dana an admin of both sites who ticked only
    West: no page may show her Lab rows. (The walk's other tests run on the
    base class; only the walk and its positive control repeat here.)"""

    test_every_route_is_classified = None
    test_the_oracle_catches_a_leak = None

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        UserSiteRole.objects.create(user=cls.dana, site=cls.lab, role=Role.ADMIN)
        UserProfile.objects.update_or_create(
            user=cls.dana, defaults={"site_selection": [str(cls.west.pk)]})
        assert cls.objs[walk.MINE]["site"] == cls.west
