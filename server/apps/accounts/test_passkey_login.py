"""Signing in with a passkey alone: no username, no password, no second step.

Nothing is patched here. ``SoftPasskey`` is a real P-256 authenticator that
signs real assertions, so py_webauthn checks the signature, the challenge, the
origin, the rp id hash and the user-verified flag exactly as it does for a
browser. The rules:

  1. only a user-verified assertion signs in, because the passkey stands in
     for the password and the second factor together;
  2. the key must be enrolled to an active account, and the user handle it
     returns must name that account;
  3. the challenge is single-use and lives in this browser's session only;
  4. every refusal reads the same, and each one counts against the sign-in
     page's per-address limit;
  5. ``next`` is followed only to this site.
"""
import hashlib
import json
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from webauthn.helpers import bytes_to_base64url

from apps.accounts.models import LoginAttempt, SecurityKey
from apps.accounts.views import MAX_FAILURES_PER_IP
from apps.accounts.webauthn_keys import PASSKEY_REFUSED

ORIGIN = "https://vigil.example.com"
RP_ID = "vigil.example.com"
SECURE = override_settings(VIGIL_PUBLIC_URL=ORIGIN)

UP, UV = 0x01, 0x04


class SoftPasskey:
    """A discoverable credential held by a pretend platform authenticator."""

    def __init__(self, user, credential_id: bytes = b"soft-passkey-1"):
        self.user = user
        self.credential_id = credential_id
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.count = 0

    def enrol(self) -> SecurityKey:
        numbers = self.private.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})
        return SecurityKey.objects.create(user=self.user, name="Laptop",
                                          credential_id=self.credential_id,
                                          public_key=cose, sign_count=0)

    def assertion(self, challenge: str, *, flags=UP | UV, origin=ORIGIN, rp_id=RP_ID,
                  handle: bytes | None = None) -> dict:
        self.count += 1
        auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
                     + struct.pack(">I", self.count))
        client_data = json.dumps({"type": "webauthn.get", "challenge": challenge,
                                  "origin": origin, "crossOrigin": False}).encode()
        signature = self.private.sign(auth_data + hashlib.sha256(client_data).digest(),
                                      ec.ECDSA(hashes.SHA256()))
        handle = str(self.user.id).encode() if handle is None else handle
        raw = bytes_to_base64url(self.credential_id)
        return {"id": raw, "rawId": raw, "type": "public-key", "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "authenticatorData": bytes_to_base64url(auth_data),
            "signature": bytes_to_base64url(signature),
            "userHandle": bytes_to_base64url(handle) if handle else None,
        }}


