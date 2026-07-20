"""Civil SSO client tests — hermetic: the "Civil" here is a locally minted
keypair and hand-forged tokens; no network anywhere."""

import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.civilsso.models import CachedCivilKey, CivilIdentity

KEY = Ed25519PrivateKey.generate()
PRIVATE_PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
PUBLIC_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def forge(sub=None, aud="vigil", iss="civil", exp_delta=60, username="alice",
          key=PRIVATE_PEM, **extra):
    now = int(time.time())
    claims = {
        "iss": iss, "aud": aud, "sub": sub or str(uuid.uuid4()),
        "preferred_username": username, "email": "a@example.com",
        "name": "Alice Q Example", "orgs": [],
        "iat": now, "exp": now + exp_delta, **extra,
    }
    return jwt.encode(claims, key, algorithm="EdDSA")


@override_settings(CIVIL_URL="http://civil.test", CIVIL_APP_SLUG="vigil")
class CallbackTests(TestCase):
    def setUp(self):
        CachedCivilKey.store(PUBLIC_PEM, "http://civil.test/api/v1/pubkey/")
        # Vigil's SetupRedirectMiddleware sends everything to /setup/ until an
        # admin exists; SSO tests exercise post-setup behavior.
        get_user_model().objects.create_superuser("setup-admin", password="x")

    def _start_state(self):
        # login_start would set this; seed the session directly.
        session = self.client.session
        session["civilsso_state"] = "st4te"
        session.save()

    def test_valid_token_provisions_and_logs_in(self):
        self._start_state()
        sub = str(uuid.uuid4())
        resp = self.client.get("/accounts/civil/callback",
                               {"token": forge(sub=sub), "state": "st4te"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")
        identity = CivilIdentity.objects.get(civil_id=sub)
        self.assertEqual(identity.user.username, "alice")
        self.assertFalse(identity.user.has_usable_password())
        self.assertEqual(int(self.client.session["_auth_user_id"]),
                         identity.user.pk)

    def test_second_login_reuses_mapping(self):
        sub = str(uuid.uuid4())
        for _ in range(2):
            self._start_state()
            self.client.get("/accounts/civil/callback",
                            {"token": forge(sub=sub), "state": "st4te"})
        self.assertEqual(CivilIdentity.objects.count(), 1)
        self.assertEqual(get_user_model().objects.exclude(username="setup-admin").count(), 1)

    def test_username_collision_gets_suffix_not_takeover(self):
        # A pre-existing local "alice" must NOT be claimable via Civil.
        local = get_user_model().objects.create_user("alice", password="x")
        self._start_state()
        self.client.get("/accounts/civil/callback",
                        {"token": forge(), "state": "st4te"})
        identity = CivilIdentity.objects.get()
        self.assertEqual(identity.user.username, "alice-2")
        self.assertNotEqual(identity.user.pk, local.pk)

    def test_state_mismatch_fails_to_login_page(self):
        self._start_state()
        resp = self.client.get("/accounts/civil/callback",
                               {"token": forge(), "state": "WRONG"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])
        self.assertEqual(CivilIdentity.objects.count(), 0)

    def test_bad_tokens_fail_closed(self):
        stranger = Ed25519PrivateKey.generate()
        stranger_pem = stranger.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        for label, token in [
            ("wrong audience", forge(aud="sigil")),
            ("wrong issuer", forge(iss="evil")),
            ("expired", forge(exp_delta=-120)),
            ("stranger key", forge(key=stranger_pem)),
            ("garbage", "not.a.jwt"),
        ]:
            self._start_state()
            resp = self.client.get("/accounts/civil/callback",
                                   {"token": token, "state": "st4te"})
            self.assertIn("/login/", resp["Location"], label)
            self.assertEqual(CivilIdentity.objects.count(), 0, label)

    def test_inactive_local_user_cannot_enter(self):
        sub = str(uuid.uuid4())
        user = get_user_model().objects.create_user("bob", password="x", is_active=False)
        CivilIdentity.objects.create(user=user, civil_id=sub)
        self._start_state()
        resp = self.client.get("/accounts/civil/callback",
                               {"token": forge(sub=sub), "state": "st4te"})
        self.assertIn("/login/", resp["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)


class DisabledTests(TestCase):
    def test_unconfigured_means_404(self):
        # No CIVIL_URL: the routes simply do not exist for this install.
        self.assertEqual(
            self.client.get("/accounts/civil/login/").status_code, 404)
        self.assertEqual(
            self.client.get("/accounts/civil/callback").status_code, 404)


@override_settings(CIVIL_URL="http://civil.test")
class LoginStartTests(TestCase):
    def test_redirects_to_civil_with_state_and_callback(self):
        resp = self.client.get("/accounts/civil/login/", {"next": "/"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(
            "http://civil.test/sso/authorize?"))
        self.assertIn("app=vigil", resp["Location"])
        self.assertIn("state=", resp["Location"])
        self.assertEqual(self.client.session["civilsso_next"], "/")

    def test_offsite_next_is_dropped(self):
        self.client.get("/accounts/civil/login/",
                        {"next": "https://evil.example.com/"})
        self.assertNotIn("civilsso_next", self.client.session)


class CivilConfigTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("cfgadmin", password="x")
        self.client.force_login(self.admin)

    def test_db_config_enables_feature_without_env(self):
        from . import client
        from .models import CivilConfig
        self.assertFalse(client.enabled())
        resp = self.client.post("/api/v1/civil/settings/",
                                '{"enabled": true, "url": "http://civil.lan:8100/", "app_slug": "vigil"}',
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["active"])
        self.assertEqual(client.civil_url(), "http://civil.lan:8100")
        cfg = CivilConfig.current()
        self.assertTrue(cfg.enabled)

    @override_settings(CIVIL_URL="http://env-civil.lan")
    def test_env_var_still_wins(self):
        from . import client
        self.client.post("/api/v1/civil/settings/",
                         '{"enabled": true, "url": "http://db-civil.lan"}',
                         content_type="application/json")
        self.assertEqual(client.civil_url(), "http://env-civil.lan")
        resp = self.client.get("/api/v1/civil/settings/")
        self.assertTrue(resp.json()["env_override"])

    def test_settings_require_admin(self):
        get_user_model().objects.create_user("pleb2", password="x")
        c = self.client_class()
        c.login(username="pleb2", password="x")
        self.assertEqual(c.get("/api/v1/civil/settings/").status_code, 403)
