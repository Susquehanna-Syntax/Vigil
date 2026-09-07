"""Every per-host endpoint must respect site scope, not just authentication.

The host *list* has always been narrowed by site. The per-host reads and writes
behind it were not, so an operator confined to one site could read another
site's metrics, containers and inventory — and approve, reboot, re-tag or
re-firewall its machines — simply by knowing an id. Sites is the feature whose
entire value is that this does not happen.

Out of scope answers 404 rather than 403 throughout: a 403 confirms the host
exists somewhere the caller cannot see.
"""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch

from apps.hosts.models import Host

#: Modules holding endpoints that take a host id.
PER_HOST_VIEW_MODULES = (
    "apps/hosts/views.py",
    "apps/metrics/views.py",
    "apps/vulns/views.py",
    "apps/aisuggest/views.py",
    "apps/statuspage/views.py",
)


class PerHostLookupsAreScopedTests(TestCase):
    def test_no_per_host_view_looks_a_host_up_unscoped(self):
        """A direct Host.objects.get(pk=host_id) skips the scope check."""
        offenders = []
        for rel in PER_HOST_VIEW_MODULES:
            path = Path(settings.BASE_DIR) / rel
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"Host\.objects\.get\(\s*(pk|id)\s*=\s*host_id", line) or \
                   re.search(r"get_object_or_404\(\s*Host\s*,\s*(pk|id)\s*=\s*host_id", line):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "look the host up with scoping.visible_host(request.user, host_id) "
            "so a scoped operator cannot reach it by id:\n  " + "\n  ".join(offenders))

    def test_the_helper_treats_missing_and_invisible_alike(self):
        from vigil import scoping

        user = get_user_model().objects.create_user("scoped", password="pw")
        host = Host.objects.create(hostname="h", ip_address="10.99.0.2",
                                   agent_token="t", status=Host.Status.ONLINE)
        self.assertIsNotNone(scoping.visible_host(user, host.id))
        with patch("vigil.scoping.host_in_scope", return_value=False):
            self.assertIsNone(scoping.visible_host(user, host.id))
        self.assertIsNone(scoping.visible_host(user, "00000000-0000-0000-0000-000000000000"))


class OutOfScopeHostIsInvisibleTests(TestCase):
    """Drive the real endpoints with scope denied and assert none leaks."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "op", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="secret", ip_address="10.99.0.3", agent_token="tok",
            status=Host.Status.ONLINE, mode="managed")

    def _urls(self):
        h = self.host.id
        return [
            ("GET", f"/api/v1/hosts/{h}/"),
            ("GET", f"/api/v1/hosts/{h}/inventory/"),
            ("GET", f"/api/v1/hosts/{h}/containers/"),
            ("GET", f"/api/v1/hosts/{h}/firewall/"),
            ("GET", f"/api/v1/metrics/{h}/cpu/usage_percent/"),
            ("GET", f"/api/v1/vulns/history/{h}/"),
            ("GET", f"/api/v1/status-pages/uptime/{h}/"),
            ("POST", f"/api/v1/hosts/{h}/approve/"),
            ("POST", f"/api/v1/hosts/{h}/reject/"),
            ("POST", f"/api/v1/hosts/{h}/poll/"),
        ]

    def test_every_per_host_endpoint_hides_a_host_out_of_scope(self):
        leaked = []
        with patch("vigil.scoping.host_in_scope", return_value=False):
            for method, url in self._urls():
                resp = (self.client.get(url) if method == "GET"
                        else self.client.post(url, {}, content_type="application/json"))
                # 404 is the required answer; 402/403 from a licence or role gate
                # that runs first is acceptable — it leaks nothing either.
                if resp.status_code not in (401, 402, 403, 404):
                    leaked.append(f"{method} {url} -> {resp.status_code}")
        self.assertEqual(leaked, [], "reachable despite being out of scope:\n  "
                         + "\n  ".join(leaked))

    def test_the_same_endpoints_work_when_the_host_is_in_scope(self):
        """The guard above would also pass if everything were simply broken."""
        ok = 0
        for method, url in self._urls():
            if method != "GET":
                continue
            resp = self.client.get(url)
            if resp.status_code == 200:
                ok += 1
        self.assertGreater(ok, 3, "in-scope reads are not working; the scoping "
                                  "test above proves nothing")
