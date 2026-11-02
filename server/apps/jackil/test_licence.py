"""Handing Jackil the signed licence: the blob, not a verdict.

Jackil verifies the SQSY signature with its own copy of the public key, so
what this endpoint owes is the exact stored blob plus this instance's id —
and nothing else. A session gets 401 here like everywhere else in the
integration surface.
"""

import os
import time
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey
from rest_framework.test import APIClient

from apps.accounts.models import UserProfile
from apps.jackil.test_integration import _authed
from apps.licensing.models import StoredLicense
from apps.licensing.tests import PUB, SK, make_blob
from vigil import licensing

LICENCE = "/api/v1/jackil/integration/licence/"


def _claims(**over):
    """A payload with every field the wire format carries, so the round trip
    through this endpoint is checked against the blob Jackil will actually
    verify — ``lid`` included, since that is what traces a leaked key back."""
    claims = {
        "instance": licensing.instance_id(),
        "org": "sqsy",
        "seats": 4,
        "sites": None,
        "exp": int(time.time()) + 86400 * 90,
        "iat": int(time.time()),
        "lid": "L-2026-0417",
        "features": ["jackil", "sites"],
    }
    claims.update(over)
    return claims


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class LicenceHandoffTests(TestCase):
    def setUp(self):
        licensing.reload()

    def tearDown(self):
        os.environ.pop("VIGIL_LICENSE_KEY", None)
        StoredLicense.replace("")
        licensing.reload()

    def test_licence_hands_over_the_blob_and_instance(self):
        blob = make_blob(features=["jackil"])
        StoredLicense.replace(blob)
        licensing.reload()
        response = _authed().get(LICENCE)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["blob"], blob)
        self.assertEqual(body["instance"], licensing.instance_id())
        self.assertEqual(body["status"], "valid")

    def test_a_full_claims_blob_is_served_byte_for_byte_and_still_verifies(self):
        """Jackil verifies the signature over the exact payload bytes, so a
        blob that arrives with anything added, stripped or re-encoded is not a
        licence there even where it verifies here. ``lid`` round-trips too —
        it is what traces a leaked key back to a buyer."""
        import base64
        import json

        from apps.licensing.tests import _sign

        blob = _sign(_claims(), SK)
        StoredLicense.replace(blob)
        licensing.reload()
        body = _authed().get(LICENCE).json()

        self.assertEqual(body["blob"], blob, "not a byte may change on the way out")
        self.assertEqual(body["blob"].strip(), body["blob"],
                         "no padding whitespace — Jackil splits on '.'")
        self.assertEqual(body["instance"], licensing.instance_id())
        self.assertEqual(body["status"], "valid")

        parts = body["blob"].split(".")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], licensing.PREFIX)
        payload = json.loads(base64.urlsafe_b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4)).decode())
        self.assertEqual(payload["lid"], "L-2026-0417")
        self.assertEqual(sorted(payload["features"]), ["jackil", "sites"])

    def test_the_env_var_licence_is_the_one_handed_over(self):
        """§4 priority applies here too: a licence in the environment beats
        the stored paste. Jackil must not be handed the loser — and on this
        install that loser is a live licence with ``jackil`` in it, so
        forwarding it would unlock Business somewhere the operator never
        meant to enable it."""
        StoredLicense.replace(make_blob(features=["jackil"]))
        env_blob = make_blob(features=["sites"])
        os.environ["VIGIL_LICENSE_KEY"] = env_blob
        try:
            licensing.reload()
            body = _authed().get(LICENCE).json()
            self.assertEqual(body["blob"], env_blob)
            self.assertNotIn("jackil", body["blob"],
                             "the superseded paste must not reach Jackil")
        finally:
            del os.environ["VIGIL_LICENSE_KEY"]
            licensing.reload()

    def test_the_env_var_wins_whichever_way_the_licences_are_ordered(self):
        """The stored licence is the better-scoped one here, and the env var
        still wins. A handoff that picked by content instead of by source would
        pass the other ordering and fail this one."""
        StoredLicense.replace(make_blob(features=["sites"]))
        env_blob = make_blob(features=["jackil"])
        os.environ["VIGIL_LICENSE_KEY"] = env_blob
        try:
            licensing.reload()
            self.assertEqual(_authed().get(LICENCE).json()["blob"], env_blob)
        finally:
            del os.environ["VIGIL_LICENSE_KEY"]
            licensing.reload()

    def test_a_licence_for_another_instance_is_handed_over_as_mismatch(self):
        """Vigil's own verdict is "no licence" and its Business features stay
        off, but the bytes are still what Jackil gets — with ``mismatch``, not
        a silent empty blob. Jackil decides."""
        other = str(uuid.uuid4())
        blob = make_blob(instance=other)
        StoredLicense.replace(blob)
        licensing.reload()
        self.assertFalse(licensing.current_state().business_active)
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], blob)
        self.assertEqual(body["status"], "mismatch")
        self.assertNotEqual(body["instance"], other,
                            "`instance` is who we are, never who the licence was cut for")

    def test_grace_is_reported_as_grace(self):
        """13 days past expiry — Business stays on here (the grace window), and
        Jackil sees the same number rather than one flat verdict."""
        StoredLicense.replace(make_blob(exp_delta=-(86400 * 13)))
        licensing.reload()
        self.assertEqual(_authed().get(LICENCE).json()["status"], "grace")

    def test_lapsed_is_not_reported_as_grace(self):
        """Beyond the 14-day cliff — and not as `none` either: an expired
        licence is a different message from no licence, and Jackil has to be
        able to tell them apart."""
        StoredLicense.replace(make_blob(exp_delta=-(86400 * 30)))
        licensing.reload()
        self.assertEqual(_authed().get(LICENCE).json()["status"], "lapsed",
                         "an expired licence is not the same message as no licence")

    def test_a_licence_one_second_past_the_cliff_is_lapsed(self):
        """The far side of the 14-day boundary; `test_grace_is_reported_as_grace`
        holds the near side. A ladder that stopped telling grace from lapsed
        fails one of the two whichever way it lands."""
        StoredLicense.replace(make_blob(exp_delta=-(licensing.GRACE_DAYS * 86400) - 30))
        licensing.reload()
        self.assertEqual(_authed().get(LICENCE).json()["status"], "lapsed")

    def test_a_forged_licence_is_handed_over_as_invalid(self):
        """Signed by a key this build does not trust. The bytes still go —
        Jackil has its own public key and reaches its own conclusion."""
        blob = make_blob(sk=SigningKey.generate())
        StoredLicense.replace(blob)
        licensing.reload()
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], blob)
        self.assertEqual(body["status"], "invalid")

    def test_an_unparsable_blob_is_handed_over_as_invalid(self):
        """Not a blob at all — the right prefix and dot count, garbage in the
        payload. Still served verbatim: this Vigil's failure to read it says
        nothing about what Jackil will make of it."""
        junk = f"{licensing.PREFIX}.not-base64-at-all.still-not-base64"
        StoredLicense.replace(junk)
        licensing.reload()
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], junk)
        self.assertEqual(body["status"], "invalid")

    def test_a_two_part_blob_is_handed_over_as_invalid(self):
        junk = f"{licensing.PREFIX}.onlytwoparts"
        StoredLicense.replace(junk)
        licensing.reload()
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], junk)
        self.assertEqual(body["status"], "invalid")

    def test_a_narrowly_feature_scoped_licence_is_still_handed_over(self):
        """Sites licensed but not Jackil — the shape a customer upgrading one
        thing at a time produces. The bytes go verbatim, and Jackil's own
        check (M8) is what refuses them there."""
        blob = make_blob(features=["sites"])
        StoredLicense.replace(blob)
        licensing.reload()
        self.assertTrue(licensing.has_feature("sites"))
        self.assertFalse(licensing.has_feature("jackil"),
                         "the fixture itself must not be a licensed-for-Jackil blob")
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], blob, "the verdict is Jackil's to reach, so hand over the bytes")
        self.assertEqual(body["status"], "valid",
                         "a licence that is fine for Vigil is fine here too — this view grants nothing")

    def test_no_licence_is_an_empty_blob(self):
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], "")
        self.assertEqual(body["instance"], licensing.instance_id())
        self.assertEqual(body["status"], "none")

    def test_a_stored_row_of_whitespace_is_an_empty_blob(self):
        StoredLicense.replace("   ")
        licensing.reload()
        body = _authed().get(LICENCE).json()
        self.assertEqual(body["blob"], "")
        self.assertEqual(body["status"], "none")

    def test_licence_needs_an_integration_key(self):
        anonymous = APIClient().get(LICENCE)
        self.assertEqual(anonymous.status_code, 401)

        session = APIClient()
        session.force_login(get_user_model().objects.create_superuser(
            username="boss", email="boss@x", password="pw"))
        self.assertEqual(session.get(LICENCE).status_code, 401,
                         "a session must not borrow the integration door")

    def test_a_profileless_session_is_refused_too(self):
        """A user with no ``UserProfile`` row at all is still refused. The
        integration auth class returns ``None`` for a non-``vjk_`` bearer
        header, so this answer comes from DRF's *default* classes rather than
        from the view — the next endpoint to list only
        ``IntegrationKeyAuthentication`` would answer 200 with the blob here,
        and nothing else in the suite would notice."""
        bare = get_user_model().objects.create(username="bare")
        self.assertEqual(UserProfile.objects.filter(user=bare).count(), 0)
        client = APIClient()
        client.force_login(bare)
        self.assertEqual(client.get(LICENCE).status_code, 401,
                         "no profile is not a credential")

    def test_the_refusal_is_about_the_credential_not_the_licence(self):
        """Same request-time state — a valid licence on disk — and the key
        holder gets the bytes while the session gets nothing."""
        blob = make_blob(features=["jackil"])
        StoredLicense.replace(blob)
        licensing.reload()
        session = APIClient()
        session.force_login(get_user_model().objects.create_superuser(
            username="boss", email="boss@x", password="pw"))
        self.assertEqual(session.get(LICENCE).status_code, 401)
        self.assertEqual(_authed().get(LICENCE).json()["blob"], blob)

    def test_the_handoff_body_is_exactly_three_fields(self):
        """The shape Jackil (M8) parses, held fixed. Forwarding entitlements —
        seats, features, tier — would fail here rather than quietly become the
        grant Jackil is supposed to derive from the signature itself; and a
        renamed key would fail on the other side of the wire with no hint why."""
        self.assertEqual(sorted(_authed().get(LICENCE).json()),
                         ["blob", "instance", "status"])

    def test_only_get_reaches_it(self):
        client = _authed()
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(client, method)(LICENCE, {}, format="json")
                self.assertIn(response.status_code, (401, 403, 405),
                              f"{method.upper()} -> {response.status_code}")

    def test_licence_is_not_read_as_a_detail_route(self):
        response = _authed().get(LICENCE)
        self.assertNotEqual(response.json(), {"error": "Unknown type"})
        self.assertEqual(response.status_code, 200)
