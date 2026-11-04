"""Install-wide settings for site admins (S1 04h, decisions-06 item 11).

A site admin passes IsAdmin, because an unscoped role question answers with
their strongest role anywhere. Before 04h that let a West-only admin change
SMTP, AI keys, scanner credentials, the SSO key and everyone's install role.
Now each settings page is full, greyed (read, change nothing) or hidden for
them, on the server, from one map; instance admins keep everything.
"""
import inspect
import json
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import SETTINGS_ACCESS
from vigil import licensing
from vigil import test_site_isolation as walk
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import Site, UserSiteRole  # noqa: E402

#: page -> (a read, a write) a site admin's answer is checked against. None:
#: the page has no endpoint of that kind.
PROBES = {
    "account": (("get", "/api/v1/accounts/totp/", None), None),
    "agents": (("get", "/api/v1/agent-profiles/", None), None),
    "api": (("get", "/api/v1/webhooks/", None), None),
    "users": (("get", "/api/v1/accounts/users/", None),
              ("patch", "/api/v1/accounts/users/{other}/role/", {"role": "admin"})),
    "sites": (("get", "/api/v1/sites/", None), ("post", "/api/v1/sites/", {"name": "Nope"})),
    "branding": (("get", "/api/v1/branding/", None), ("patch", "/api/v1/branding/", {"product_name": "X"})),
    "display": (("get", "/api/v1/instance-settings/", None),
                ("post", "/api/v1/instance-settings/", {"values": {"VIGIL_TIME_FORMAT": "24h"}})),
    "retention": (("get", "/api/v1/instance-settings/", None),
                  ("post", "/api/v1/instance-settings/", {"values": {"VIGIL_ALERT_RETENTION_DAYS": 30}})),
    "relays": (("get", "/api/v1/relays/", None), ("post", "/api/v1/relays/", {"name": "r", "url": "https://r.example"})),
    "distribution": (None, ("post", "/agent/upload/linux/", {})),
    "images": (("get", "/api/v1/reprovision/images/", None), ("post", "/api/v1/reprovision/images/pull/", {})),
    "license": (("get", "/api/v1/license/", None), ("post", "/api/v1/license/", {"license": "x"})),
    "email": (None, ("post", "/api/v1/instance-settings/test/", {"kind": "email"})),
    "ai": (("get", "/api/v1/ai/providers/", None), None),
    "identity": (("get", "/api/v1/civil/settings/", None), None),
    "directory": (("get", "/api/v1/hosts/ad/", None), None),
    "scanners": (None, ("post", "/api/v1/instance-settings/", {"values": {"NESSUS_URL": "https://n.example"}})),
    "jackil": (("get", "/api/v1/jackil/integration-keys/", None), None),
}


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=walk.SiteIsolationTests.__dict__.get(
    "_override_key", None) or __import__("base64").b64encode(
        walk._LICENSE_KEY.verify_key.encode()).decode())
class SettingsSplitTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.west = Site.objects.create(name="West", slug="west")
        self.dana = User.objects.create_user("dana", password="x")
        UserProfile.objects.create(user=self.dana, role=Role.VIEWER)
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.ida = User.objects.create_user("ida", password="x")
        UserProfile.objects.create(user=self.ida, role=Role.ADMIN)
        self.other = User.objects.create_user("otto", password="x")
        UserProfile.objects.create(user=self.other, role=Role.VIEWER)
        licensing.reload()
        licensing.set_license(walk._business_license())
        self.client.raise_request_exception = False

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def call(self, user, probe):
        method, url, body = probe
        self.client.force_login(user)
        url = url.format(other=self.other.pk)
        if body is None:
            return getattr(self.client, method)(url)
        return getattr(self.client, method)(url, data=json.dumps(body), content_type="application/json")

    def test_the_map_covers_the_table(self):
        self.assertEqual(sorted(SETTINGS_ACCESS), sorted(PROBES))
        self.client.force_login(self.dana)
        self.assertEqual(self.client.get("/api/v1/accounts/settings-access/").json(), SETTINGS_ACCESS)
        self.client.force_login(self.ida)
        self.assertEqual(set(self.client.get("/api/v1/accounts/settings-access/").json().values()), {"full"})

    def test_each_page_answers_a_site_admin_as_the_table_says(self):
        for page, (read, write) in PROBES.items():
            access = SETTINGS_ACCESS[page]
            with self.subTest(page=page):
                if read is not None:
                    code = self.call(self.dana, read).status_code
                    if access == "hidden":
                        self.assertEqual(code, 403, f"{page}: a hidden page must refuse a read")
                    else:
                        self.assertNotEqual(code, 403, f"{page}: a {access} page must allow a read")
                if write is not None and access != "full":
                    self.assertEqual(self.call(self.dana, write).status_code, 403,
                                     f"{page}: a {access} page must refuse a site admin's write")

    def test_instance_admins_keep_every_page(self):
        for page, (read, write) in PROBES.items():
            with self.subTest(page=page):
                for probe in (read, write):
                    if probe is not None:
                        self.assertNotEqual(self.call(self.ida, probe).status_code, 403, page)

    def test_a_site_admin_cannot_hand_out_install_roles(self):
        # The escalation the split closes: an admin profile with no site rows
        # is an instance admin.
        self.assertEqual(self.call(self.dana, PROBES["users"][1]).status_code, 403)
        self.other.profile.refresh_from_db()
        self.assertEqual(self.other.profile.role, Role.VIEWER)
        self.assertEqual(self.call(self.ida, PROBES["users"][1]).status_code, 200)

    def test_a_greyed_read_shows_no_secret_holding_group(self):
        self.client.force_login(self.dana)
        groups = self.client.get("/api/v1/instance-settings/").json()["groups"]
        self.assertEqual(sorted(g["id"] for g in groups), ["locale", "retention", "sessions"])
        self.assertTrue(all(g["read_only"] for g in groups))
        body = json.dumps(groups)
        for name in ("EMAIL_HOST_PASSWORD", "NESSUS_SECRET_KEY", "GREENBONE_PASSWORD", "JACKIL_API_KEY"):
            self.assertNotIn(name, body)

    def test_the_audit_log_is_instance_admin_only(self):
        self.client.force_login(self.dana)
        self.assertEqual(self.client.get("/api/v1/audits/").status_code, 403)
        self.client.force_login(self.ida)
        self.assertEqual(self.client.get("/api/v1/audits/").status_code, 200)