@SECURE
class PasskeySignInTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("connor", password="pw-unused-here")
        self.passkey = SoftPasskey(self.user)
        self.passkey.enrol()

    def options(self, client=None):
        resp = (client or self.client).post("/login/passkey/options/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def sign_in(self, credential, client=None, next_url=""):
        return (client or self.client).post(
            "/login/passkey/", json.dumps({"credential": credential, "next": next_url}),
            content_type="application/json")

    def signed_in_as(self, client=None):
        return (client or self.client).session.get("_auth_user_id")

    def test_the_page_offers_a_passkey_once_one_is_enrolled(self):
        self.assertContains(self.client.get("/login/"), "Sign in with a passkey")
        SecurityKey.objects.all().delete()
        self.assertNotContains(self.client.get("/login/"), "Sign in with a passkey")

    @override_settings(VIGIL_PUBLIC_URL="http://vigil.example.com")
    def test_no_passkey_offer_without_a_secure_context(self):
        self.assertNotContains(self.client.get("/login/"), "Sign in with a passkey")
        self.assertEqual(self.client.post("/login/passkey/options/").status_code, 400)

    def test_options_name_no_account_and_require_user_verification(self):
        opts = self.options()
        self.assertEqual(opts["rpId"], RP_ID)
        self.assertEqual(opts["userVerification"], "required")
        self.assertEqual(opts.get("allowCredentials", []), [])

    def test_a_verified_passkey_signs_in_without_password_or_second_step(self):
        challenge = self.options()["challenge"]
        resp = self.sign_in(self.passkey.assertion(challenge), next_url="/hosts/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"next": "/hosts/"})
        self.assertEqual(self.signed_in_as(), str(self.user.pk))
        key = SecurityKey.objects.get()
        self.assertEqual(key.sign_count, 1)
        self.assertIsNotNone(key.last_used_at)

    def test_presence_without_user_verification_is_refused(self):
        challenge = self.options()["challenge"]
        resp = self.sign_in(self.passkey.assertion(challenge, flags=UP))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], PASSKEY_REFUSED)
        self.assertIsNone(self.signed_in_as())

    def test_the_user_handle_must_name_the_keys_owner(self):
        other = get_user_model().objects.create_user("mallory", password="x")
        for handle in (str(other.id).encode(), b""):
            with self.subTest(handle=handle):
                challenge = self.options()["challenge"]
                resp = self.sign_in(self.passkey.assertion(challenge, handle=handle))
                self.assertEqual(resp.json()["error"], PASSKEY_REFUSED)
                self.assertIsNone(self.signed_in_as())

    def test_a_disabled_account_or_unknown_key_reads_the_same(self):
        stranger = SoftPasskey(self.user, credential_id=b"never-enrolled")
        resp = self.sign_in(stranger.assertion(self.options()["challenge"]))
        self.assertEqual(resp.json()["error"], PASSKEY_REFUSED)
        self.user.is_active = False
        self.user.save()
        resp = self.sign_in(self.passkey.assertion(self.options()["challenge"]))
        self.assertEqual(resp.json()["error"], PASSKEY_REFUSED)
        self.assertIsNone(self.signed_in_as())

    def test_a_signature_from_another_key_is_refused(self):
        impostor = SoftPasskey(self.user)  # same credential id, different private key
        resp = self.sign_in(impostor.assertion(self.options()["challenge"]))
        self.assertEqual(resp.json()["error"], PASSKEY_REFUSED)
        self.assertIsNone(self.signed_in_as())

    def test_the_wrong_origin_or_rp_id_is_refused(self):
        for kw in ({"origin": "https://evil.example.net"}, {"rp_id": "evil.example.net"}):
            with self.subTest(**kw):
                resp = self.sign_in(self.passkey.assertion(self.options()["challenge"], **kw))
                self.assertEqual(resp.status_code, 400)
                self.assertIsNone(self.signed_in_as())

    def test_a_challenge_is_spent_by_its_first_use_and_bound_to_its_session(self):
        challenge = self.options()["challenge"]
        self.assertEqual(self.sign_in(self.passkey.assertion(challenge, flags=UP)).status_code, 400)
        # The same challenge again, now properly verified: already spent.
        self.assertEqual(self.sign_in(self.passkey.assertion(challenge)).status_code, 400)
        self.assertIsNone(self.signed_in_as())
        # A challenge issued to one browser does nothing in another.
        other = self.client_class()
        challenge = self.options()["challenge"]
        self.assertEqual(self.sign_in(self.passkey.assertion(challenge), client=other).status_code, 400)
        self.assertIsNone(self.signed_in_as(other))

    def test_refusals_count_against_the_address_and_lock_it_out(self):
        for _ in range(MAX_FAILURES_PER_IP):
            self.sign_in(self.passkey.assertion(self.options()["challenge"], flags=UP))
        self.assertEqual(LoginAttempt.objects.filter(username="").count(), MAX_FAILURES_PER_IP)
        resp = self.sign_in(self.passkey.assertion(self.options()["challenge"]))
        self.assertEqual(resp.status_code, 429)
        self.assertIsNone(self.signed_in_as())

    def test_next_only_leads_back_to_this_site(self):
        resp = self.sign_in(self.passkey.assertion(self.options()["challenge"]),
                            next_url="https://evil.example.net/")
        self.assertEqual(resp.json(), {"next": "/"})

    def test_a_cloned_authenticator_is_refused_by_its_sign_count(self):
        self.assertEqual(self.sign_in(self.passkey.assertion(self.options()["challenge"])).status_code, 200)
        self.client.logout()
        self.passkey.count = 0  # a copy that never saw the first use
        resp = self.sign_in(self.passkey.assertion(self.options()["challenge"]))
        self.assertEqual(resp.json()["error"], PASSKEY_REFUSED)

    def test_the_options_and_sign_in_need_csrf_like_the_password_form(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post("/login/passkey/options/").status_code, 403)
        self.assertEqual(self.sign_in({}, client=strict).status_code, 403)

    def test_a_malformed_credential_is_refused_cleanly(self):
        self.options()
        for bad in (None, "x", {"id": "AAAA"}, {"id": "AAAA", "response": "x"}):
            with self.subTest(bad=bad):
                self.options()
                resp = self.sign_in(bad)
                self.assertEqual(resp.status_code, 400, resp.content)
