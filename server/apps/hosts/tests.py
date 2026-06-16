from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.hosts.models import Host


class RegisterTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_creates_pending_host(self):
        resp = self.client.post(
            "/api/v1/register",
            {"agent_token": "tok-" + "a" * 32, "hostname": "web-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        host = Host.objects.get(agent_token="tok-" + "a" * 32)
        self.assertEqual(host.status, Host.Status.PENDING)
        self.assertEqual(host.hostname, "web-01")

    def test_register_requires_token_and_hostname(self):
        resp = self.client.post("/api/v1/register", {"hostname": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)


class CheckinIpTrustTests(TestCase):
    """Regression coverage for F1 — an agent must not set its own IP address."""

    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-01",
            agent_token="tok-" + "b" * 32,
            status=Host.Status.ONLINE,
            mode=Host.Mode.MONITOR,
        )

    def test_checkin_ignores_agent_supplied_ip(self):
        resp = self.client.post(
            "/api/v1/checkin",
            {"ip_address": "10.9.9.9", "hostname": "web-01"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
            REMOTE_ADDR="203.0.113.7",
        )
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        # The connection address wins; the body value is discarded.
        self.assertEqual(self.host.ip_address, "203.0.113.7")

    def test_checkin_rejects_bad_token(self):
        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "web-01"},
            format="json",
            HTTP_AUTHORIZATION="Bearer not-a-real-token",
        )
        self.assertEqual(resp.status_code, 401)


class AgentTagNamespaceTests(TestCase):
    """Agent-advertised tags must land under agent:* so a rogue agent
    can't mint an operator-looking tag and opt into tag-targeted deploys."""

    def setUp(self):
        self.client = APIClient()

    def test_register_namespaces_agent_tags(self):
        self.client.post(
            "/api/v1/register",
            {"agent_token": "tok-" + "c" * 32, "hostname": "h", "tags": ["prod"]},
            format="json",
        )
        host = Host.objects.get(agent_token="tok-" + "c" * 32)
        self.assertIn("agent:prod", host.tags)
        self.assertNotIn("prod", host.tags)

    def test_checkin_namespaces_agent_tags(self):
        host = Host.objects.create(
            hostname="h", agent_token="tok-" + "d" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        )
        self.client.post(
            "/api/v1/checkin",
            {"hostname": "h", "tags": ["office"]},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {host.agent_token}",
        )
        host.refresh_from_db()
        self.assertIn("agent:office", host.tags)
        self.assertNotIn("office", host.tags)


class ForceUpdateAgentTests(TestCase):
    def setUp(self):
        from apps.accounts.models import UserProfile
        from apps.accounts.totp import generate_secret
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        profile = UserProfile.objects.create(user=self.user)
        self.secret = generate_secret()
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )

    def _totp(self):
        from apps.accounts.totp import generate_totp
        return generate_totp(self.secret)

    def test_monitor_mode_rejected(self):
        self.host.mode = Host.Mode.MONITOR
        self.host.save()
        resp = self.client.post(
            f"/api/v1/hosts/{self.host.id}/update-agent/",
            {"totp": self._totp()}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_requires_totp(self):
        from apps.agent_dist.models import AgentBinary
        AgentBinary.objects.create(platform="linux-amd64", version="1", sha256="a" * 64)
        resp = self.client.post(
            f"/api/v1/hosts/{self.host.id}/update-agent/", {}, format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_no_binaries_503(self):
        resp = self.client.post(
            f"/api/v1/hosts/{self.host.id}/update-agent/",
            {"totp": self._totp()}, format="json",
        )
        self.assertEqual(resp.status_code, 503)

    @override_settings(VIGIL_AGENT_DIST_DIR="/nonexistent-vigil-test-dist")
    def test_queues_signed_update_task(self):
        from apps.agent_dist.models import AgentBinary
        from apps.tasks.models import Task
        AgentBinary.objects.create(platform="linux-amd64", version="1", sha256="a" * 64)
        resp = self.client.post(
            f"/api/v1/hosts/{self.host.id}/update-agent/",
            {"totp": self._totp()}, format="json",
        )
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        task = Task.objects.get(host=self.host, action="update_agent")
        self.assertEqual(task.params["binary_sha256"]["linux-amd64"], "a" * 64)
        self.assertEqual(task.state, Task.State.PENDING)


class AboutEndpointAuthTests(TestCase):
    """/api/v1/about/ leaks version/scanner fingerprints — session-gated."""

    def setUp(self):
        self.client = APIClient()

    def test_anonymous_denied(self):
        resp = self.client.get("/api/v1/about/")
        self.assertIn(resp.status_code, (401, 403))

    def test_authenticated_ok(self):
        user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(user)
        resp = self.client.get("/api/v1/about/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("vigil_version", resp.data)
