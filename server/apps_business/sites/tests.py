"""Sites tests — licensed and unlicensed behavior per SQSY-LICENSING.md §5/§6."""

import base64
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.baselines.models import Baseline
from apps.hosts.models import Host
from vigil import licensing

from .models import (
    AutomationSiteAssignment, BaselineSiteAssignment, ChannelSiteAssignment,
    GlobalSuppression, HostSiteAssignment, Site, SiteCapability, UserSiteRole,
)

SK = SigningKey.generate()
PUB = base64.b64encode(SK.verify_key.encode()).decode()


def make_blob(*, exp_delta=86400 * 90, seats=4):
    claims = {
        "instance": licensing.instance_id(),
        "org": "test-org",
        "seats": seats,
        "sites": None,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    sig = SK.sign(payload).signature

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(sig)}"


def make_host(hostname="h1"):
    return Host.objects.create(hostname=hostname, agent_token=f"tok-{hostname}-{uuid.uuid4()}")


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class SitesApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x", is_staff=True)
        self.client.force_login(self.user)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def license_up(self, **kw):
        licensing.set_license(make_blob(**kw))

    # -- migration ----------------------------------------------------------

    def test_global_site_exists_after_migrate(self):
        self.assertTrue(Site.objects.filter(is_global=True, slug="global").exists())

    # -- unlicensed ---------------------------------------------------------

    def test_list_is_readable_without_license(self):
        resp = self.client.get("/api/v1/sites/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Global", [s["name"] for s in resp.json()])

    def test_create_without_license_is_402_with_upgrade_body(self):
        resp = self.client.post("/api/v1/sites/", {"name": "Branch", "slug": "branch"})
        self.assertEqual(resp.status_code, 402)
        body = resp.json()
        self.assertEqual(body["feature"], "sites")
        self.assertIn("upgrade_url", body)
        # Nothing was created. Asserted by name rather than by total count:
        # the global site is a structural row, not a user site.
        self.assertFalse(Site.objects.filter(slug="branch").exists())

    def test_license_lapse_freezes_writes_not_reads(self):
        self.license_up(exp_delta=-30 * 86400)  # past grace
        self.assertEqual(self.client.get("/api/v1/sites/").status_code, 200)
        resp = self.client.post("/api/v1/sites/", {"name": "X", "slug": "x"})
        self.assertEqual(resp.status_code, 402)

    # -- licensed -----------------------------------------------------------

    def test_create_update_delete_with_license(self):
        self.license_up()
        resp = self.client.post("/api/v1/sites/", {"name": "Branch", "slug": "branch"})
        self.assertEqual(resp.status_code, 201, resp.content)
        sid = resp.json()["id"]
        resp = self.client.patch(f"/api/v1/sites/{sid}/", {"name": "Branch 2"},
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Branch 2")
        self.assertEqual(self.client.delete(f"/api/v1/sites/{sid}/").status_code, 204)
        self.assertFalse(Site.objects.filter(pk=sid).exists())

    def test_cannot_delete_global_site(self):
        self.license_up()
        glob = Site.objects.get(is_global=True)
        resp = self.client.delete(f"/api/v1/sites/{glob.pk}/")
        self.assertEqual(resp.status_code, 400)

    def test_unassigned_hosts_count_toward_global_site(self):
        make_host("a")
        make_host("b")
        resp = self.client.get("/api/v1/sites/")
        glob = next(s for s in resp.json() if s["is_global"])
        self.assertEqual(glob["host_count"], 2)

    def test_assign_and_unassign_host(self):
        self.license_up()
        site = Site.objects.create(name="Branch", slug="branch")
        host = make_host("c")
        resp = self.client.put(f"/api/v1/sites/{site.pk}/hosts/{host.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(HostSiteAssignment.objects.get(host=host).site, site)
        resp = self.client.delete(f"/api/v1/sites/{site.pk}/hosts/{host.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(HostSiteAssignment.objects.filter(host=host).exists())

    def test_delete_site_moves_hosts_to_global(self):
        self.license_up()
        site = Site.objects.create(name="Branch", slug="branch")
        host = make_host("d")
        HostSiteAssignment.objects.create(host=host, site=site)
        self.client.delete(f"/api/v1/sites/{site.pk}/")
        self.assertFalse(HostSiteAssignment.objects.filter(host=host).exists())
        resp = self.client.get("/api/v1/sites/")
        glob = next(s for s in resp.json() if s["is_global"])
        self.assertEqual(glob["host_count"], 1)


class GlobalSiteTests(TestCase):
    def test_exactly_one_global_site_exists_after_migrate(self):
        self.assertEqual(Site.objects.filter(is_global=True).count(), 1)

    def test_global_site_accessor_returns_it(self):
        self.assertTrue(Site.objects.global_site().is_global)

    def test_there_is_no_separate_default_site(self):
        """One concept, one row: the global site holds unassigned hosts."""
        self.assertEqual(Site.objects.count(), 1)
        self.assertFalse(hasattr(Site.objects.global_site(), "is_default"))

    def test_the_global_site_is_named_global(self):
        g = Site.objects.global_site()
        self.assertEqual((g.name, g.slug), ("Global", "global"))

    def test_global_site_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            Site.objects.global_site().delete()

    def test_unassigned_hosts_count_toward_the_global_site(self):
        make_host("a")
        make_host("b")
        user = get_user_model().objects.create_user("z", password="x", is_staff=True)
        self.client.force_login(user)
        rows = self.client.get("/api/v1/sites/").json()
        glob = next(s for s in rows if s["is_global"])
        self.assertEqual(glob["host_count"], 2)

    def test_deleting_the_global_site_over_the_api_is_a_400_not_a_500(self):
        user = get_user_model().objects.create_user("root", password="x", is_staff=True)
        self.client.force_login(user)
        with override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB):
            licensing.set_license(make_blob())
            resp = self.client.delete(f"/api/v1/sites/{Site.objects.global_site().id}/")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(Site.objects.filter(is_global=True).exists())


class ScopeAssignmentTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="West Campus", slug="west-campus")
        self.baseline = Baseline.objects.create(name="Edge hardening")

    def test_a_baseline_belongs_to_exactly_one_site(self):
        BaselineSiteAssignment.objects.create(baseline=self.baseline, site=self.site)
        other = Site.objects.create(name="Lab", slug="lab")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            BaselineSiteAssignment.objects.create(baseline=self.baseline, site=other)

    def test_deleting_the_baseline_removes_its_assignment(self):
        BaselineSiteAssignment.objects.create(baseline=self.baseline, site=self.site)
        self.baseline.delete()
        self.assertEqual(BaselineSiteAssignment.objects.count(), 0)

    def test_deleting_the_site_removes_its_assignments_but_not_the_baseline(self):
        BaselineSiteAssignment.objects.create(baseline=self.baseline, site=self.site)
        self.site.delete()
        self.assertEqual(BaselineSiteAssignment.objects.count(), 0)
        self.assertTrue(Baseline.objects.filter(pk=self.baseline.pk).exists())

    def test_suppression_is_unique_per_site_and_object(self):
        ct = ContentType.objects.get_for_model(Baseline)
        GlobalSuppression.objects.create(
            site=self.site, content_type=ct, object_id=self.baseline.id)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            GlobalSuppression.objects.create(
                site=self.site, content_type=ct, object_id=self.baseline.id)

    def test_a_user_has_one_role_per_site(self):
        user = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=user, site=self.site, role="operator")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            UserSiteRole.objects.create(user=user, site=self.site, role="viewer")


