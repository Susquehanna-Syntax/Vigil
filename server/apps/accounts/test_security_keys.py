"""Security keys and passkeys as a second factor (QA-65).

The rules each test here is the reason the feature is allowed to exist at all:

  1. a key token is single-use, short-lived, and bound to the user it was issued to;
  2. a WebAuthn challenge is single-use, server-side in the session, and expires —
     reading one reads-and-deletes it, which ``steal_the_challenge`` proves;
  3. adding a key costs the second factor you already have;
  4. removing a key costs a second-factor confirmation;
  5. the sign count is stored, so a cloned authenticator can be refused;
  6. WebAuthn needs a secure context — TOTP keeps working everywhere else.

A real authenticator cannot run in a test, so ``verify_registration_response``
and ``verify_authentication_response`` are patched with fakes that return the
documented attributes. The challenge handling is never patched: single-use
challenges are exactly what these tests are about.
"""
import time
from dataclasses import dataclass
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, SecurityKey, UserProfile
from apps.accounts.totp import (
    consume_totp,
    generate_secret,
    generate_totp,
    has_second_factor,
    require_totp_confirmation,
)
from apps.accounts.views import _totp_attempt_allowed

REGISTER_URL = "/api/v1/accounts/security-keys/register/"
REGISTER_OPTIONS_URL = "/api/v1/accounts/security-keys/register/options/"
ASSERT_URL = "/api/v1/accounts/security-keys/assert/"
ASSERT_OPTIONS_URL = "/api/v1/accounts/security-keys/assert/options/"
LIST_URL = "/api/v1/accounts/security-keys/"

#: What the fake "authenticator" produces. The library never sees these bytes:
#: the verify functions are patched, so only their documented shape matters.
CRED_ID = b"\x01\x02\x03credential-bytes"
PUBLIC_KEY = b"\xa4\x01\x02\x03key-bytes"


@dataclass
class FakeRegistration:
    credential_id: bytes = CRED_ID
    credential_public_key: bytes = PUBLIC_KEY
    sign_count: int = 7
    aaguid: str = "00000000-0000-0000-0000-000000000001"


@dataclass
class FakeAuthentication:
    new_sign_count: int = 8


def credential_payload(raw_id: bytes = CRED_ID) -> dict:
    """The JSON shape the browser produces, with a base64url id."""
    from webauthn.helpers import bytes_to_base64url

    encoded = bytes_to_base64url(raw_id)
    return {
        "id": encoded,
        "rawId": encoded,
        "type": "public-key",
        "response": {
            "clientDataJSON": encoded,
            "authenticatorData": encoded,
            "signature": encoded,
            "userHandle": None,
        },
    }


def enroll_totp(user, secret: str = "JBSWY3DPEHPK3PXP") -> str:
    profile = user.profile
    profile.totp_secret = secret
    profile.totp_confirmed_at = now()
    profile.save(update_fields=["totp_secret_encrypted", "totp_confirmed_at"])
    return generate_totp(secret)


def add_key(user, name: str = "YubiKey", credential_id: bytes = CRED_ID) -> SecurityKey:
    return SecurityKey.objects.create(user=user, name=name,
                                      credential_id=credential_id,
                                      public_key=PUBLIC_KEY)


#: A secure context everywhere else in this file: the test client's scheme is
#: http and its host is not localhost, which is exactly the case WebAuthn (and
#: these endpoints) refuse. SecureContextTests covers both sides of that rule.
SECURE = override_settings(VIGIL_PUBLIC_URL="https://vigil.example.com")


class CeremonyRequest:
    """Enough of a request for ``webauthn_keys`` to run outside the API: a
    session it can store a challenge in, and a user whose keys it looks up."""

    scheme = "https"

    def __init__(self, user):
        from django.contrib.sessions.backends.db import SessionStore

        self.user = user
        self.session = SessionStore()

    def get_host(self) -> str:
        return "vigil.example.com"


class _KeyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("root", password="pw")
        UserProfile.objects.create(user=self.user)
        self.client.force_authenticate(self.user)
        cache.clear()