class SettingsRoutesAreClassifiedTests(TestCase):
    """Every endpoint behind a settings page names its page in code, pinned
    here; a new one fails until it is added on purpose."""

    ROUTES = {
        "api/v1/ai/providers/": "ai",
        "api/v1/ai/providers/<int:provider_id>/": "ai",
        "api/v1/ai/providers/<int:provider_id>/test/": "ai",
        "api/v1/hosts/ad/": "directory",
        "api/v1/hosts/ad/sync/": "directory",
        "api/v1/relays/": "relays",
        "api/v1/relays/<uuid:relay_id>/": "relays",
        "api/v1/relays/<uuid:relay_id>/rotate-key/": "relays",
        "agent/upload/<str:platform>/": "distribution",
        "api/v1/license/": "license",
        "api/v1/license/refresh/": "license",
        "api/v1/jackil/tickets/": "jackil",
        "api/v1/jackil/integration-keys/": "jackil",
        "api/v1/jackil/integration-keys/<int:pk>/revoke/": "jackil",
        "api/v1/jackil/integration-keys/<int:pk>/devices/": "jackil",
        "api/v1/vulns/data/": "retention",
        "api/v1/instance-settings/test/": "email",
        "api/v1/accounts/users/": "users",
        "api/v1/accounts/users/<int:user_id>/": "users",
        "api/v1/accounts/users/<int:user_id>/role/": "users",
        "api/v1/reprovision/images/": "images",
        "api/v1/reprovision/images/pull/": "images",
        "api/v1/reprovision/images/upload/": "images",
        "api/v1/reprovision/images/<uuid:image_id>/": "images",
        "api/v1/civil/settings/": "identity",
        "api/v1/branding/": "branding",
    }

    def test_each_names_its_page(self):
        routes = dict(walk.all_routes())
        missing = sorted(r for r in self.ROUTES if r not in routes)
        self.assertEqual(missing, [], "stale entries")
        for route, page in self.ROUTES.items():
            with self.subTest(route=route):
                self.assertIn(page, _pages_named(routes[route]))

    def test_instance_settings_names_a_page_for_every_group(self):
        from apps.instance import registry
        from apps.instance.views import GROUP_PAGE
        self.assertEqual(sorted(GROUP_PAGE), sorted({k.group for k in registry.KEYS}))
        self.assertTrue(set(GROUP_PAGE.values()) <= set(SETTINGS_ACCESS))


def _pages_named(callback):
    """The settings pages a view names: a SettingsPage permission, or a
    settings_allows(..., "page") call in the view function's own source."""
    import importlib

    cls = getattr(callback, "cls", None)
    pages = {getattr(p, "settings_page", None) for p in getattr(cls, "permission_classes", ())}
    # DRF's api_view names its wrapper class after the function it wraps.
    owner = cls if cls is not None else callback
    # The decorated module attribute is DRF's view, so read the function's
    # own text out of its module rather than asking inspect for it.
    module_src = inspect.getsource(importlib.import_module(owner.__module__))
    match = re.search(rf"^def {owner.__name__}\(.*?(?=^(?:def |class |@)|\Z)", module_src, re.M | re.S)
    if match:
        pages |= set(re.findall(r'settings_allows\([^,]+, "([a-z]+)"', match.group(0)))
    return pages - {None}
