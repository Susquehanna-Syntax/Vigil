"""Licensing core tests — the §0 invariant under test: nothing ever blocks.

Test licenses are signed with a throwaway keypair generated per-run; the
matching public key is injected with override_settings. This mirrors the
dev/prod key-custody model: whichever single key a build trusts is the only
key licenses verify against.
"""

import base64
import json
import time
import uuid

from django.contrib.auth import get_user_model
from django.core import management
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from apps.licensing.models import InstanceIdentity, StoredLicense
from vigil import licensing


def _sign(claims: dict, sk: SigningKey) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    sig = sk.sign(payload).signature

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(sig)}"


SK = SigningKey.generate()
PUB = base64.b64encode(SK.verify_key.encode()).decode()
OTHER_SK = SigningKey.generate()


def make_blob(*, instance=None, exp_delta=86400 * 90, seats=4, features=None, sk=SK):
    claims = {
        "instance": instance or licensing.instance_id(),
        "org": "test-org",
        "seats": seats,
        "sites": None,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if features is not None:
        claims["features"] = features
    return _sign(claims, sk)


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class LicensingTests(TestCase):
    def setUp(self):
        licensing.reload()

    def tearDown(self):
        StoredLicense.replace("")
        licensing.reload()

    # -- instance identity --------------------------------------------------

    def test_instance_id_is_stable_and_uuid(self):
        first = licensing.instance_id()
        uuid.UUID(first)  # raises if not a UUID
        self.assertEqual(first, licensing.instance_id())
        self.assertEqual(InstanceIdentity.objects.count(), 1)

    # -- the free default ----------------------------------------------------

    def test_no_license_is_free_tier_with_free_features(self):
        state = licensing.current_state()
        self.assertIs(state.status, licensing.Status.NONE)
        self.assertEqual(state.tier, "free")
        self.assertTrue(licensing.has_feature("baselines"))
        self.assertTrue(licensing.has_feature("ai_suggestions"))
        self.assertFalse(licensing.has_feature("sites"))
        self.assertFalse(licensing.has_feature("audit_log"))
        self.assertEqual(licensing.seats_allowed(), licensing.FREE_SEATS)

    # -- happy path ----------------------------------------------------------

    def test_valid_license_lights_business_features_without_restart(self):
        licensing.set_license(make_blob())
        state = licensing.current_state()
        self.assertIs(state.status, licensing.Status.VALID)
        self.assertEqual(state.tier, "business")
        self.assertTrue(licensing.has_feature("sites"))
        self.assertTrue(licensing.has_feature("rbac_advanced"))
        self.assertEqual(licensing.seats_allowed(), 4)

    def test_features_subset_license(self):
        licensing.set_license(make_blob(features=["sites"]))
        self.assertTrue(licensing.has_feature("sites"))
        self.assertFalse(licensing.has_feature("audit_log"))

    # -- instance binding: one license, one deployment (§4) ------------------

    def test_license_for_another_instance_is_no_license(self):
        licensing.set_license(make_blob(instance=str(uuid.uuid4())))
        state = licensing.current_state()
        self.assertIs(state.status, licensing.Status.MISMATCH)
        self.assertEqual(state.tier, "free")
        self.assertFalse(licensing.has_feature("sites"))
        # The banner names both IDs so support can fix it.
        msgs = " ".join(b["message"] for b in licensing.banners())
        self.assertIn(licensing.instance_id(), msgs)
        self.assertIn("Re-bind", msgs)

    # -- degradation ladder (§6) ----------------------------------------------

    def test_grace_period_keeps_business_on(self):
        licensing.set_license(make_blob(exp_delta=-3 * 86400))  # expired 3d ago
        state = licensing.current_state()
        self.assertIs(state.status, licensing.Status.GRACE)
        self.assertTrue(state.business_active)
        self.assertTrue(licensing.has_feature("sites"))
        self.assertEqual(licensing.banners()[0]["severity"], "critical")

    def test_lapsed_turns_business_off_never_monitoring(self):
        licensing.set_license(make_blob(exp_delta=-30 * 86400))
        state = licensing.current_state()
        self.assertIs(state.status, licensing.Status.LAPSED)
        self.assertFalse(licensing.has_feature("sites"))
        self.assertTrue(licensing.has_feature("baselines"))  # free never blinks

    def test_expiry_warning_ladder(self):
        for days, severity in ((25, "info"), (10, "warning"), (3, "critical")):
            licensing.set_license(make_blob(exp_delta=days * 86400))
            sev = [b["severity"] for b in licensing.banners()]
            self.assertIn(severity, sev, f"T-{days}d should carry {severity}")

    # -- garbage in, free tier out (§0: cannot raise) --------------------------

    def test_wrong_key_garbage_and_tampering_degrade_to_free(self):
        for blob in (
            make_blob(sk=OTHER_SK),                    # signed by a stranger
            "SQSY-LICENSE-V1.not.real",                # junk
            "hello world",                             # not even close
            make_blob().replace("SQSY-LICENSE-V1", "SQSY-LICENSE-V9"),
        ):
            StoredLicense.replace(blob)
            state = licensing.reload()
            self.assertIn(state.status,
                          (licensing.Status.INVALID,), msg=blob[:40])
            self.assertEqual(state.tier, "free")
            self.assertTrue(licensing.has_feature("baselines"))

    # -- seat overage banners, never blocks (§6) -------------------------------

    def test_seat_overage_banners_and_user_creation_never_blocked(self):
        licensing.set_license(make_blob(seats=1))
        User = get_user_model()
        for i in range(3):  # 3 users on a 1-seat license — all must succeed
            User.objects.create_user(f"u{i}", password="x")
        self.assertEqual(licensing.seats_used(), 3)
        over = [b for b in licensing.banners() if "seats in use" in b["message"]]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["severity"], "info")  # a nudge, not an alarm

    # -- env var beats DB (§4 priority) ----------------------------------------

    def test_env_var_wins_over_db_paste(self):
        import os
        licensing.set_license(make_blob(seats=2))
        env_blob = make_blob(seats=7)
        os.environ["VIGIL_LICENSE_KEY"] = env_blob
        try:
            state = licensing.reload()
            self.assertEqual(state.source, "env")
            self.assertEqual(state.claims.seats, 7)
        finally:
            del os.environ["VIGIL_LICENSE_KEY"]
            licensing.reload()

    # -- management command -----------------------------------------------------

    def test_manage_license_set_and_show(self):
        from io import StringIO
        out = StringIO()
        management.call_command("license", "set", make_blob(), stdout=out)
        self.assertIn("license valid", out.getvalue())
        self.assertIn(licensing.instance_id(), out.getvalue())

    # -- DRF gate -----------------------------------------------------------------

    def test_require_feature_402_body_when_unlicensed(self):
        from rest_framework.exceptions import APIException

        perm = licensing.require_feature("audit_log")()
        with self.assertRaises(APIException) as ctx:
            perm.has_permission(None, None)
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(perm.message["feature"], "audit_log")
        licensing.set_license(make_blob())
        self.assertTrue(perm.has_permission(None, None))