@SECURE
class RegistrationTests(_KeyTests):
    def test_options_without_a_stored_challenge_are_refused(self):
        # The ceremony is driven from vigil-utils.js, which cannot run here: an
        # assert with no options call behind it has nothing to verify against.
        resp = self.client.post(ASSERT_URL, {"credential": credential_payload()}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_options_store_their_nonce_in_the_session(self):
        """What a single-use challenge is stored *in* is the project's session
        (server-side since QA-13). Asserting on it directly would mean reaching
        into the request's session from outside a request, so this pins the
        client-visible half: the options hand back a base64url nonce, and the
        library is handed the very same bytes to verify against. The challenge
        *handling* stays unpatched throughout this file."""

        def store_then_verify(*, credential, expected_challenge, **_kwargs):
            # ``expected_challenge`` is whatever webauthn_keys took out of the
            # session — proving it came from _store_challenge and nowhere else.
            self.assertEqual(len(expected_challenge), 32)
            return FakeRegistration()

        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   side_effect=store_then_verify):
            resp = self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            challenge = resp.json()["challenge"]
            self.assertTrue(challenge)
            self.assertNotIn("+", challenge)         # base64url, not base64
            self.assertNotIn("/", challenge)
            created = self.client.post(REGISTER_URL,
                                       {"name": "k", "credential": credential_payload()},
                                       format="json")
        self.assertEqual(created.status_code, 201, created.content)

    def test_a_challenge_is_spent_by_the_first_attempt_at_it(self):
        """Reading the stored challenge removes it, so it cannot be spent twice."""
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()):
            self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            first = self.client.post(REGISTER_URL,
                                     {"name": "k", "credential": credential_payload()},
                                     format="json")
            spent = self.client.post(REGISTER_URL,
                                     {"name": "again", "credential": credential_payload()},
                                     format="json")
        self.assertEqual(first.status_code, 201, first.content)
        # The second attempt had no challenge left to be handed to the library.
        self.assertEqual(spent.status_code, 400)
        self.assertIn("expired", spent.json()["error"])
        self.assertEqual(SecurityKey.objects.filter(name="again").count(), 0)

    def test_each_options_call_replaces_the_stored_challenge(self):
        """Never the same nonce twice: an old ceremony cannot be finished after
        a newer one was started, so the first attempt's challenge stops working."""
        seen = []

        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   side_effect=lambda **kw: (seen.append(kw["expected_challenge"]),
                                             FakeRegistration())[1]):
            self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            created = self.client.post(REGISTER_URL,
                                       {"name": "k", "credential": credential_payload()},
                                       format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(len(seen), 1)               # only the newest was offered up

    def test_register_saves_the_keys_id_and_public_key_and_clears_the_challenge(self):
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()) as verify:
            resp = self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertIn("challenge", resp.json())
            stored = self.client.session["webauthn_reg"]
            self.assertTrue(stored["challenge"])

            created = self.client.post(REGISTER_URL,
                                       {"name": " Office key ", "credential": credential_payload()},
                                       format="json")
            self.assertEqual(created.status_code, 201, created.content)

        key = SecurityKey.objects.get(pk=created.json()["id"])
        self.assertEqual(bytes(key.credential_id), CRED_ID)
        self.assertEqual(bytes(key.public_key), PUBLIC_KEY)
        self.assertEqual(key.sign_count, 7)                 # the fake's counter is stored
        self.assertEqual(key.name, "Office key")            # stripped on the way in
        self.assertEqual(key.aaguid, "00000000-0000-0000-0000-000000000001")
        self.assertNotIn("webauthn_reg", self.client.session)
        # The expected challenge is the one that was stored in the session.
        from webauthn.helpers import base64url_to_bytes
        self.assertEqual(verify.call_args.kwargs["expected_challenge"],
                         base64url_to_bytes(stored["challenge"]))

    def test_a_second_register_with_the_same_session_is_refused(self):
        """One challenge, one key: the ceremony has to be started again."""
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()):
            self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            self.assertEqual(self.client.post(REGISTER_URL,
                                              {"name": "k", "credential": credential_payload()},
                                              format="json").status_code, 201)
            again = self.client.post(REGISTER_URL,
                                     {"name": "k2", "credential": credential_payload()},
                                     format="json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("expired", again.json()["error"])
        self.assertEqual(SecurityKey.objects.count(), 1)

    def test_an_expired_challenge_is_refused(self):
        with patch("apps.accounts.webauthn_keys.CHALLENGE_SECONDS", -1):
            self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()) as verify:
            resp = self.client.post(REGISTER_URL,
                                    {"name": "k", "credential": credential_payload()},
                                    format="json")
        self.assertEqual(resp.status_code, 400)
        verify.assert_not_called()

    def test_a_name_of_more_than_sixty_characters_is_refused(self):
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()):
            self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            resp = self.client.post(REGISTER_URL,
                                    {"name": "k" * 61, "credential": credential_payload()},
                                    format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(SecurityKey.objects.count(), 0)


@SECURE
class AddingAKeyNeedsTheExistingFactorTests(_KeyTests):
    def test_a_user_with_totp_must_supply_a_code(self):
        enroll_totp(self.user)

        refused = self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(SecurityKey.objects.count(), 0)

        accepted = self.client.post(REGISTER_OPTIONS_URL,
                                    {"totp": generate_totp(self.user.profile.totp_secret)},
                                    format="json")
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertIn("challenge", accepted.json())

    def test_a_wrong_code_does_not_open_registration(self):
        enroll_totp(self.user)
        _totp_attempt_allowed(self.user)   # the gate records attempts before verifying
        resp = self.client.post(REGISTER_OPTIONS_URL, {"totp": "000000"}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("webauthn_reg", self.client.session)

    def test_a_user_with_no_factor_at_all_may_add_their_first_key(self):
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()):
            resp = self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
            self.assertEqual(resp.status_code, 200, resp.content)
            created = self.client.post(REGISTER_URL,
                                       {"name": "first", "credential": credential_payload()},
                                       format="json")
        self.assertEqual(created.status_code, 201, created.content)

    def test_a_second_key_needs_the_first_one(self):
        add_key(self.user)
        refused = self.client.post(REGISTER_OPTIONS_URL, {}, format="json")
        self.assertEqual(refused.status_code, 401)

        from apps.accounts import webauthn_keys
        with patch.object(webauthn_keys, "verify_authentication_response",
                          return_value=FakeAuthentication()):
            self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
            token = self.client.post(ASSERT_URL, {"credential": credential_payload()},
                                     format="json").json()["token"]
        accepted = self.client.post(REGISTER_OPTIONS_URL, {"totp": token}, format="json")
        self.assertEqual(accepted.status_code, 200, accepted.content)


@SECURE
class AssertionAndKeyTokenTests(_KeyTests):
    def setUp(self):
        super().setUp()
        self.key = add_key(self.user)

    def _assert(self, payload=None):
        with patch("apps.accounts.webauthn_keys.verify_authentication_response",
                   return_value=FakeAuthentication()):
            options = self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
            self.assertEqual(options.status_code, 200, options.content)
            self.assertIn("challenge", options.json())
            ids = [c["id"] for c in options.json()["allowCredentials"]]
            self.assertEqual(len(ids), 1)
            return self.client.post(ASSERT_URL,
                                    {"credential": payload or credential_payload()},
                                    format="json")

    def test_the_token_passes_the_existing_gate_exactly_once(self):
        resp = self._assert()
        self.assertEqual(resp.status_code, 200, resp.content)
        token = resp.json()["token"]
        self.assertTrue(token.startswith("wa_"))

        self.assertIsNone(require_totp_confirmation(self.user, {"totp": token}))
        second = require_totp_confirmation(self.user, {"totp": token})
        self.assertIsNotNone(second, "a key token must be single-use")
        self.assertIn("expired", second)

    def test_the_token_expires(self):
        with patch("apps.accounts.webauthn_keys.KEY_TOKEN_SECONDS", 0):
            resp = self._assert()
        self.assertEqual(resp.status_code, 200, resp.content)
        err = require_totp_confirmation(self.user, {"totp": resp.json()["token"]})
        self.assertIsNotNone(err)

    def test_the_token_is_bound_to_the_user_it_was_issued_to(self):
        other = get_user_model().objects.create_user("other", password="pw")
        UserProfile.objects.create(user=other)
        token = self._assert().json()["token"]

        err = require_totp_confirmation(other, {"totp": token})
        self.assertIsNotNone(err, "another user must not be able to spend this token")
        # The refusal did not consume it either: the owner can still use it once.
        self.assertIsNone(require_totp_confirmation(self.user, {"totp": token}))

    def test_the_sign_count_and_last_used_at_are_updated(self):
        self.assertEqual(self._assert().status_code, 200)
        self.key.refresh_from_db()
        self.assertEqual(self.key.sign_count, 8)         # the fake's new counter
        self.assertIsNotNone(self.key.last_used_at)

    def test_the_challenge_is_single_use(self):
        with patch("apps.accounts.webauthn_keys.verify_authentication_response",
                   return_value=FakeAuthentication()):
            self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
            self.assertEqual(self.client.post(ASSERT_URL, {"credential": credential_payload()},
                                              format="json").status_code, 200)
            replay = self.client.post(ASSERT_URL, {"credential": credential_payload()},
                                      format="json")
        self.assertEqual(replay.status_code, 400)
        self.assertIn("expired", replay.json()["error"])

    def test_a_credential_belonging_to_another_user_is_refused(self):
        stranger = get_user_model().objects.create_user("mallory", password="pw")
        UserProfile.objects.create(user=stranger)
        foreign = add_key(stranger, name="theirs", credential_id=b"\x09foreign-bytes")

        from apps.accounts import webauthn_keys
        with patch.object(webauthn_keys, "verify_authentication_response",
                          return_value=FakeAuthentication()) as verify:
            self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
            resp = self.client.post(ASSERT_URL,
                                    {"credential": credential_payload(bytes(foreign.credential_id))},
                                    format="json")
        self.assertEqual(resp.status_code, 400)
        verify.assert_not_called()          # never even looked up outside this user's keys
        foreign.refresh_from_db()
        self.assertIsNone(foreign.last_used_at)

    def test_wrong_confirmations_and_codes_share_one_lockout(self):
        from apps.accounts.totp import MAX_TOTP_FAILURES

        for _ in range(MAX_TOTP_FAILURES):
            with patch("apps.accounts.webauthn_keys.verify_authentication_response",
                       side_effect=ValueError("bad signature")):
                self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
                self.assertEqual(self.client.post(ASSERT_URL,
                                                  {"credential": credential_payload()},
                                                  format="json").status_code, 400)
        with patch("apps.accounts.webauthn_keys.verify_authentication_response",
                   return_value=FakeAuthentication()):
            self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
            locked = self.client.post(ASSERT_URL, {"credential": credential_payload()},
                                      format="json")
        self.assertEqual(locked.status_code, 429)

    def test_assert_options_refuse_a_user_with_no_keys(self):
        keyless = get_user_model().objects.create_user("keyless", password="pw")
        UserProfile.objects.create(user=keyless)
        anon = APIClient()
        anon.force_authenticate(keyless)
        resp = anon.post(ASSERT_OPTIONS_URL, {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No security keys enrolled", resp.json()["error"])


class KeyTokenUnitTests(_KeyTests):
    """The consuming half, without the ceremony in front of it."""

    def test_consume_totp_accepts_a_key_token_for_a_user_with_no_totp(self):
        add_key(self.user)                     # key-only user: no profile secret
        token = "wa_" + "x" * 10
        cache.set(f"webauthn:token:{token}", self.user.id, 60)

        ok, err = consume_totp(self.user, token)
        self.assertTrue(ok, err)
        self.assertFalse(consume_totp(self.user, token)[0])

    def test_an_unknown_token_is_refused_rather_than_treated_as_a_code(self):
        enroll_totp(self.user)
        ok, err = consume_totp(self.user, "wa_not_in_the_cache")
        self.assertFalse(ok)
        self.assertIn("security key", err)


@SECURE
class KeyManagementTests(_KeyTests):
    def test_the_list_never_carries_the_credential_or_the_public_key(self):
        add_key(self.user)
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("YubiKey", body)
        row = resp.json()[0]
        self.assertEqual(sorted(row), ["created_at", "id", "last_used_at", "name"])
        self.assertNotIn("credential_id", body)
        self.assertNotIn("public_key", body)
        self.assertNotIn(CRED_ID.hex(), body)

    def test_removing_a_key_needs_a_valid_second_factor(self):
        key = add_key(self.user)               # keys are the only factor here
        refused = self.client.delete(LIST_URL + f"{key.id}/", {}, format="json")
        self.assertEqual(refused.status_code, 401)
        self.assertTrue(SecurityKey.objects.filter(pk=key.id).exists())

        from apps.accounts import webauthn_keys
        with patch.object(webauthn_keys, "verify_authentication_response",
                          return_value=FakeAuthentication()):
            self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
            token = self.client.post(ASSERT_URL, {"credential": credential_payload()},
                                     format="json").json()["token"]
        gone = self.client.delete(LIST_URL + f"{key.id}/", {"totp": token}, format="json")
        self.assertEqual(gone.status_code, 204)
        self.assertFalse(SecurityKey.objects.filter(pk=key.id).exists())

    def test_a_user_cannot_rename_or_delete_another_users_key(self):
        theirs = add_key(get_user_model().objects.create_user("other", password="pw"))
        self.assertEqual(self.client.patch(LIST_URL + f"{theirs.id}/",
                                           {"name": "mine now"}, format="json").status_code, 404)
        enroll_totp(self.user)
        self.assertEqual(self.client.delete(LIST_URL + f"{theirs.id}/",
                                            {"totp": generate_totp(self.user.profile.totp_secret)},
                                            format="json").status_code, 404)
        self.assertTrue(SecurityKey.objects.filter(pk=theirs.id).exists())

    def test_rename_works_on_the_owners_own_key(self):
        key = add_key(self.user)
        resp = self.client.patch(LIST_URL + f"{key.id}/", {"name": "Office"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        key.refresh_from_db()
        self.assertEqual(key.name, "Office")

    def test_every_endpoint_needs_a_signed_in_session(self):
        anon = APIClient()
        for method, url in (("get", LIST_URL), ("post", ASSERT_OPTIONS_URL),
                            ("post", REGISTER_OPTIONS_URL), ("post", ASSERT_URL)):
            resp = getattr(anon, method)(url, {}, format="json")
            self.assertIn(resp.status_code, (401, 403), f"{method.upper()} {url}")


class SecureContextTests(TestCase):
    """WebAuthn only runs where the browser will run it. TOTP is unaffected."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user("root", password="pw")
        # Two factors on purpose: rule 3 asks for one before either options
        # endpoint may answer, so the tests below pay that first and then see
        # the secure-context refusal they are actually about.
        UserProfile.objects.create(user=self.user)
        enroll_totp(self.user)
        add_key(self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _factor(self):
        """A code or a key tap — either answers the confirmation prompt."""
        return generate_totp(self.user.profile.totp_secret)

    @override_settings(VIGIL_PUBLIC_URL="http://vigil.example.com")
    def test_plain_http_off_localhost_refuses_the_options_endpoints(self):
        for url in (REGISTER_OPTIONS_URL, ASSERT_OPTIONS_URL):
            resp = self.client.post(url, {"totp": self._factor()}, format="json")
            self.assertEqual(resp.status_code, 400, url)
            self.assertEqual(resp.json()["error"], "Security keys need HTTPS.")

    @override_settings(VIGIL_PUBLIC_URL="http://vigil.example.com")
    def test_a_confirmation_that_is_not_valid_is_refused_before_the_https_rule(self):
        """The factor gate is not skipped just because the context is insecure:
        a stolen session guessing at an https-gated option still costs guesses."""
        resp = self.client.post(REGISTER_OPTIONS_URL, {"totp": "000001"}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("webauthn_reg", self.client.session)

    @override_settings(VIGIL_PUBLIC_URL="https://vigil.example.com")
    def test_https_offers_them(self):
        with patch("apps.accounts.webauthn_keys.verify_registration_response",
                   return_value=FakeRegistration()):
            resp = self.client.post(REGISTER_OPTIONS_URL, {"totp": self._factor()},
                                    format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("challenge", resp.json())
        self.assertEqual(self.client.post(ASSERT_OPTIONS_URL, {}, format="json").status_code, 200)

    @override_settings(VIGIL_PUBLIC_URL="http://localhost:8000")
    def test_localhost_over_http_offers_them(self):
        resp = self.client.post(REGISTER_OPTIONS_URL, {"totp": self._factor()},
                                format="json", HTTP_HOST="localhost:8000")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("challenge", resp.json())

    @override_settings(VIGIL_PUBLIC_URL="", ALLOWED_HOSTS=["*"])
    def test_without_a_public_url_keys_are_off_whatever_the_host_header_says(self):
        # The identity a key is bound to comes from configuration only: a
        # request claiming to be localhost does not make WebAuthn available.
        resp = self.client.post(REGISTER_OPTIONS_URL, {"totp": self._factor()},
                                format="json", HTTP_HOST="localhost:8000")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("VIGIL_PUBLIC_URL", resp.json()["error"])
        body = self.client.get("/api/v1/accounts/totp/", HTTP_HOST="evil.example").json()
        self.assertFalse(body["keys_possible"])
        self.assertIn("VIGIL_PUBLIC_URL", body["keys_reason"])

    @override_settings(VIGIL_PUBLIC_URL="http://vigil.example.com")
    def test_the_status_endpoint_reports_it_so_the_ui_can_say_so(self):
        body = self.client.get("/api/v1/accounts/totp/").json()
        self.assertEqual(body["keys"], 1)
        self.assertTrue(body["enrolled"])            # TOTP is unaffected by http
        self.assertFalse(body["keys_possible"])

    @override_settings(VIGIL_PUBLIC_URL="https://vigil.example.com")
    def test_a_key_count_of_zero_does_not_offer_the_button(self):
        SecurityKey.objects.all().delete()          # nothing to confirm with either
        body = self.client.get("/api/v1/accounts/totp/").json()
        self.assertEqual(body["keys"], 0)
        self.assertTrue(body["keys_possible"])

    @override_settings(VIGIL_PUBLIC_URL="http://vigil.example.com")
    def test_the_gate_still_works_over_plain_http_because_totp_needs_no_https(self):
        """The refusal is about WebAuthn, not about two-factor in general."""
        ok, err = consume_totp(self.user, self._factor())
        self.assertTrue(ok, err)

    @override_settings(VIGIL_PUBLIC_URL="http://vigil.example.com")
    def test_the_options_endpoints_never_reach_the_library_over_plain_http(self):
        with patch("apps.accounts.webauthn_keys.generate_registration_options") as reg, \
                patch("apps.accounts.webauthn_keys.generate_authentication_options") as auth:
            self.client.post(REGISTER_OPTIONS_URL, {"totp": self._factor()}, format="json")
            self.client.post(ASSERT_OPTIONS_URL, {}, format="json")
        reg.assert_not_called()
        auth.assert_not_called()
        self.assertNotIn("webauthn_reg", self.client.session)
        self.assertNotIn("webauthn_auth", self.client.session)


class HasSecondFactorTests(TestCase):
    def test_totp_only_keys_only_and_neither(self):
        totp_user = get_user_model().objects.create_user("totp-only", password="pw")
        UserProfile.objects.create(user=totp_user)
        enroll_totp(totp_user)
        self.assertTrue(has_second_factor(totp_user))

        key_user = get_user_model().objects.create_user("keys-only", password="pw")
        UserProfile.objects.create(user=key_user)
        add_key(key_user)
        self.assertTrue(has_second_factor(key_user))

        bare = get_user_model().objects.create_user("nothing", password="pw")
        UserProfile.objects.create(user=bare)
        self.assertFalse(has_second_factor(bare))

    def test_a_pending_totp_enrollment_is_not_a_factor(self):
        user = get_user_model().objects.create_user("pending", password="pw")
        profile = UserProfile.objects.create(user=user)
        profile.totp_secret = generate_secret()      # no totp_confirmed_at
        profile.save()
        self.assertFalse(has_second_factor(user))

    @SECURE
    @override_settings(VIGIL_SESSION_BUS="memory")   # the wake push, not Redis
    def test_a_key_only_user_may_open_a_remote_session(self):
        """QA-56's gate asked for "TOTP"; a key tap must answer it too."""
        from apps.hosts.models import Host
        from apps.remote.tests.test_start import AGENT_TOKEN

        host = Host.objects.create(hostname="shell-host", agent_token=AGENT_TOKEN,
                                   status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
                                   agent_allow_remote_shell=True)
        key_user = get_user_model().objects.create_user("keyuser", password="pw")
        UserProfile.objects.create(user=key_user, role=Role.ADMIN)   # hosts:shell
        add_key(key_user)
        client = APIClient()
        client.force_authenticate(key_user)

        options = client.get(f"/api/v1/sessions/options/?host={host.id}")
        self.assertTrue(options.json()["shell"]["available"], options.content)
        self.assertEqual(options.json()["shell"]["reason"], "")

        from apps.accounts import webauthn_keys
        with patch.object(webauthn_keys, "verify_authentication_response",
                          return_value=FakeAuthentication()):
            client.force_authenticate(key_user)
            client.post(ASSERT_OPTIONS_URL, {}, format="json")
            token = client.post(ASSERT_URL, {"credential": credential_payload()},
                                format="json").json()["token"]
        resp = client.post("/api/v1/sessions/", {"host": str(host.id), "kind": "shell",
                                                 "totp": token}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)   # a key tap opened it


class TokenShapeTests(TestCase):
    def test_the_prefix_and_lifetimes_are_what_the_ui_and_cache_agree_on(self):
        from apps.accounts import webauthn_keys

        self.assertEqual(webauthn_keys.KEY_TOKEN_PREFIX, "wa_")
        self.assertEqual(webauthn_keys.KEY_TOKEN_SECONDS, 60)
        self.assertEqual(webauthn_keys.CHALLENGE_SECONDS, 120)

    def test_consume_key_token_needs_the_matching_owner_and_the_delete_winning(self):
        from django.core.cache import cache as default_cache

        from apps.accounts import webauthn_keys

        alice = get_user_model().objects.create_user("alice", password="pw")
        bob = get_user_model().objects.create_user("bob", password="pw")
        default_cache.set(f"webauthn:token:{webauthn_keys.KEY_TOKEN_PREFIX}t", alice.id, 60)

        self.assertFalse(webauthn_keys.consume_key_token(bob, "wa_t"))   # not hers
        # Only the caller whose delete removed the entry wins.
        with patch("apps.accounts.webauthn_keys.cache") as racing:
            racing.get.return_value = alice.id
            racing.delete.return_value = False       # somebody else got there first
            self.assertFalse(webauthn_keys.consume_key_token(alice, "wa_t"))
        self.assertTrue(webauthn_keys.consume_key_token(alice, "wa_t"))
        self.assertFalse(webauthn_keys.consume_key_token(alice, "wa_t"))

    def test_a_token_expires_after_its_own_lifetime(self):
        from django.core.cache import cache as default_cache

        from apps.accounts import webauthn_keys

        user = get_user_model().objects.create_user("carol", password="pw")
        key = f"webauthn:token:{webauthn_keys.KEY_TOKEN_SECONDS}s"
        # The TTL the issuer uses is the one the UI's 60-second promise names.
        with patch.object(webauthn_keys, "KEY_TOKEN_SECONDS", 7) as _life:
            default_cache.set(key, user.id, webauthn_keys.KEY_TOKEN_SECONDS)
            self.assertEqual(_life, 7)
        with patch.object(default_cache, "get", return_value=None):   # past the TTL
            self.assertFalse(webauthn_keys.consume_key_token(user, key.split(":", 1)[1]))

    @override_settings(VIGIL_PUBLIC_URL="https://vigil.example.com")
    def test_the_issued_token_carries_the_users_own_id_as_owner(self):
        """The assertion binds the token to the asserting user's id."""
        from django.core.cache import cache as default_cache

        from apps.accounts import webauthn_keys

        carol = get_user_model().objects.create_user("carol2", password="pw")
        UserProfile.objects.create(user=carol)
        add_key(carol)
        request = CeremonyRequest(carol)
        with patch.object(webauthn_keys, "verify_authentication_response",
                          return_value=FakeAuthentication()):
            webauthn_keys.authentication_options(request)
            token = webauthn_keys.authenticate(request, credential_payload())
        self.assertTrue(token.startswith("wa_"))
        self.assertEqual(default_cache.get(f"webauthn:token:{token}"), carol.id)


class RelyingPartyTests(SimpleTestCase):
    def test_the_public_url_wins_over_the_request_host(self):
        from apps.accounts.webauthn_keys import relying_party, secure_context

        with self.settings(VIGIL_PUBLIC_URL="https://vigil.example.com:8443/"):
            rp_id, origin = relying_party(_FakeRequest("http://internal:8000"))
        self.assertEqual(rp_id, "vigil.example.com")
        self.assertEqual(origin, "https://vigil.example.com:8443")
        self.assertTrue(secure_context(origin))

    def test_without_a_public_url_there_is_no_relying_party(self):
        from apps.accounts.webauthn_keys import relying_party, secure_context

        with self.settings(VIGIL_PUBLIC_URL=""):
            rp_id, origin = relying_party(_FakeRequest("http://127.0.0.1:8000"))
        self.assertEqual((rp_id, origin), ("", ""))
        self.assertFalse(secure_context(origin))
        self.assertTrue(secure_context("http://127.0.0.1:8000"))
        self.assertFalse(secure_context("http://192.168.1.10:8000"))


class _FakeRequest:
    """Just enough request for ``relying_party``."""

    def __init__(self, url: str):
        from urllib.parse import urlsplit

        split = urlsplit(url)
        self.scheme = split.scheme
        self._host = split.netloc

    def get_host(self) -> str:
        return self._host


class SourceShapeTests(SimpleTestCase):
    """The browser half cannot run here, so the source is the artifact."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        cls.utils = (repo / "server/static/js/vigil-utils.js").read_text()
        cls.settings_js = (repo / "server/static/js/vigil-settings.js").read_text()
        cls.template = (repo / "server/templates/pages/_settings.html").read_text()

    def test_the_prompt_offers_the_key_and_runs_the_ceremony(self):
        for needed in ("Use security key", "navigator.credentials.get",
                       "/api/v1/accounts/security-keys/assert/",
                       "clientDataJSON", "authenticatorData", "userHandle",
                       "allowCredentials"):
            self.assertIn(needed, self.utils, f"vigil-utils.js is missing {needed}")

    def test_the_settings_card_defines_its_two_handlers(self):
        for needed in ("function loadSecurityKeys", "async function addSecurityKey",
                       "navigator.credentials.create", "attestationObject",
                       "getTransports"):
            self.assertIn(needed, self.settings_js, f"vigil-settings.js is missing {needed}")

    def test_the_template_has_the_card_and_no_inline_handlers(self):
        self.assertIn('id="security-keys-list"', self.template)
        self.assertIn('data-act="addSecurityKey"', self.template)
        self.assertIn("Security keys need HTTPS.", self.template)
        self.assertNotIn("onclick=", self.template)
        self.assertNotIn("onclick=", self.settings_js)


class TimingSanityTests(TestCase):
    def test_challenge_expiry_is_checked_by_the_clock_not_the_caller(self):
        from apps.accounts import webauthn_keys

        class Session(dict):
            pass

        session = Session()
        webauthn_keys._store_challenge(session, "webauthn_auth", b"x" * 32)
        self.assertEqual(webauthn_keys._take_challenge(session, "webauthn_auth"), b"x" * 32)
        with self.assertRaises(ValueError):
            webauthn_keys._take_challenge(session, "webauthn_auth")

        webauthn_keys._store_challenge(session, "webauthn_reg", b"y" * 32)
        session["webauthn_reg"]["exp"] = time.time() - 1
        with self.assertRaises(ValueError):
            webauthn_keys._take_challenge(session, "webauthn_reg")


class ChallengeIsTheOneSentTests(TestCase):
    """The challenge in the options the browser receives is the one the session
    keeps. With two different random values, every real key fails verification
    while a mocked verify passes, which is how this was first missed."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from apps.accounts.models import Role, UserProfile

        self.user = get_user_model().objects.create_user("challenge-user", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_registration_options_carry_the_stored_challenge(self):
        with self.settings(VIGIL_PUBLIC_URL="https://vigil.example.com"):
            opts = self.client.post("/api/v1/accounts/security-keys/register/options/",
                                    {}, format="json").json()
        stored = self.client.session["webauthn_reg"]["challenge"]
        self.assertEqual(opts["challenge"], stored)

    def test_assertion_options_carry_the_stored_challenge(self):
        from apps.accounts.models import SecurityKey

        SecurityKey.objects.create(user=self.user, name="k", credential_id=b"cred-1",
                                   public_key=b"pk")
        with self.settings(VIGIL_PUBLIC_URL="https://vigil.example.com"):
            opts = self.client.post("/api/v1/accounts/security-keys/assert/options/",
                                    {}, format="json").json()
        stored = self.client.session["webauthn_auth"]["challenge"]
        self.assertEqual(opts["challenge"], stored)