class GlobalSiteApiShapeTests(TestCase):
    """A client must be able to tell the cascading scope from a real site."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("op3", password="x", is_staff=True)
        self.client.force_login(self.user)

    def test_list_marks_the_global_site(self):
        Site.objects.create(name="West Campus", slug="west-campus")
        rows = {s["name"]: s for s in self.client.get("/api/v1/sites/").json()}
        self.assertTrue(rows["Global"]["is_global"])
        self.assertFalse(rows["West Campus"]["is_global"])

    def test_is_global_cannot_be_set_over_the_api(self):
        with override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB):
            licensing.set_license(make_blob())
            resp = self.client.post(
                "/api/v1/sites/", {"name": "Sneaky", "slug": "sneaky", "is_global": True})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(Site.objects.get(slug="sneaky").is_global)
        self.assertEqual(Site.objects.filter(is_global=True).count(), 1)


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class UserSiteRoleApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "boss", password="x", is_staff=True)
        self.dana = get_user_model().objects.create_user("dana", password="x")
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.client.force_login(self.admin)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def _grant(self, **over):
        payload = {"user": self.dana.id, "site": str(self.west.id),
                   "role": "operator",
                   "capabilities": [["baselines", "run"], ["alerts", "ack"]]}
        payload.update(over)
        return self.client.post("/api/v1/sites/roles/", data=json.dumps(payload),
                                content_type="application/json")

    def test_granting_requires_a_license(self):
        resp = self._grant()
        self.assertEqual(resp.status_code, 402, resp.content)
        self.assertEqual(resp.json()["feature"], "rbac_advanced")

    def test_grant_with_license_stores_capabilities(self):
        licensing.set_license(make_blob())
        resp = self._grant()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(
            resp.json()["capabilities"], [["alerts", "ack"], ["baselines", "run"]])

    def test_unknown_capability_is_rejected(self):
        licensing.set_license(make_blob())
        resp = self._grant(capabilities=[["hosts", "teleport"]])
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_unknown_role_is_rejected(self):
        licensing.set_license(make_blob())
        self.assertEqual(self._grant(role="wizard").status_code, 400)

    def test_reading_grants_is_never_gated(self):
        """Reading and resolving must survive a lapse — revoking access on
        lapse would be an outage, not a downgrade."""
        licensing.set_license(make_blob())
        self._grant()
        licensing.set_license(make_blob(exp_delta=-30 * 86400))
        resp = self.client.get(f"/api/v1/sites/roles/?user={self.dana.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_an_existing_grant_still_resolves_after_lapse(self):
        from apps.accounts.permissions import can
        licensing.set_license(make_blob())
        self._grant()
        licensing.set_license(make_blob(exp_delta=-30 * 86400))
        self.assertTrue(can(self.dana, self.west, "baselines", "run"))
        self.assertFalse(can(self.dana, self.west, "hosts", "approve"))

    def test_revoking_is_not_gated(self):
        licensing.set_license(make_blob())
        rid = self._grant().json()["id"]
        licensing.set_license(make_blob(exp_delta=-30 * 86400))
        self.assertEqual(
            self.client.delete(f"/api/v1/sites/roles/{rid}/").status_code, 204)

    def test_revoking_cascades_capabilities(self):
        licensing.set_license(make_blob())
        rid = self._grant().json()["id"]
        self.client.delete(f"/api/v1/sites/roles/{rid}/")
        self.assertEqual(SiteCapability.objects.count(), 0)

    def test_non_admin_cannot_grant(self):
        licensing.set_license(make_blob())
        self.client.force_login(self.dana)
        self.assertEqual(self._grant().status_code, 403)
