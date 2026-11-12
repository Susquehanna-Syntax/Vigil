"""Per-site tags and wave ladders (S1 04h, decisions-06 item 18).

Each tag and wave belongs to one site or is global. Names stay unique across
the install. A site admin manages their own site's; global ones are an
instance admin's; anyone may use a global one; nobody may put another site's
tag on their host.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host, Tag
from apps.tasks.models import PatchWave
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole  # noqa: E402


class _Sites(TestCase):
    def setUp(self):
        User = get_user_model()
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = User.objects.create_user("dana", password="x")
        UserProfile.objects.create(user=self.dana, role=Role.VIEWER)
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.ida = User.objects.create_user("ida", password="x")
        UserProfile.objects.create(user=self.ida, role=Role.ADMIN)
        self.hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab)):
            self.hosts[name] = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                                   status=Host.Status.ONLINE, mode="managed")
            HostSiteAssignment.objects.create(host=self.hosts[name], site=site)
        self.global_tag = Tag.objects.create(name="prod")
        self.lab_tag = Tag.objects.create(name="lab-gpu", site_id=self.lab.pk)
        self.west_tag = Tag.objects.create(name="west-web", site_id=self.west.pk)

    def send(self, user, method, url, body=None):
        self.client.force_login(user)
        if method in ("get", "delete"):
            return getattr(self.client, method)(url)
        return getattr(self.client, method)(url, data=json.dumps(body or {}),
                                            content_type="application/json")


class SiteTagTests(_Sites):
    def test_a_site_admin_makes_tags_in_their_site_only(self):
        made = self.send(self.dana, "post", "/api/v1/tags/", {"name": "west-db", "site_id": str(self.west.pk)})
        self.assertEqual(made.status_code, 201, made.content)
        self.assertEqual(made.json()["site_id"], str(self.west.pk))
        self.assertEqual(self.send(self.dana, "post", "/api/v1/tags/", {"name": "anywhere"}).status_code, 403)
        self.assertEqual(self.send(self.dana, "post", "/api/v1/tags/",
                                   {"name": "sneaky", "site_id": str(self.lab.pk)}).status_code, 404)
        made = self.send(self.ida, "post", "/api/v1/tags/", {"name": "anywhere"})
        self.assertEqual((made.status_code, made.json()["site_id"]), (201, None))

    def test_names_clash_across_sites_without_naming_the_other(self):
        clash = self.send(self.dana, "post", "/api/v1/tags/", {"name": "LAB-GPU", "site_id": str(self.west.pk)})
        self.assertEqual(clash.status_code, 400)
        self.assertNotIn(b"Lab", clash.content.replace(b"LAB-GPU", b""))

    def test_the_list_shows_global_and_own_site_tags(self):
        self.client.force_login(self.dana)
        names = {t["name"] for t in self.client.get("/api/v1/tags/").json()}
        self.assertEqual(names, {"prod", "west-web"})

    def test_the_list_says_which_tags_this_user_may_change(self):
        # UI check 2026-11-05: a site admin was offered Rename and Delete on a
        # global tag, which the server then refused.
        self.client.force_login(self.dana)
        mine = {t["name"]: t["can_change"] for t in self.client.get("/api/v1/tags/").json()}
        self.assertEqual(mine, {"prod": False, "west-web": True})
        self.client.force_login(self.ida)
        theirs = {t["name"]: t["can_change"] for t in self.client.get("/api/v1/tags/").json()}
        self.assertTrue(theirs["prod"] and theirs["lab-gpu"])

    def test_change_rules(self):
        self.assertEqual(self.send(self.dana, "get", f"/api/v1/tags/{self.lab_tag.pk}/").status_code, 404)
        self.assertEqual(self.send(self.dana, "patch", f"/api/v1/tags/{self.lab_tag.pk}/",
                                   {"name": "x"}).status_code, 404)
        self.assertEqual(self.send(self.dana, "patch", f"/api/v1/tags/{self.global_tag.pk}/",
                                   {"name": "production"}).status_code, 403)
        self.assertEqual(self.send(self.dana, "patch", f"/api/v1/tags/{self.west_tag.pk}/",
                                   {"name": "west-www"}).status_code, 200)
        self.assertEqual(self.send(self.dana, "delete", f"/api/v1/tags/{self.global_tag.pk}/").status_code, 403)
        self.assertEqual(self.send(self.ida, "patch", f"/api/v1/tags/{self.global_tag.pk}/",
                                   {"name": "production"}).status_code, 200)

    def test_a_host_takes_global_and_own_site_tags_only(self):
        url = f"/api/v1/hosts/{self.hosts['west-1'].pk}/tags/"
        refused = self.send(self.dana, "patch", url, {"tags": ["prod", "lab-gpu"]})
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn(b"Lab", refused.content.replace(b"lab-gpu", b""))
        ok = self.send(self.dana, "patch", url, {"tags": ["prod", "west-web", "brand-new"]})
        self.assertEqual(ok.status_code, 200, ok.content)
        # A tag first typed on a West host by a West admin is West's.
        self.assertEqual(Tag.objects.get(key="brand-new").site_id, self.west.pk)
        self.assertIsNone(Tag.objects.get(key="prod").site_id)

    def test_a_global_tag_made_in_between_is_not_claimed(self):
        # Security review of 5c04a28: a global tag created after the view
        # looked its names up must stay global, not become West's.
        from unittest import mock

        real = Tag.objects.filter

        def racing_filter(*args, **kwargs):
            if "key__in" in kwargs and not Tag.objects.all().filter(key="raced").exists():
                result = real(*args, **kwargs)
                list(result)                        # the view's lookup sees no "raced" row...
                Tag.objects.create(name="raced")    # ...then an instance admin makes it, global
                return result
            return real(*args, **kwargs)

        url = f"/api/v1/hosts/{self.hosts['west-1'].pk}/tags/"
        with mock.patch.object(Tag.objects, "filter", side_effect=racing_filter):
            self.assertEqual(self.send(self.dana, "patch", url, {"tags": ["raced"]}).status_code, 200)
        self.assertIsNone(Tag.objects.get(key="raced").site_id)

    def test_an_instance_admins_new_tag_stays_global(self):
        url = f"/api/v1/hosts/{self.hosts['west-1'].pk}/tags/"
        self.assertEqual(self.send(self.ida, "patch", url, {"tags": ["fleet-new"]}).status_code, 200)
        self.assertIsNone(Tag.objects.get(key="fleet-new").site_id)


class SiteWaveTests(_Sites):
    def wave(self, name, order, site=None, group="srv"):
        wave = PatchWave.objects.create(name=name, order=order, tags=["prod"], group_tags=[group],
                                        site_id=site.pk if site else None)
        return wave

    def test_a_site_admin_builds_their_own_ladder(self):
        body = {"name": "West canary", "order": 1, "tags": ["prod"], "group_tags": ["srv"]}
        self.assertEqual(self.send(self.dana, "post", "/api/v1/waves/", body).status_code, 403)
        made = self.send(self.dana, "post", "/api/v1/waves/", {**body, "site_id": str(self.west.pk)})
        self.assertEqual(made.status_code, 201, made.content)
        self.assertEqual(PatchWave.objects.get(pk=made.json()["id"]).site_id, self.west.pk)
        # Lab's wave 1 on the same ladder does not collide with West's.
        self.wave("Lab canary", 1, self.lab)
        made = self.send(self.ida, "post", "/api/v1/waves/", {**body, "name": "Global canary"})
        self.assertEqual(made.status_code, 201, made.content)

    def test_visibility_and_change_rules(self):
        lab = self.wave("Lab canary", 1, self.lab)
        glob = self.wave("Global canary", 1)
        west = self.wave("West canary", 1, self.west)
        self.client.force_login(self.dana)
        self.assertEqual({w["name"] for w in self.client.get("/api/v1/waves/").json()},
                         {"Global canary", "West canary"})
        self.assertEqual(self.client.get(f"/api/v1/waves/{lab.pk}/").status_code, 404)
        self.assertEqual(self.send(self.dana, "patch", f"/api/v1/waves/{glob.pk}/",
                                   {"validation_hours": 1}).status_code, 403)
        self.assertEqual(self.send(self.dana, "patch", f"/api/v1/waves/{west.pk}/",
                                   {"validation_hours": 1}).status_code, 200)

    def test_a_sites_rollout_walks_its_own_ladder_or_the_global_one(self):
        from apps.tasks.rollout import _first_enabled_wave, _next_enabled_wave
        g1, g2 = self.wave("G1", 1), self.wave("G2", 2)
        w1 = self.wave("W1", 1, self.west)
        with scoping.system(reason="test"):
            self.assertEqual(_first_enabled_wave("srv", self.west.pk), w1)
            self.assertIsNone(_next_enabled_wave(1, "srv", self.west.pk))  # never steps onto G2
            self.assertEqual(_first_enabled_wave("srv", self.lab.pk), g1)  # Lab has no ladder
            self.assertEqual(_first_enabled_wave("srv"), g1)               # fleet-wide: global only
            self.assertEqual(_next_enabled_wave(1, "srv"), g2)
