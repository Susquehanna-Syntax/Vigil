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

    def test_checkin_syncs_mode_from_agent(self):
        # setUp host is MONITOR; the agent reports its authoritative local mode
        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "web-01", "mode": "managed"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.mode, Host.Mode.MANAGED)

    def test_checkin_ignores_invalid_mode(self):
        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "web-01", "mode": "root_me_please"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.mode, Host.Mode.MONITOR)

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
        # Explicitly an admin. A profile defaults to VIEWER, and these tests are
        # about the TOTP gate — before hosts.update_agent existed they passed
        # because a read-only user could force an agent update at all.
        from apps.accounts.models import Role

        profile = UserProfile.objects.create(user=self.user, role=Role.ADMIN)
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


class EnrollmentOriginTests(TestCase):
    """The install one-liner must point at VIGIL_PUBLIC_URL when it is set.

    Enrolling from a LAN address bakes that address into the agent, which then
    silently stops checking in once the host leaves the LAN — the exact failure
    remote access exists to prevent. See docs/REMOTE-ACCESS.md.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw", is_staff=True)
        self.client.force_login(self.user)

    @override_settings(VIGIL_PUBLIC_URL="https://vigil.example.com")
    def test_public_url_is_served_to_the_enrollment_snippet(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "https://vigil.example.com")

    @override_settings(VIGIL_PUBLIC_URL="")
    def test_falls_back_to_browser_origin_when_unset(self):
        """Unset means the template hands JS an empty string and JS falls back."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="vigil-public-url"')
        self.assertContains(resp, "window.location.origin")


class HostSitePayloadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "sp", password="x", is_staff=True)
        self.client.force_login(self.user)

    def test_every_host_reports_a_site(self):
        Host.objects.create(hostname="loose", agent_token="tok-loose")
        row = self.client.get("/api/v1/hosts/").json()[0]
        self.assertEqual(row["site_name"], "Global")
        self.assertIsNotNone(row["site"])

    def test_assigned_host_reports_its_own_site(self):
        from apps_business.sites.models import HostSiteAssignment, Site
        h = Host.objects.create(hostname="west-01", agent_token="tok-w")
        site = Site.objects.create(name="West Campus", slug="west-campus")
        HostSiteAssignment.objects.create(host=h, site=site)
        row = next(r for r in self.client.get("/api/v1/hosts/").json()
                   if r["hostname"] == "west-01")
        self.assertEqual(row["site_name"], "West Campus")


class WindowsUpdateIngestTests(TestCase):
    """The agent's update count, and the difference between zero and unknown."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="win", ip_address="10.97.0.2", agent_token="wtok",
            status=Host.Status.ONLINE, mode="managed")

    def _checkin(self, payload):
        return self.client.post(
            "/api/v1/checkin", {"hostname": "win", **payload},
            content_type="application/json", HTTP_AUTHORIZATION="Bearer wtok")

    def test_a_reported_count_is_stored(self):
        self._checkin({"windows_updates": {"pending": 7, "critical": 2,
                                           "important": 3, "reboot_required": 1}})
        self.host.refresh_from_db()
        self.assertEqual(self.host.windows_updates["pending"], 7)
        self.assertEqual(self.host.windows_updates["critical"], 2)
        self.assertIsNotNone(self.host.windows_updates_at)

    def test_an_absent_key_leaves_the_previous_count_alone(self):
        """A Linux host, or an agent too old to count, must not zero it."""
        self._checkin({"windows_updates": {"pending": 4}})
        self._checkin({})
        self.host.refresh_from_db()
        self.assertEqual(self.host.windows_updates["pending"], 4)

    def test_zero_pending_is_recorded_as_zero_not_unknown(self):
        self._checkin({"windows_updates": {"pending": 0}})
        self.host.refresh_from_db()
        self.assertEqual(self.host.windows_updates["pending"], 0)

    def test_a_host_that_never_reported_stays_null(self):
        self.assertIsNone(self.host.windows_updates)

    def test_junk_in_the_payload_does_not_break_a_checkin(self):
        resp = self._checkin({"windows_updates": "not a dict"})
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertIsNone(self.host.windows_updates)
