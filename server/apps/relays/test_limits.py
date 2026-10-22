"""Free gets one relay — the built-in one or one remote — however it got there.

The limit is checked when a relay is added or turned on, and enforced again on
every request: relays past it (after a Business license lapses, say) carry no
agent traffic and are handed to no agent as a fallback.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from .link import load_targets
from .models import Relay, generate_key, in_service, relay_urls, set_key
from .tests import checkin
from .trust import link_token


def _remote(name, priority=100, enabled=True, seen=True):
    relay = Relay(name=name, url=f"https://{name}.example.com", kind=Relay.Kind.REMOTE,
                  priority=priority, enabled=enabled,
                  last_seen_at=now() - timedelta(minutes=1) if seen else None)
    set_key(relay, generate_key())
    relay.save()
    return relay


def _builtin():
    return Relay.objects.get(kind=Relay.Kind.BUILTIN)


def _set_builtin(on):
    b = _builtin()
    b.enabled = on
    b.save(update_fields=["enabled"])


class FreeSwitchTests(TestCase):
    """Moving Free from the built-in relay to its one remote, through the API."""

    def setUp(self):
        self.client = APIClient()
        admin = get_user_model().objects.create_user("admin", password="pw")
        UserProfile.objects.update_or_create(user=admin, defaults={"role": Role.ADMIN})
        self.client.force_authenticate(admin)
        patcher = patch("apps.relays.views.require_totp_confirmation", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_free_can_switch_to_a_remote_and_ends_with_one(self):
        created = self.client.post("/api/v1/relays/", {"name": "edge", "url": "https://edge.example.com"},
                                   format="json")
        self.assertEqual(created.status_code, 201)
        edge = Relay.objects.get(name="edge")
        self.assertEqual(in_service(), [_builtin()])
        # The link reaches it so it can prove it works, but agents cannot use it yet.
        self.assertEqual([t.id for t in load_targets()], [str(edge.id)])
        self.assertEqual(relay_urls(), [])

        Relay.objects.filter(pk=edge.pk).update(last_seen_at=now())
        off = self.client.patch(f"/api/v1/relays/{_builtin().id}/", {"enabled": False}, format="json")
        self.assertEqual(off.status_code, 200)
        self.assertEqual(in_service(), [edge])
        self.assertEqual(relay_urls(), ["https://edge.example.com"])

        back_on = self.client.patch(f"/api/v1/relays/{_builtin().id}/", {"enabled": True}, format="json")
        self.assertEqual(back_on.status_code, 402)
        self.assertEqual(len(in_service()), 1)

    def test_free_cannot_turn_on_a_second_remote(self):
        _set_builtin(False)
        _remote("edge")
        spare = _remote("spare", enabled=False)
        resp = self.client.patch(f"/api/v1/relays/{spare.id}/", {"enabled": True}, format="json")
        self.assertEqual(resp.status_code, 402)


class OverTheLimitTests(TestCase):
    """Relays that exist past the limit, e.g. after Business lapses to Free."""

    def setUp(self):
        _set_builtin(False)
        self.first = _remote("first", priority=10)
        self.second = _remote("second", priority=20)
        self.third = _remote("third", priority=30)
        self.host = Host.objects.create(hostname="h1", agent_token="a" * 64,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)

    def _through(self, relay):
        return checkin(self.client, self.host, HTTP_X_VIGIL_LINK=link_token(),
                       HTTP_X_VIGIL_RELAY=str(relay.id), HTTP_X_VIGIL_AGENT_IP="198.51.100.9")

    def test_only_the_first_in_fallback_order_serves(self):
        self.assertEqual(in_service(), [self.first])
        self.assertEqual(relay_urls(), ["https://first.example.com"])

    def test_the_gate_refuses_relays_past_the_limit(self):
        self.assertEqual(self._through(self.first).status_code, 200)
        self.assertEqual(self._through(self.second).status_code, 403)
        self.assertEqual(self._through(self.third).status_code, 403)

    def test_the_built_in_relay_takes_the_slot_when_it_is_on(self):
        _set_builtin(True)
        self.assertEqual(in_service(), [_builtin()])
        self.assertEqual(self._through(self.first).status_code, 403)
        self.assertEqual(relay_urls(), [])

    def test_business_serves_seats_plus_two(self):
        with patch("vigil.licensing.relays_allowed", return_value=2):
            self.assertEqual(in_service(), [self.first, self.second])
            self.assertEqual(self._through(self.second).status_code, 200)
            self.assertEqual(self._through(self.third).status_code, 403)
