"""Tickets: what makes one acceptable to an agent, and what does not."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from vigil.signing import agent_fingerprint, get_public_key_b64, get_signing_key

from ..tickets import TICKET_TTL_SECONDS, make_ticket, verify_ticket

TOKEN = "a" * 64


def fake_session(kind="shell", container=""):
    """A stand-in for the row: ``make_ticket`` only reads these five things."""
    return SimpleNamespace(
        id="11111111-2222-3333-4444-555555555555",
        host_id="66666666-7777-8888-9999-000000000000",
        kind=kind,
        container=container,
        nonce="ab" * 16,
        host=SimpleNamespace(token_fingerprint=agent_fingerprint(TOKEN)),
    )


class TicketTests(TestCase):
    def setUp(self):
        self.key = get_public_key_b64()

    def verify(self, ticket, token=TOKEN, now=None):
        return verify_ticket(ticket, self.key, token, now)

    def test_round_trip_accepts_a_fresh_ticket_for_the_right_agent(self):
        session = fake_session(kind="container", container="web-1")
        body = self.verify(make_ticket(session))
        self.assertEqual(body["sid"], str(session.id))
        self.assertEqual(body["kind"], "container")
        self.assertEqual(body["container"], "web-1")
        self.assertEqual(body["v"], 1)
        self.assertGreater(body["exp"], time.time())

    def test_ticket_expiry_is_the_short_window_a_stale_wake_cannot_reuse(self):
        body = self.verify(make_ticket(fake_session()))
        self.assertLessEqual(body["exp"] - time.time(), TICKET_TTL_SECONDS)

    def test_changing_the_kind_after_signing_fails_the_signature(self):
        ticket = make_ticket(fake_session())
        # The obvious forgery: keep a valid signature, ask for something else.
        ticket["kind"] = "rdp"
        with self.assertRaises(ValueError):
            self.verify(ticket)

    def test_a_ticket_signed_for_another_agent_is_refused_here(self):
        ticket = make_ticket(fake_session())
        with self.assertRaises(ValueError) as caught:
            self.verify(ticket, token="b" * 64)
        self.assertIn("not for this agent", str(caught.exception))

    def test_a_ticket_past_its_expiry_is_refused(self):
        ticket = make_ticket(fake_session())
        with self.assertRaises(ValueError) as caught:
            self.verify(ticket, now=ticket["exp"] + 1)
        self.assertIn("expired", str(caught.exception))

    def test_another_version_is_refused_rather_than_guessed(self):
        ticket = reissue(fake_session(), v=2)
        with self.assertRaises(ValueError) as caught:
            self.verify(ticket)
        self.assertIn("version", str(caught.exception))

    def test_an_unknown_kind_never_becomes_a_command_on_the_host(self):
        # Signed correctly, and still refused: the kind is what the agent runs.
        ticket = reissue(fake_session(), kind="exec")
        with self.assertRaises(ValueError) as caught:
            self.verify(ticket)
        self.assertIn("kind", str(caught.exception))

    def test_verification_uses_the_same_key_the_agent_already_has(self):
        # QA-57 verifies tickets with the v2 task-signing key; if this drifted,
        # the agent would need new configuration and the docs would be wrong.
        with patch("apps.remote.tickets.get_public_key_b64", return_value="k"):
            from ..tickets import public_key_b64

            self.assertEqual(public_key_b64(), "k")


def reissue(session, **changes) -> dict:
    """A ticket as it would look if *changes* had been in it when Vigil signed
    it — which is how to test a rejection that happens after the signature has
    already verified, rather than a forgery the signature itself catches."""
    body = {k: v for k, v in make_ticket(session).items() if k != "sig"}
    body.update(changes)
    signature = base64.b64encode(
        get_signing_key().sign(json.dumps(body, sort_keys=True).encode()).signature
    ).decode()
    return {**body, "sig": signature}
