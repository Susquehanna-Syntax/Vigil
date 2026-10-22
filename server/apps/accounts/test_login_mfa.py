"""Signing in takes a second factor for every account (login_mfa)."""
import time
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils.timezone import now

from apps.accounts.login_mfa import ENROL_KEY, PENDING_KEY
from apps.accounts.models import SecurityKey, UserProfile
from apps.accounts.totp import MAX_TOTP_FAILURES, generate_totp
from apps.accounts.webauthn_keys import KEY_TOKEN_PREFIX

SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "correct-horse1"


def _with_totp(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.totp_secret = SECRET
    profile.totp_confirmed_at = now()
    profile.save()
    return user


def _with_key(user):
    return SecurityKey.objects.create(user=user, name="Key", credential_id=b"cred-1",
                                      public_key=b"pk", sign_count=0)


class PasswordAloneTests(TestCase):
    def setUp(self):
        self.user = _with_totp(get_user_model().objects.create_user("alice", password=PASSWORD))

    def _password(self, next_url=""):
        url = "/login/" + (f"?next={next_url}" if next_url else "")
        return self.client.post(url, {"username": "alice", "password": PASSWORD})

    def test_a_correct_password_does_not_sign_in(self):
        resp = self._password()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/login/verify/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_pending_session_is_anonymous_everywhere_else(self):
        self._password()
        self.assertIn(self.client.get("/api/v1/accounts/totp/").status_code, (401, 403))
        self.assertTrue(self.client.get("/").url.startswith("/login/"))

    def test_a_wrong_password_never_reaches_the_second_step(self):
        self.client.post("/login/", {"username": "alice", "password": "nope"})
        self.assertNotIn(PENDING_KEY, self.client.session)
        self.assertEqual(self.client.get("/login/verify/")["Location"], "/login/")


class CodeTests(TestCase):
    def setUp(self):
        self.user = _with_totp(get_user_model().objects.create_user("alice", password=PASSWORD))

    def _password(self, next_url="/hosts/"):
        self.client.post(f"/login/?next={next_url}", {"username": "alice", "password": PASSWORD})

    def test_the_right_code_signs_in_and_goes_on(self):
        self._password()
        resp = self.client.post("/login/verify/", {"code": generate_totp(SECRET)})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/hosts/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertNotIn(PENDING_KEY, self.client.session)

    def test_a_wrong_code_does_not(self):
        self._password()
        resp = self.client.post("/login/verify/", {"code": "000000"})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_offsite_next_is_dropped(self):
        self._password("https://evil.example.com/")
        resp = self.client.post("/login/verify/", {"code": generate_totp(SECRET)})
        self.assertEqual(resp["Location"], "/")

    def test_a_code_cannot_sign_in_twice(self):
        code = generate_totp(SECRET)
        self._password()
        self.client.post("/login/verify/", {"code": code})
        self.client.logout()
        self._password()
        resp = self.client.post("/login/verify/", {"code": code})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_guesses_run_out(self):
        self._password()
        for _ in range(MAX_TOTP_FAILURES):
            self.client.post("/login/verify/", {"code": "000000"})
        resp = self.client.post("/login/verify/", {"code": generate_totp(SECRET)})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_typed_key_token_is_refused(self):
        token = KEY_TOKEN_PREFIX + "x" * 40
        cache.set(f"webauthn:token:{token}", self.user.id, 60)
        self._password()
        resp = self.client.post("/login/verify/", {"code": token})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_second_step_expires(self):
        self._password()
        session = self.client.session
        session[PENDING_KEY]["exp"] = time.time() - 1
        session.save()
        resp = self.client.post("/login/verify/", {"code": generate_totp(SECRET)})
        self.assertEqual(resp["Location"], "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_user_switched_off_meanwhile_is_not_signed_in(self):
        self._password()
        self.user.is_active = False
        self.user.save()
        resp = self.client.post("/login/verify/", {"code": generate_totp(SECRET)})
        self.assertEqual(resp["Location"], "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)


class EnrolTests(TestCase):
    """An account with no factor sets one up before the dashboard opens."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", password=PASSWORD)
        self.client.post("/login/", {"username": "bob", "password": PASSWORD})

    def test_the_page_offers_enrolment(self):
        resp = self.client.get("/login/verify/")
        self.assertContains(resp, "Set up two-factor sign-in")
        self.assertTrue(self.client.session[ENROL_KEY])

    def test_confirming_enrols_and_signs_in(self):
        self.client.get("/login/verify/")
        secret = self.client.session[ENROL_KEY]
        resp = self.client.post("/login/verify/", {"code": generate_totp(secret)})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        profile = UserProfile.objects.get(user=self.user)
        self.assertIsNotNone(profile.totp_confirmed_at)
        self.assertEqual(profile.totp_secret, secret)

    def test_a_wrong_code_enrols_nothing(self):
        self.client.get("/login/verify/")
        resp = self.client.post("/login/verify/", {"code": "000000"})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)
        profile = UserProfile.objects.filter(user=self.user).first()
        self.assertTrue(profile is None or profile.totp_confirmed_at is None)

    def test_enrolment_guesses_run_out(self):
        self.client.get("/login/verify/")
        secret = self.client.session[ENROL_KEY]
        for _ in range(MAX_TOTP_FAILURES):
            self.client.post("/login/verify/", {"code": "000000"})
        resp = self.client.post("/login/verify/", {"code": generate_totp(secret)})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_secret_no_sign_in(self):
        # Posting before the page minted a secret must not enrol anything.
        resp = self.client.post("/login/verify/", {"code": "123456"})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(VIGIL_PUBLIC_URL="https://vigil.example.com")
class KeyTests(TestCase):
    def setUp(self):
        self.user = _with_totp(get_user_model().objects.create_user("alice", password=PASSWORD))
        _with_key(self.user)

    def _password(self):
        self.client.post("/login/?next=/reports/", {"username": "alice", "password": PASSWORD})

    def _verified_as(self, owner):
        """Stand in for the WebAuthn check: it passes, and the token it hands
        back belongs to ``owner``."""
        def fake(request, credential, user=None):
            token = KEY_TOKEN_PREFIX + "t" * 40
            cache.set(f"webauthn:token:{token}", owner.id, 60)
            return token
        return patch("apps.accounts.login_mfa.webauthn_authenticate", side_effect=fake)

    def test_the_page_offers_the_key(self):
        self._password()
        self.assertContains(self.client.get("/login/verify/"), "Use a passkey or security key")

    def test_options_need_a_pending_sign_in(self):
        resp = self.client.post("/login/verify/key/options/")
        self.assertEqual(resp.status_code, 401)

    def test_options_list_the_pending_users_keys(self):
        self._password()
        resp = self.client.post("/login/verify/key/options/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["rpId"], "vigil.example.com")
        self.assertEqual(len(resp.json()["allowCredentials"]), 1)

    def test_a_verified_tap_signs_in(self):
        self._password()
        with self._verified_as(self.user):
            resp = self.client.post("/login/verify/key/", {"credential": {}},
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["next"], "/reports/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_the_key_is_checked_against_the_pending_user(self):
        self._password()
        with self._verified_as(self.user) as fake:
            self.client.post("/login/verify/key/", {"credential": {}},
                             content_type="application/json")
        self.assertEqual(fake.call_args.kwargs["user"], self.user)

    def test_a_token_for_someone_else_does_not_sign_in(self):
        other = get_user_model().objects.create_user("mallory", password=PASSWORD)
        self._password()
        with self._verified_as(other):
            resp = self.client.post("/login/verify/key/", {"credential": {}},
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_failed_tap_does_not_sign_in(self):
        self._password()
        with patch("apps.accounts.login_mfa.webauthn_authenticate",
                   side_effect=ValueError("That security key is not enrolled for this account.")):
            resp = self.client.post("/login/verify/key/", {"credential": {}},
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_tap_without_a_pending_sign_in_is_refused(self):
        with self._verified_as(self.user):
            resp = self.client.post("/login/verify/key/", {"credential": {}},
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_taps_share_the_guess_limit(self):
        self._password()
        with patch("apps.accounts.login_mfa.webauthn_authenticate", side_effect=ValueError("no")):
            for _ in range(MAX_TOTP_FAILURES):
                self.client.post("/login/verify/key/", {"credential": {}},
                                 content_type="application/json")
        with self._verified_as(self.user):
            resp = self.client.post("/login/verify/key/", {"credential": {}},
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)


class ResetCommandTests(TestCase):
    def test_reset_clears_both_factors(self):
        user = _with_totp(get_user_model().objects.create_user("alice", password=PASSWORD))
        _with_key(user)
        call_command("reset_2fa", "alice", stdout=StringIO())
        profile = UserProfile.objects.get(user=user)
        self.assertIsNone(profile.totp_confirmed_at)
        self.assertEqual(profile.totp_secret, "")
        self.assertFalse(SecurityKey.objects.filter(user=user).exists())

    def test_after_a_reset_the_next_sign_in_enrols(self):
        _with_totp(get_user_model().objects.create_user("alice", password=PASSWORD))
        call_command("reset_2fa", "alice", stdout=StringIO())
        self.client.post("/login/", {"username": "alice", "password": PASSWORD})
        self.assertContains(self.client.get("/login/verify/"), "Set up two-factor sign-in")


class AdminLoginTests(TestCase):
    """The Django admin's own login form must not be a way round the second step."""

    def setUp(self):
        _with_totp(get_user_model().objects.create_superuser("root", password=PASSWORD))

    def test_the_admin_login_page_sends_you_to_vigils(self):
        resp = self.client.get("/admin/login/?next=/admin/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/login/?next=%2Fadmin%2F")

    def test_posting_a_password_to_it_signs_nobody_in(self):
        self.client.post("/admin/login/", {"username": "root", "password": PASSWORD})
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)

    def test_after_the_second_step_the_admin_opens(self):
        self.client.post("/login/?next=/admin/", {"username": "root", "password": PASSWORD})
        resp = self.client.post("/login/verify/", {"code": generate_totp(SECRET)})
        self.assertEqual(resp["Location"], "/admin/")
        self.assertEqual(self.client.get("/admin/").status_code, 200)
