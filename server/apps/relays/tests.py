"""Relays: the model, its edition limit, and the gate on agent paths."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from vigil import licensing

from .middleware import RelayGateMiddleware
from .models import Relay, direct_allowed, enabled_count, generate_key, relay_urls
from .trust import link_token


def make_relay(name="r1", url="https://relay.example.com/", kind=Relay.Kind.REMOTE,
               enabled=True, last_seen=None):
    return Relay.objects.create(
        name=name, url=url.rstrip("/"), kind=kind, enabled=enabled,
        last_seen_at=last_seen)


def checkin(client, host, **environ):
    return client.post(
        "/api/v1/checkin",
        {"hostname": host.hostname},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}",
        **environ)


class RelayModelTests(TestCase):
    def test_builtin_row_exists(self):
        relays = Relay.objects.filter(kind=Relay.Kind.BUILTIN)
        self.assertEqual(relays.count(), 1)
        self.assertTrue(relays.get().enabled)

    def test_generate_key_shape(self):
        key = generate_key()
        self.assertTrue(key.startswith("vrk_"))
        self.assertGreater(len(key), 30)

    def test_direct_allowed_tracks_the_builtin_row(self):
        self.assertTrue(direct_allowed())
        builtin = Relay.objects.get(kind=Relay.Kind.BUILTIN)
        builtin.enabled = False
        builtin.save(update_fields=["enabled"])
        self.assertFalse(direct_allowed())

    def test_relay_urls_is_enabled_remote_only_in_name_order(self):
        # "keyless" is a disabled relay with no url to hand out; both keep it out
        # of relay_urls(), the license counter does not count relays that are off.
        make_relay("beta", "https://b.example.com/")
        make_relay("alpha", "https://a.example.com")
        make_relay("zulu", "https://z.example.com", enabled=False)
        make_relay("keyless", "", enabled=False)
        self.assertEqual(relay_urls(), ["https://a.example.com",
                                        "https://b.example.com"])
        self.assertEqual(enabled_count(), 3)  # builtin + beta + alpha


class RelayApiTests(TestCase):
    def setUp(self):
        totp = patch("apps.relays.views.require_totp_confirmation", return_value=None)
        totp.start()
        self.addCleanup(totp.stop)
        self.client = APIClient()
        self.admin = get_user_model().objects.create_user(username="admin")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def _create(self, **body):
        body.setdefault("name", "edge")
        body.setdefault("url", "https://relay.example.com/")
        return self.client.post("/api/v1/relays/", body, format="json")

    def test_community_limit_is_one(self):
        self.assertEqual(licensing.relays_allowed(), 1)
        over = self._create()
        self.assertEqual(over.status_code, 402)
        self.assertEqual(over.json()["allowed"], 1)
        self.assertEqual(over.json()["used"], 1)

        created = self._create(enabled=False)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["key"].startswith("vrk_"))
        self.assertEqual(Relay.objects.count(), 2)

        listing = self.client.get("/api/v1/relays/").json()
        body = str(listing)
        self.assertNotIn("key", body)
        self.assertNotIn("key_sha256", body)
        self.assertEqual([r["kind"] for r in listing["relays"]],
                         ["builtin", "remote"])

    def test_business_limit_is_seats_plus_two(self):
        with patch("apps.relays.views.relays_allowed", return_value=6):
            for i in range(5):
                resp = self._create(name=f"r{i}")
                self.assertEqual(resp.status_code, 201, resp.json())
            sixth = self._create(name="one-too-many")
        self.assertEqual(sixth.status_code, 402)
        self.assertEqual(enabled_count(), 6)

    def test_licensing_relays_allowed_is_seats_plus_two_when_business(self):
        state = licensing.LicenseState(status=licensing.Status.VALID,
                                       claims=_claims(seats=4))
        with patch("vigil.licensing.current_state", return_value=state):
            self.assertEqual(licensing.relays_allowed(), 6)

    def test_cannot_disable_builtin_without_a_connected_relay(self):
        builtin = Relay.objects.get(kind=Relay.Kind.BUILTIN)
        refused = self.client.patch(f"/api/v1/relays/{builtin.id}/",
                                    {"enabled": False}, format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("connected at least once", refused.json()["detail"])

        make_relay("edge", "https://relay.example.com",
                   last_seen=now() - timedelta(minutes=1))
        # That relay must be enabled to count as a way in.
        ok = self.client.patch(f"/api/v1/relays/{builtin.id}/",
                               {"enabled": False}, format="json")
        self.assertEqual(ok.status_code, 200)
        builtin.refresh_from_db()
        self.assertFalse(builtin.enabled)

    def test_totp_required_on_create_and_rotate(self):
        with patch("apps.relays.views.require_totp_confirmation",
                   return_value="bad code"):
            resp = self.client.post("/api/v1/relays/",
                                    {"name": "edge",
                                     "url": "https://relay.example.com"},
                                    format="json")
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["detail"], "bad code")
        self.assertEqual(Relay.objects.count(), 1)  # builtin only

        remote = make_relay()
        with patch("apps.relays.views.require_totp_confirmation",
                   return_value="bad code"):
            refused = self.client.post(f"/api/v1/relays/{remote.id}/rotate-key/",
                                       {}, format="json")
        self.assertEqual(refused.status_code, 400)
        remote.refresh_from_db()
        self.assertEqual(remote.key_sha256, "")

    def test_rotate_key_issues_a_new_one(self):
        remote = make_relay()
        with patch("apps.relays.views.require_totp_confirmation", return_value=None):
            resp = self.client.post(f"/api/v1/relays/{remote.id}/rotate-key/",
                                    {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["key"].startswith("vrk_"))
        remote.refresh_from_db()
        self.assertEqual(remote.key_sha256, resp.json()["key_sha256"])

    def test_url_validation(self):
        for bad in ("ftp://x", "https://a b", ""):
            resp = self._create(url=bad)
            self.assertEqual(resp.status_code, 400, bad)
        self.assertEqual(Relay.objects.count(), 1)

    def test_trailing_slash_is_stripped_once(self):
        # Community allows one connection point, so this one is stored disabled —
        # the URL validation (including the single trailing "/" stripped) is what
        # is under test here.
        created = self._create(url="https://relay.example.com/t/acme/",
                               enabled=False)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["url"], "https://relay.example.com/t/acme")

    def test_delete_removes_a_remote_but_never_the_builtin(self):
        remote = make_relay()
        self.assertEqual(
            self.client.delete(f"/api/v1/relays/{remote.id}/").status_code, 204)
        builtin = Relay.objects.get(kind=Relay.Kind.BUILTIN)
        self.assertEqual(
            self.client.delete(f"/api/v1/relays/{builtin.id}/").status_code, 400)

    def test_non_admin_forbidden(self):
        viewer = get_user_model().objects.create_user(username="watcher")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        self.client.force_authenticate(viewer)
        self.assertEqual(self.client.get("/api/v1/relays/").status_code, 403)


class RelayGateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="h1", agent_token="a" * 64,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)

    def test_direct_agent_refused_when_builtin_off(self):
        builtin = Relay.objects.get(kind=Relay.Kind.BUILTIN)
        builtin.enabled = False
        builtin.save(update_fields=["enabled"])

        resp = checkin(self.client, self.host)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.json()["error"],
            "Direct agent connections are off. Connect through a relay.")
        self.assertEqual(self.client.get("/api/v1/health/").status_code, 200)

    def test_link_header_attests_ip_and_relay(self):
        builtin = Relay.objects.get(kind=Relay.Kind.BUILTIN)
        builtin.enabled = False
        builtin.save(update_fields=["enabled"])
        remote = make_relay()

        resp = checkin(self.client, self.host,
                       HTTP_X_VIGIL_LINK=link_token(),
                       HTTP_X_VIGIL_RELAY=str(remote.id),
                       HTTP_X_VIGIL_AGENT_IP="198.51.100.9")
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.ip_address, "198.51.100.9")
        self.assertEqual(self.host.last_relay, remote)

    def test_forged_headers_are_ignored(self):
        resp = checkin(self.client, self.host,
                       HTTP_X_VIGIL_LINK="not-the-token",
                       HTTP_X_VIGIL_RELAY=str(uuid4()),
                       HTTP_X_VIGIL_AGENT_IP="198.51.100.9",
                       REMOTE_ADDR="203.0.113.7")
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.ip_address, "203.0.113.7")
        self.assertIsNone(self.host.last_relay)

    def test_unknown_relay_id_refused(self):
        resp = checkin(self.client, self.host,
                       HTTP_X_VIGIL_LINK=link_token(),
                       HTTP_X_VIGIL_RELAY=str(uuid4()),
                       HTTP_X_VIGIL_AGENT_IP="198.51.100.9")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "Unknown relay")

    def test_disabled_relay_id_refused(self):
        remote = make_relay(enabled=False)
        resp = checkin(self.client, self.host,
                       HTTP_X_VIGIL_LINK=link_token(),
                       HTTP_X_VIGIL_RELAY=str(remote.id))
        self.assertEqual(resp.status_code, 403)

    def test_checkin_lists_fallbacks(self):
        make_relay("beta", "https://b.example.com")
        make_relay("alpha", "https://a.example.com")
        make_relay("dark", "https://d.example.com", enabled=False)

        resp = checkin(self.client, self.host)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["relays"], ["https://a.example.com",
                                          "https://b.example.com"])
        self.assertTrue(body["direct"])

    def test_unparseable_agent_ip_leaves_the_socket_address(self):
        resp = checkin(self.client, self.host,
                       HTTP_X_VIGIL_LINK=link_token(),
                       HTTP_X_VIGIL_RELAY=str(make_relay().id),
                       HTTP_X_VIGIL_AGENT_IP="not-an-ip",
                       REMOTE_ADDR="203.0.113.7")
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.ip_address, "203.0.113.7")


def _claims(seats):
    return licensing.Claims(instance="i", org="o", seats=seats, exp=0, iat=0)


class MiddlewarePassthroughTests(TestCase):
    """The middleware's own contract, without the whole request stack."""

    def test_non_agent_path_is_untouched(self):
        calls = []
        mw = RelayGateMiddleware(lambda request: calls.append(request) or "ok")

        class Fake:
            path = "/api/v1/hosts/"
            META = {}  # noqa: RUF012 — a read-only stand-in for request.META

        self.assertEqual(mw(Fake()), "ok")
        self.assertEqual(len(calls), 1)


class RelayStepUpTests(TestCase):
    """Moving a relay, switching one on or off, or deleting it needs a fresh code."""

    def setUp(self):
        self.client = APIClient()
        admin = get_user_model().objects.create_user(username="admin")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_authenticate(admin)
        self.remote = make_relay(enabled=False)

    def test_url_enabled_and_delete_need_totp(self):
        with patch("apps.relays.views.require_totp_confirmation", return_value="bad code"):
            for body in ({"url": "https://evil.example.com"}, {"enabled": True}):
                resp = self.client.patch(f"/api/v1/relays/{self.remote.id}/", body, format="json")
                self.assertEqual(resp.status_code, 400, body)
            self.assertEqual(self.client.delete(f"/api/v1/relays/{self.remote.id}/").status_code, 400)
            renamed = self.client.patch(f"/api/v1/relays/{self.remote.id}/", {"name": "edge-2"},
                                        format="json")
        self.assertEqual(renamed.status_code, 200)
        self.remote.refresh_from_db()
        self.assertEqual(self.remote.url, "https://relay.example.com")
        self.assertFalse(self.remote.enabled)
