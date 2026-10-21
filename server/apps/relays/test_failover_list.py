"""The signed fallback list the check-in response carries (QA-54b).

A relay carries check-in responses, so the relay list is only safe to hand out if
the relay cannot edit it. Everything here is about that: the payload names the
agent it was signed for, and the agent verifies before storing an address.
"""

import base64
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from nacl.exceptions import BadSignatureError
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from vigil.signing import get_signing_key

from .models import Relay, relay_urls, signed_relay_list


def make_relay(name, url, priority=100, kind=Relay.Kind.REMOTE, enabled=True):
    return Relay.objects.create(name=name, url=url, kind=kind, enabled=enabled,
                                priority=priority)


class RelayUrlOrderTests(TestCase):
    def test_priority_orders_the_list_and_name_breaks_ties(self):
        make_relay("charlie", "https://c.example.com")          # default 100
        make_relay("delta", "https://d.example.com", priority=1)
        make_relay("bravo", "https://b.example.com", priority=50)
        make_relay("alpha", "https://a.example.com", priority=50)
        self.assertEqual(relay_urls(), [
            "https://d.example.com",
            "https://a.example.com",
            "https://b.example.com",
            "https://c.example.com",
        ])

    def test_disabled_and_keyless_rows_stay_out(self):
        make_relay("off", "https://off.example.com", priority=1, enabled=False)
        make_relay("keyless", "", priority=1)
        make_relay("on", "https://on.example.com", priority=5)
        self.assertEqual(relay_urls(), ["https://on.example.com"])


class SignedRelayListTests(TestCase):
    def test_payload_is_the_list_with_a_signature_that_verifies(self):
        make_relay("edge", "https://edge.example.com", priority=5)
        with patch("apps.relays.models.direct_allowed", return_value=False):
            body = signed_relay_list("f" * 64)
        self.assertEqual(body["relays"], ["https://edge.example.com"])
        self.assertFalse(body["direct"])

        payload = {"v": 1, "type": "relay-list", "issued": body["relays_issued"], "agent": "f" * 64, "relays": body["relays"],
                   "direct": body["direct"]}
        get_signing_key().verify_key.verify(
            json.dumps(payload, sort_keys=True).encode(),
            base64.b64decode(body["relays_sig"]))

    def test_a_signature_cannot_be_moved_to_another_agent(self):
        body = signed_relay_list("f" * 64)
        other = {"v": 1, "type": "relay-list", "issued": body["relays_issued"], "agent": "e" * 64, "relays": body["relays"],
                 "direct": body["direct"]}
        with self.assertRaises(BadSignatureError):
            get_signing_key().verify_key.verify(
                json.dumps(other, sort_keys=True).encode(),
                base64.b64decode(body["relays_sig"]))


class CheckinRelayListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="h1", agent_token="a" * 64,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)

    def _checkin(self):
        return self.client.post(
            "/api/v1/checkin",
            {"hostname": self.host.hostname},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")

    def test_checkin_signature_covers_this_host_and_the_relay_order(self):
        make_relay("zulu", "https://z.example.com", priority=90)
        make_relay("alpha", "https://a.example.com", priority=10)

        body = self._checkin().json()
        self.assertEqual(body["relays"],
                         ["https://a.example.com", "https://z.example.com"])
        self.assertTrue(body["direct"])

        payload = {"v": 1, "type": "relay-list", "issued": body["relays_issued"], "agent": self.host.token_fingerprint,
                   "relays": body["relays"], "direct": body["direct"]}
        get_signing_key().verify_key.verify(
            json.dumps(payload, sort_keys=True).encode(),
            base64.b64decode(body["relays_sig"]))

    def test_a_signature_issued_for_another_host_does_not_verify_here(self):
        other = Host.objects.create(
            hostname="h2", agent_token="b" * 64,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        body = self._checkin().json()

        payload = {"v": 1, "type": "relay-list", "issued": body["relays_issued"], "agent": other.token_fingerprint,
                   "relays": body["relays"], "direct": body["direct"]}
        with self.assertRaises(BadSignatureError):
            get_signing_key().verify_key.verify(
                json.dumps(payload, sort_keys=True).encode(),
                base64.b64decode(body["relays_sig"]))


class RelayPriorityApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        admin = get_user_model().objects.create_user(username="admin")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_authenticate(admin)

    def test_priority_is_in_the_listing(self):
        make_relay("edge", "https://edge.example.com", priority=7)
        row = self.client.get("/api/v1/relays/").json()["relays"]
        self.assertEqual([r["priority"] for r in row if r["name"] == "edge"], [7])

    def test_patch_priority_needs_no_second_factor(self):
        remote = make_relay("edge", "https://edge.example.com")
        with patch("apps.relays.views.require_totp_confirmation",
                   side_effect=AssertionError("TOTP must not be required")):
            resp = self.client.patch(f"/api/v1/relays/{remote.id}/",
                                     {"priority": 3}, format="json")
        self.assertEqual(resp.status_code, 200)
        remote.refresh_from_db()
        self.assertEqual(remote.priority, 3)

    def test_priority_out_of_range_is_refused(self):
        remote = make_relay("edge", "https://edge.example.com", priority=40)
        resp = self.client.patch(f"/api/v1/relays/{remote.id}/",
                                 {"priority": 1001}, format="json")
        self.assertEqual(resp.status_code, 400)
        remote.refresh_from_db()
        self.assertEqual(remote.priority, 40)

    def test_create_accepts_a_priority(self):
        with (
            patch("apps.relays.views.require_totp_confirmation", return_value=None),
            patch("apps.relays.views.relays_allowed", return_value=6),
        ):
            resp = self.client.post("/api/v1/relays/",
                                    {"name": "edge", "url": "https://e.example.com",
                                     "priority": 12}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["priority"], 12)


class RelayListFreshnessTests(TestCase):
    def test_the_list_carries_a_signed_issue_time(self):
        with patch("apps.relays.models.time.time", return_value=1_800_000_000.7):
            body = signed_relay_list("f" * 64)
        self.assertEqual(body["relays_issued"], 1_800_000_000)
        stale = {"v": 1, "type": "relay-list", "issued": 1_700_000_000,
                 "agent": "f" * 64, "relays": body["relays"], "direct": body["direct"]}
        with self.assertRaises(BadSignatureError):
            get_signing_key().verify_key.verify(
                json.dumps(stale, sort_keys=True).encode(),
                base64.b64decode(body["relays_sig"]))

