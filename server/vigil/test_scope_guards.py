"""Install-wide guards stay install-wide once their models are scoped (S1 04c).

A uniqueness check or a licence count that runs through a scoped manager sees
only the caller's sites. These pin the ones that must count every site.
"""
import base64
import json
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.accounts.models import Role
from apps.policies.models import UpdatePolicy
from apps.statuspage.models import StatusPage
from vigil import licensing
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import Site, UserSiteRole  # noqa: E402

_KEY = SigningKey.generate()


def _license():
    claims = {"instance": licensing.instance_id(), "org": "t", "seats": 5, "sites": None,
              "exp": int(time.time()) + 86400, "iat": int(time.time()),
              "features": ["sites", "rbac_advanced"]}  # no status_branding
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(_KEY.sign(payload).signature)}"


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=base64.b64encode(_KEY.verify_key.encode()).decode())
class InstallWideGuardTests(TestCase):
    def setUp(self):
        licensing.reload()
        licensing.set_license(_license())
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.client.force_login(self.dana)

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def test_policy_name_taken_on_another_site_is_a_400_not_a_500(self):
        UpdatePolicy.objects.create(name="Nightly", site_id=self.lab.pk)
        self.client.raise_request_exception = False
        resp = self.client.post("/api/v1/policies/",
                                data=json.dumps({"name": "Nightly", "site_id": str(self.west.pk)}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("already exists", resp.json()["detail"])

    def test_free_status_page_limit_counts_every_site(self):
        StatusPage.objects.create(site_id=self.lab.pk)
        resp = self.client.post("/api/v1/status-pages/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 402, resp.content)
        self.assertEqual(StatusPage.objects.unscoped(reason="test").count(), 1)


class PlaybookAndTagGuardTests(TestCase):
    """Playbook names are referenced by name (playbook includes), so they
    stay unique across sites; a tag rename rewrites every site's copies."""

    def setUp(self):
        from apps.hosts.models import Tag
        from apps.playbooks.models import Playbook
        from apps_business.sites.models import PlaybookSiteAssignment

        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        root = get_user_model().objects.create_user("root", password="x", is_staff=True)
        self.lab_pb = Playbook.objects.create(name="Harden", created_by=root, target_tags=["web"])
        PlaybookSiteAssignment.objects.create(playbook=self.lab_pb, site=self.lab)
        self.tag, _ = Tag.objects.get_or_create(key="web", defaults={"name": "web"})
        self.dana = get_user_model().objects.create_user("dana", password="x", is_staff=True)
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.client.force_login(self.dana)

    def test_a_playbook_name_another_site_uses_is_taken(self):
        resp = self.client.post("/api/v1/playbooks/", data=json.dumps({"name": "harden"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("already exists", resp.json()["detail"])

    def test_tag_rename_rewrites_every_sites_playbooks(self):
        resp = self.client.patch(f"/api/v1/tags/{self.tag.id}/", data=json.dumps({"name": "frontend"}),
                                 content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.lab_pb.refresh_from_db()
        self.assertEqual(self.lab_pb.target_tags, ["frontend"])


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=base64.b64encode(_KEY.verify_key.encode()).decode())
class PolicyWriteRuleTests(TestCase):
    """decisions-06 items 1-4 on update policies, as a West-only admin (dana),
    a Global viewer who administers West (gwen), and an instance admin (ida)."""

    def setUp(self):
        from apps.accounts.models import UserProfile
        licensing.reload()
        licensing.set_license(_license())
        User = get_user_model()
        self.glob = Site.objects.get(is_global=True)
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = User.objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.gwen = User.objects.create_user("gwen", password="x")
        UserSiteRole.objects.create(user=self.gwen, site=self.west, role=Role.ADMIN)
        UserSiteRole.objects.create(user=self.gwen, site=self.glob, role=Role.VIEWER)
        self.ida = User.objects.create_user("ida", password="x")
        UserProfile.objects.create(user=self.ida, role=Role.ADMIN)
        self.global_policy = UpdatePolicy.objects.create(name="Global nightly")
        self.lab_policy = UpdatePolicy.objects.create(name="Lab nightly", site_id=self.lab.pk)
        self.west_policy = UpdatePolicy.objects.create(name="West nightly", site_id=self.west.pk)

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def _send(self, user, method, url, body=None):
        self.client.force_login(user)
        return getattr(self.client, method)(url, data=json.dumps(body or {}),
                                            content_type="application/json")

    def test_create(self):
        post = lambda user, **body: self._send(user, "post", "/api/v1/policies/",
                                               {"name": f"p-{user}-{len(body)}-{body}", **body}).status_code
        self.assertEqual(post(self.dana, site_id=str(self.lab.pk)), 404)
        self.assertEqual(post(self.dana), 403)
        self.assertEqual(post(self.dana, site_id=str(self.glob.pk)), 403)
        self.assertEqual(post(self.gwen), 403)
        self.assertEqual(post(self.dana, site_id=str(self.west.pk)), 201)
        self.assertEqual(post(self.ida), 201)
        self.assertEqual(post(self.ida, site_id=str(self.lab.pk)), 201)

    def test_edit_and_delete(self):
        url = lambda p: f"/api/v1/policies/{p.pk}/"
        for user in (self.dana, self.gwen):
            self.assertEqual(self._send(user, "put", url(self.global_policy), {"enabled": False}).status_code, 403)
            self.assertEqual(self._send(user, "delete", url(self.global_policy)).status_code, 403)
        self.assertEqual(self._send(self.dana, "put", url(self.lab_policy), {"enabled": False}).status_code, 404)
        # Moving West's policy to Lab, or making it global, is refused.
        self.assertEqual(self._send(self.dana, "put", url(self.west_policy),
                                    {"site_id": str(self.lab.pk)}).status_code, 404)
        self.assertEqual(self._send(self.dana, "put", url(self.west_policy), {"site_id": None}).status_code, 403)
        self.assertEqual(self._send(self.dana, "put", url(self.west_policy), {"enabled": False}).status_code, 200)
        self.assertEqual(self._send(self.ida, "put", url(self.global_policy), {"enabled": False}).status_code, 200)
        self.global_policy.refresh_from_db()
        self.west_policy.refresh_from_db()
        self.assertEqual((self.global_policy.enabled, self.west_policy.site_id), (False, self.west.pk))

    def _drifted(self):
        from apps.hosts.models import Host
        from apps_business.sites.models import HostSiteAssignment
        hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab)):
            hosts[name] = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                              status=Host.Status.ONLINE, mode="managed")
            HostSiteAssignment.objects.create(host=hosts[name], site=site)
        drift = {"hosts": [{"host_id": h.pk} for h in hosts.values()], "compliant": 0, "unknown": []}
        return hosts, drift

    def test_run_now_narrows_to_hosts_the_runner_may_run_on(self):
        from unittest import mock
        hosts, drift = self._drifted()
        sent = {}

        def dispatch(policy, definition, targets, **kw):
            sent["hosts"] = sorted(h.hostname for h in targets)

        with mock.patch("apps.policies.run.policy_drift", return_value=drift), \
                mock.patch("apps.policies.run.compile_policy", return_value=mock.Mock(risk_level="low")), \
                mock.patch("apps.policies.run.dispatch_to_hosts", side_effect=dispatch), \
                mock.patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            resp = self._send(self.gwen, "post", f"/api/v1/policies/{self.global_policy.pk}/run/",
                              {"totp": "123456"})
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertEqual(sent["hosts"], ["west-1"])
            # Nothing of theirs drifted: a 400 that says so, not a quiet no-op.
            drift["hosts"] = [{"host_id": hosts["lab-1"].pk}]
            sent.clear()
            resp = self._send(self.gwen, "post", f"/api/v1/policies/{self.global_policy.pk}/run/",
                              {"totp": "123456"})
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(sent, {})


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=base64.b64encode(_KEY.verify_key.encode()).decode())
class StatusPageWriteRuleTests(TestCase):
    """A status page is public: what it lists is published. A site admin's page
    can list only their own site's hosts, and global pages are not theirs."""

    def setUp(self):
        from apps.hosts.models import Host
        from apps_business.sites.models import HostSiteAssignment
        licensing.reload()
        claims_features = ["sites", "rbac_advanced", "status_branding"]
        licensing.set_license(_license_with(claims_features))
        self.west = Site.objects.create(name="West", slug="west")
        self.lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab)):
            self.hosts[name] = Host.objects.create(hostname=name, agent_token=f"t-{name}",
                                                   status=Host.Status.ONLINE)
            HostSiteAssignment.objects.create(host=self.hosts[name], site=site)
        self.client.force_login(self.dana)

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def _send(self, method, url, body):
        return getattr(self.client, method)(url, data=json.dumps(body), content_type="application/json")

    def test_a_west_page_cannot_list_a_lab_host(self):
        resp = self._send("post", "/api/v1/status-pages/",
                          {"site_id": str(self.west.pk), "host_ids": [str(self.hosts["lab-1"].pk)]})
        self.assertEqual(resp.status_code, 400, resp.content)
        resp = self._send("post", "/api/v1/status-pages/",
                          {"site_id": str(self.west.pk), "host_ids": [str(self.hosts["west-1"].pk)]})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_public_page_never_shows_another_sites_host(self):
        # A row written before 04e (or by hand) naming a Lab host on West's page.
        page = StatusPage.objects.create(site_id=self.west.pk, enabled=True,
                                         host_ids=[str(self.hosts["lab-1"].pk), str(self.hosts["west-1"].pk)])
        self.assertEqual([h.hostname for h in page.hosts()], ["west-1"])

    def test_global_page_and_create_rules(self):
        page = StatusPage.objects.create(title="all")
        self.assertEqual(self._send("patch", f"/api/v1/status-pages/{page.pk}/", {"title": "x"}).status_code, 403)
        self.assertEqual(self._send("post", "/api/v1/status-pages/", {"title": "g"}).status_code, 403)
        self.assertEqual(self._send("post", "/api/v1/status-pages/",
                                    {"site_id": str(self.lab.pk)}).status_code, 404)


def _license_with(features):
    claims = {"instance": licensing.instance_id(), "org": "t", "seats": 5, "sites": None,
              "exp": int(time.time()) + 86400, "iat": int(time.time()), "features": features}
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(_KEY.sign(payload).signature)}"
