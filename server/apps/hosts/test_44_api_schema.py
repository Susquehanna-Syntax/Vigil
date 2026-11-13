"""The twelve endpoints phase 44 typed (14.3, 14.4, M11, QA-25, the Firewall
tab and the Tag rows): each body against the serializer its ``@extend_schema``
now names.

One file rather than six: the shape of a response is what the schema promises a
client, and the promise lives in one place, so the check does too. It runs with
the Business apps installed, because ``site_id`` and ``can_change`` are null or
absent without them — which would let a genuinely wrong shape pass here.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert
from apps.hosts.models import (
    DockerContainer,
    Host,
    HostFirewall,
    HostNetwork,
    Tag,
    UnmanagedDevice,
)
from apps.vulns.models import VulnFinding
from vigil.schema_check import assert_shape
from vigil.testsupport import require_business_module

# Exercises core with the Business apps; a source checkout has none.
require_business_module()

_TOTP = "apps.accounts.totp.require_totp_confirmation"

INTERFACES = [{"name": "eth0", "up": True, "mac": "aa:bb:cc:dd:ee:ff",
               "addrs": [{"family": "ipv4", "address": "10.0.0.5", "prefix": 24}]}]


class HostsApiSchemaTests(TestCase):
    """Every typed hosts endpoint, with data present so lists are non-empty."""

    def setUp(self):
        from apps_business.sites.models import Site

        self.admin = get_user_model().objects.create_user("p44admin", password="x")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

        self.site = Site.objects.create(name="West", slug="west-44")
        self.web = Host.objects.create(hostname="web-01", agent_token="p44a" * 8,
                                       status=Host.Status.ONLINE, mode=Host.Mode.FULL_CONTROL,
                                       os="Ubuntu 24.04", ip_address="10.0.0.5",
                                       tags=["prod"])
        self.nas = Host.objects.create(hostname="nas", agent_token="p44b" * 8,
                                       status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
                                       os="Debian 12", ip_address="10.0.0.6")
        for host in (self.web, self.nas):
            HostNetwork.objects.create(host=host, interfaces=INTERFACES, collected_at=timezone.now(),
                                       listening=[{"proto": "tcp", "port": 22, "address": "0.0.0.0",
                                                   "process": "sshd"}],
                                       connections=[{"proto": "tcp", "direction": "out",
                                                     "remote_ip": "10.0.0.6", "remote_port": 5432,
                                                     "local_port": 41234, "process": "python3",
                                                     "count": 3}])
            VulnFinding.objects.create(host=host, scanner="trivy", plugin_id_or_oid="p",
                                       cve_id="CVE-2026-4444", severity="high", state="open",
                                       title="openssl overflow")
        UnmanagedDevice.objects.create(name="edge-router", device_type="router",
                                       ip_address="10.0.0.1")
        Alert.objects.create(host=self.web, severity="critical", message="disk nearly full",
                             state="firing")

    # ── docker (14.3) ───────────────────────────────────────────────────────

    def test_the_docker_snapshot_and_summary_are_the_documented_shapes(self):
        from .api_schema import (
            HostsDockerHostSummarySerializer,
            HostsDockerSnapshotSerializer,
        )

        DockerContainer.objects.create(host=self.web, container_id="c1", name="db",
                                       image="postgres:16", image_id="sha256:" + "a" * 64,
                                       state="running", stack="shop",
                                       ports=[{"private": 5432, "public": 5432, "type": "tcp"}],
                                       networks=[{"name": "shop_default", "ip": "172.20.0.3"}],
                                       mounts=[{"type": "volume", "name": "shop_db",
                                                "destination": "/var/lib/db", "rw": True}])
        self.web.docker_images.create(image_id="sha256:" + "a" * 64, tags=["postgres:16"],
                                      digests=[], size_bytes=1000,
                                      created=timezone.now() - timedelta(days=3))
        self.web.docker_volumes.create(name="shop_db", driver="local", mountpoint="/var/lib/docker")
        self.web.docker_networks.create(network_id="n1", name="shop_default", driver="bridge",
                                        subnets=["172.20.0.0/16"], stack="shop")

        assert_shape(self, self.api.get(f"/api/v1/hosts/{self.web.id}/docker/").json(),
                     HostsDockerSnapshotSerializer)
        assert_shape(self, self.api.get("/api/v1/hosts/docker-summary/").json(),
                     HostsDockerHostSummarySerializer(many=True))

    # ── live container logs (M11) ───────────────────────────────────────────

    def test_opening_polling_and_feeding_a_log_tail_are_the_documented_shapes(self):
        from .api_schema import (
            HostsLogTailAgentUploadSerializer,
            HostsLogTailCreatedSerializer,
            HostsLogTailPollSerializer,
        )

        with mock.patch(_TOTP, return_value=None):
            opened = self.api.post(f"/api/v1/hosts/{self.web.id}/containers/web/logs/",
                                   {"totp": "123456", "tail": 50}, format="json")
        self.assertEqual(opened.status_code, 201, opened.content)
        assert_shape(self, opened.json(), HostsLogTailCreatedSerializer)

        session = opened.json()["session"]
        fed = self.client.post(f"/api/v1/agent/log-tail/{session}/", {"lines": ["a", "b"]},
                               content_type="application/json",
                               HTTP_AUTHORIZATION=f"Bearer {self.web.raw_agent_token}")
        self.assertEqual(fed.status_code, 200, fed.content)
        # The answer's only key is "continue", a Python keyword no serializer
        # field can carry — so the schema documents it as an object, and the
        # behaviour of that key is what LogTailTests pins.
        self.assertIs(fed.json()["continue"], True)
        assert_shape(self, {"lines": ["a"]}, HostsLogTailAgentUploadSerializer)

        polled = self.api.get(f"/api/v1/hosts/log-tails/{session}/", {"after": 0})
        self.assertEqual(polled.status_code, 200, polled.content)
        self.assertNotEqual(polled.json()["lines"], [], "an empty list checks no item shape")
        assert_shape(self, polled.json(), HostsLogTailPollSerializer)

    # ── search everything (QA-25) ───────────────────────────────────────────

    def test_a_search_that_finds_everything_is_the_documented_shape(self):
        from .api_schema import HostsSearchSerializer

        DockerContainer.objects.create(host=self.nas, container_id="c9", name="webproxy",
                                       image="nginx:1.25", state="running")
        by_kind = {}
        for query in ("web", "2026", "disk", "edge"):
            resp = self.api.get("/api/v1/search/", {"q": query})
            self.assertEqual(resp.status_code, 200, resp.content)
            assert_shape(self, resp.json(), HostsSearchSerializer)
            by_kind.update({g["kind"]: g["results"] for g in resp.json()["groups"]})
        # A kind whose list stayed empty had no item checked, so the shapes
        # with a nested result have to be represented between them.
        self.assertGreaterEqual(set(by_kind), {"host", "container", "cve"})

    # ── topology (14.4) ─────────────────────────────────────────────────────

    def test_the_topology_response_is_the_documented_shape(self):
        from .api_schema import HostsTopologySerializer

        DockerContainer.objects.create(host=self.nas, container_id="c7", name="jellyfin",
                                       stack="media", state="running",
                                       ports=[{"private": 8096, "public": 8096, "type": "tcp"}],
                                       networks=[{"name": "media_default", "ip": "172.21.0.4"}])
        HostFirewall.objects.create(host=self.web, tool="ufw", enabled=True,
                                    defaults={"incoming": "deny", "outgoing": "allow"},
                                    rules=[{"port": 22, "protocol": "tcp", "action": "allow"}])
        resp = self.api.get("/api/v1/hosts/topology/")
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertNotEqual(payload["hosts"], [])
        assert_shape(self, payload, HostsTopologySerializer)

    # ── firewall tab ────────────────────────────────────────────────────────

    def test_the_firewall_read_refresh_and_apply_are_the_documented_shapes(self):
        from .api_schema import (
            HostsErrorSerializer,
            HostsFirewallSerializer,
            HostsFirewallTaskSerializer,
        )

        assert_shape(self, self.api.get(f"/api/v1/hosts/{self.web.id}/firewall/").json(),
                     HostsFirewallSerializer)

        refused = self.api.post(f"/api/v1/hosts/{self.nas.id}/firewall/refresh/")
        self.assertEqual(refused.status_code, 400, refused.content)
        assert_shape(self, refused.json(), HostsErrorSerializer)

        queued = self.api.post(f"/api/v1/hosts/{self.web.id}/firewall/refresh/")
        self.assertEqual(queued.status_code, 202, queued.content)
        assert_shape(self, queued.json(), HostsFirewallTaskSerializer)

        unconfirmed = self.api.post(
            f"/api/v1/hosts/{self.web.id}/firewall/apply/",
            {"action": "add_firewall_rule", "params": {"port": 8080, "protocol": "tcp"}},
            content_type="application/json")
        self.assertEqual(unconfirmed.status_code, 401, unconfirmed.content)
        assert_shape(self, unconfirmed.json(), HostsErrorSerializer)

        with mock.patch(_TOTP, return_value=None):
            applied = self.api.post(
                f"/api/v1/hosts/{self.web.id}/firewall/apply/",
                {"action": "add_firewall_rule", "params": {"port": 8080, "protocol": "tcp"},
                 "totp": "123456"},
                content_type="application/json")
        self.assertEqual(applied.status_code, 201, applied.content)
        assert_shape(self, applied.json(), HostsFirewallTaskSerializer)

    # ── tags ────────────────────────────────────────────────────────────────

    def test_the_tag_list_creation_rename_and_refusals_are_the_documented_shapes(self):
        from .api_schema import (
            HostsErrorSerializer,
            HostsFieldErrorsSerializer,
            HostsTagSerializer,
        )

        listed = self.api.get("/api/v1/tags/")
        self.assertEqual(listed.status_code, 200, listed.content)
        assert_shape(self, listed.json(), HostsTagSerializer(many=True))

        created = self.api.post("/api/v1/tags/", {"name": "west", "site_id": str(self.site.id),
                                                  "description": "West Campus boxes"},
                                content_type="application/json")
        self.assertEqual(created.status_code, 201, created.content)
        assert_shape(self, created.json(), HostsTagSerializer)

        patched = self.api.patch(f"/api/v1/tags/{created.json()['id']}/",
                                 {"description": "west only"}, content_type="application/json")
        self.assertEqual(patched.status_code, 200, patched.content)
        assert_shape(self, patched.json(), HostsTagSerializer)

        clash = self.api.post("/api/v1/tags/", {"name": "WEST"}, content_type="application/json")
        self.assertEqual(clash.status_code, 400, clash.content)
        assert_shape(self, clash.json(), HostsFieldErrorsSerializer)

        auto = Tag.get_or_create_by_name("os:debian")[0]
        refused = self.api.patch(f"/api/v1/tags/{auto.pk}/", {"name": "os:ubuntu"},
                                 content_type="application/json")
        self.assertEqual(refused.status_code, 400, refused.content)
        assert_shape(self, refused.json(), HostsErrorSerializer)

        used = self.api.delete(f"/api/v1/tags/{created.json()['id']}/")
        self.assertIn(used.status_code, (204, 409))
        if used.status_code != 204:
            assert_shape(self, used.json(), HostsErrorSerializer)

    def test_an_unknown_host_404s_the_documented_way_everywhere(self):
        from uuid import uuid4

        from .api_schema import HostsErrorSerializer

        gone = str(uuid4())
        for url in (f"/api/v1/hosts/{gone}/docker/", f"/api/v1/hosts/{gone}/firewall/",
                    "/api/v1/tags/999999/"):
            resp = self.api.get(url)
            self.assertEqual(resp.status_code, 404, url)
            assert_shape(self, resp.json(), HostsErrorSerializer)

        for url in (f"/api/v1/hosts/{gone}/firewall/refresh/",
                    f"/api/v1/hosts/{gone}/firewall/apply/",
                    f"/api/v1/hosts/{gone}/containers/web/logs/"):
            resp = self.api.post(url, {}, content_type="application/json")
            self.assertEqual(resp.status_code, 404, url)
            assert_shape(self, resp.json(), HostsErrorSerializer)
