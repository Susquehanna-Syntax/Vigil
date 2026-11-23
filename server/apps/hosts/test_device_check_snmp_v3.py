"""SNMPv3 as a device check kind (S9, phase 05g): the v3 params the server accepts,
the two passwords kept encrypted on the check's secret column as one JSON blob, the
"password is stored" flags that answer without ever naming one, and the v3 entry the
poller is signed.

The v2c community — its column, its `has_community`, its place in the signed list —
is apps.hosts.test_device_check_snmp's subject; nothing here repeats it beyond the
two tests that prove a check can move between the two versions. The agent side of
`version: 3` is phase 05e's.
"""

import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from apps.hosts.device_checks import signed_check_list, validate_params
from apps.hosts.models import DeviceCheck, Host, UnmanagedDevice

AUTH_PASSWORD = "autheNtic08"
PRIV_PASSWORD = "privacyPassword9"


def make_host(hostname, token, poller=False):
    return Host.objects.create(hostname=hostname, agent_token=token, status=Host.Status.ONLINE,
                               mode=Host.Mode.MONITOR, agent_poller=poller)


def _v3_params(**extra):
    params = {"host": "10.0.0.5", "version": "3", "user": "monitorUser", "auth": "sha"}
    params.update(extra)
    return params


class SnmpV3ParamsTests(TestCase):
    def test_an_authPriv_check_keeps_its_non_secret_v3_keys(self):
        self.assertEqual(validate_params("snmp", _v3_params()),
                         {"host": "10.0.0.5", "version": "3", "user": "monitorUser",
                          "auth": "sha", "priv": True})

    def test_a_2c_check_is_stored_exactly_as_it_has_always_been(self):
        self.assertEqual(validate_params("snmp", _v3_params(version="2c")), {"host": "10.0.0.5"})
        self.assertEqual(validate_params("snmp", {"host": "10.0.0.5"}), {"host": "10.0.0.5"})

    def test_a_2c_check_drops_the_v3_keys_it_has_no_use_for(self):
        cleaned = validate_params("snmp", _v3_params(version="2c", priv=False, auth="sha256"))
        self.assertEqual(cleaned, {"host": "10.0.0.5"})

    def test_authNoPriv_is_a_check_that_stores_priv_false(self):
        cleaned = validate_params("snmp", _v3_params(priv=False, auth="sha256"))
        self.assertEqual(cleaned["priv"], False)
        self.assertEqual(cleaned["auth"], "sha256")

    def test_the_passwords_are_not_params_keys(self):
        cleaned = validate_params("snmp", _v3_params(auth_password=AUTH_PASSWORD,
                                                     priv_password=PRIV_PASSWORD))
        self.assertNotIn("auth_password", cleaned)
        self.assertNotIn("priv_password", cleaned)
        self.assertEqual(cleaned["user"], "monitorUser")

    def test_refusals_say_one_sentence(self):
        refused = [
            (_v3_params(version="1"), "version must be 2c or 3"),
            (_v3_params(version=3), "version must be 2c or 3"),
            (_v3_params(user=""), "user must be 1 to 32 characters"),
            (_v3_params(user="x" * 33), "user must be 1 to 32 characters"),
            (_v3_params(user=None), "user must be 1 to 32 characters"),
            (_v3_params(user="user\n"), "user must be 1 to 32 characters"),
            (_v3_params(auth="md5"), "auth must be sha or sha256"),
            (_v3_params(auth=None), "auth must be sha or sha256"),
            (_v3_params(priv="yes"), "priv must be true or false"),
            (_v3_params(priv=1), "priv must be true or false"),
        ]
        for params, message in refused:
            with self.subTest(params=params), self.assertRaises(ValueError) as caught:
                validate_params("snmp", params)
            self.assertEqual(str(caught.exception), message)

    def test_thirty_two_bytes_are_a_user_however_many_characters_that_is(self):
        self.assertEqual(validate_params("snmp", _v3_params(user="é" * 16))["user"], "é" * 16)
        with self.assertRaises(ValueError):
            validate_params("snmp", _v3_params(user="é" * 17))

    def test_the_v3_names_are_reserved_oid_labels(self):
        for label in ("version", "user", "auth", "priv", "community", "auth_password",
                      "priv_password"):
            with self.subTest(label=label), self.assertRaises(ValueError) as caught:
                validate_params("snmp", _v3_params(oids=[{"oid": "1.3.6.1.2.1.1.5.0",
                                                          "label": label}]))
            self.assertEqual(str(caught.exception), f"oids: {label} is a reserved label")


class SnmpV3ApiTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_user(username="admin-" + uuid4().hex[:8])
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(admin)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.url = f"/api/v1/hosts/devices/{self.device.id}/checks/"

    def _create(self, **body):
        params = body.pop("params", None) or _v3_params()
        payload = {"kind": "snmp", "params": params}
        payload.update(body)
        return self.client.post(self.url, payload, format="json")

    def _detail(self, check_id):
        return f"{self.url}{check_id}/"

    def _check(self, resp) -> DeviceCheck:
        return DeviceCheck.objects.get(pk=resp.data["id"])

    def _stored(self, check) -> dict:
        return json.loads(decrypt_secret(check.secret))

    def test_authPriv_is_created_with_both_passwords_stored_encrypted(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["params"], {"host": "10.0.0.5", "version": "3",
                                                 "user": "monitorUser", "auth": "sha",
                                                 "priv": True})
        self.assertTrue(created.data["has_auth_password"])
        self.assertTrue(created.data["has_priv_password"])
        self.assertFalse(created.data["has_community"])

        check = self._check(created)
        self.assertNotIn(AUTH_PASSWORD.encode(), check.secret)
        self.assertEqual(self._stored(check), {"auth": AUTH_PASSWORD, "priv": PRIV_PASSWORD})

    def test_authNoPriv_is_accepted_without_a_privacy_password(self):
        created = self._create(params=_v3_params(priv=False, auth="sha256"),
                               auth_password=AUTH_PASSWORD)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertIs(created.data["params"]["priv"], False)
        self.assertTrue(created.data["has_auth_password"])
        self.assertFalse(created.data["has_priv_password"])
        self.assertEqual(self._stored(self._check(created)),
                         {"auth": AUTH_PASSWORD, "priv": None})

    def test_the_passwords_are_nowhere_in_what_comes_back(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        for resp in (created, self.client.get(self.url),
                     self.client.patch(self._detail(created.data["id"]), {"enabled": False},
                                       format="json")):
            with self.subTest(status=resp.status_code):
                self.assertLess(resp.status_code, 300)
                for secret in (AUTH_PASSWORD.encode(), PRIV_PASSWORD.encode()):
                    self.assertNotIn(secret, resp.content)
                self.assertNotIn(b'"auth_password"', resp.content)
                self.assertNotIn(b'"priv_password"', resp.content)

    def _refused(self, body, message):
        """POSTed as a new check and 400 with that sentence; no password in the body."""
        payload = {"kind": "snmp", "params": body.pop("params", None) or _v3_params()}
        payload.update(body)
        resp = self.client.post(self.url, payload, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["error"], message)
        for secret in (AUTH_PASSWORD.encode(), PRIV_PASSWORD.encode()):
            self.assertNotIn(secret, resp.content)
        return resp

    def test_a_refused_body_never_says_the_password_it_was_sent(self):
        self._refused({"auth_password": AUTH_PASSWORD, "priv_password": "x"},
                      "priv_password must be 8 to 64 printable characters")

    def test_refusals_each_say_their_one_sentence(self):
        refused = [
            ({"params": _v3_params(version="1"), "auth_password": AUTH_PASSWORD,
              "priv_password": PRIV_PASSWORD}, "version must be 2c or 3"),
            ({"params": _v3_params(user=""), "auth_password": AUTH_PASSWORD,
              "priv_password": PRIV_PASSWORD}, "user must be 1 to 32 characters"),
            ({"params": _v3_params(user="u" * 33), "auth_password": AUTH_PASSWORD,
              "priv_password": PRIV_PASSWORD}, "user must be 1 to 32 characters"),
            ({"params": _v3_params(auth="md5"), "auth_password": AUTH_PASSWORD,
              "priv_password": PRIV_PASSWORD}, "auth must be sha or sha256"),
            ({"params": _v3_params(priv="yes"), "auth_password": AUTH_PASSWORD,
              "priv_password": PRIV_PASSWORD}, "priv must be true or false"),
            ({"auth_password": "a" * 7, "priv_password": PRIV_PASSWORD},
             "auth_password must be 8 to 64 printable characters"),
            ({"auth_password": AUTH_PASSWORD, "priv_password": "p" * 65},
             "priv_password must be 8 to 64 printable characters"),
            ({}, "an SNMPv3 check needs an auth password"),
            ({"auth_password": AUTH_PASSWORD},
             "an SNMPv3 check with privacy needs a privacy password"),
        ]
        for body, message in refused:
            with self.subTest(body=body):
                self._refused(body, message)
        self.assertEqual(DeviceCheck.objects.count(), 0)

    def test_a_v3_check_ignores_a_community(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD,
                               community="public")
        self.assertEqual(created.status_code, 201, created.data)
        check = self._check(created)
        self.assertNotIn(b"public", check.secret)
        self.assertEqual(self._stored(check), {"auth": AUTH_PASSWORD, "priv": PRIV_PASSWORD})
        self.assertFalse(created.data["has_community"])

    def test_a_patch_leaves_both_passwords_alone_when_the_body_omits_them(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        detail = self._detail(created.data["id"])

        patched = self.client.patch(detail, {"interval_seconds": 120}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertTrue(patched.data["has_auth_password"])
        self.assertTrue(patched.data["has_priv_password"])
        self.assertEqual(self._stored(self._check(patched)),
                         {"auth": AUTH_PASSWORD, "priv": PRIV_PASSWORD})

    def test_a_patch_that_changes_only_the_user_keeps_both_passwords(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        detail = self._detail(created.data["id"])

        patched = self.client.patch(detail, {"params": _v3_params(user="otherUser")},
                                    format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["params"]["user"], "otherUser")
        self.assertEqual(self._stored(self._check(patched)),
                         {"auth": AUTH_PASSWORD, "priv": PRIV_PASSWORD})

    def test_a_new_auth_password_replaces_only_that_one(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        detail = self._detail(created.data["id"])

        patched = self.client.patch(detail, {"auth_password": "rotatedAuth1"}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(self._stored(self._check(patched)),
                         {"auth": "rotatedAuth1", "priv": PRIV_PASSWORD})

    def test_priv_false_drops_the_stored_privacy_password(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        detail = self._detail(created.data["id"])

        patched = self.client.patch(detail, {"params": _v3_params(priv=False)}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertFalse(patched.data["has_priv_password"])
        self.assertTrue(patched.data["has_auth_password"])
        self.assertEqual(self._stored(self._check(patched)),
                         {"auth": AUTH_PASSWORD, "priv": None})

    def test_priv_true_needs_a_privacy_password_and_keeps_the_stored_one(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        detail = self._detail(created.data["id"])

        dropped = self.client.patch(detail, {"params": _v3_params(priv=False)}, format="json")
        self.assertEqual(dropped.status_code, 200, dropped.data)

        refused = self.client.patch(detail, {"params": _v3_params(priv=True)}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(refused.data["error"],
                         "an SNMPv3 check with privacy needs a privacy password")

        moved = self.client.patch(detail, {"params": _v3_params(priv=True),
                                           "priv_password": PRIV_PASSWORD}, format="json")
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertTrue(moved.data["has_priv_password"])

    def test_a_v3_check_turned_into_v2c_needs_a_community(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        detail = self._detail(created.data["id"])

        refused = self.client.patch(detail, {"params": {"host": "10.0.0.5"}}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(refused.data["error"], "an SNMP check needs a community")

        moved = self.client.patch(detail, {"params": {"host": "10.0.0.5", "version": "2c"},
                                           "community": "public"}, format="json")
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertTrue(moved.data["has_community"])
        self.assertFalse(moved.data["has_auth_password"])
        self.assertFalse(moved.data["has_priv_password"])
        check = self._check(moved)
        self.assertEqual(decrypt_secret(check.secret), "public")

    def test_a_v2c_check_turned_into_v3_needs_the_passwords(self):
        created = self.client.post(self.url, {"kind": "snmp", "params": {"host": "10.0.0.5"},
                                              "community": "public"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        detail = self._detail(created.data["id"])

        refused = self.client.patch(detail, {"params": _v3_params()}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(refused.data["error"], "an SNMPv3 check needs an auth password")

        moved = self.client.patch(detail, {"params": _v3_params(),
                                           "auth_password": AUTH_PASSWORD,
                                           "priv_password": PRIV_PASSWORD}, format="json")
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertTrue(moved.data["has_auth_password"])
        self.assertFalse(moved.data["has_community"])

    def test_a_check_whose_secret_is_garbage_reports_no_passwords(self):
        created = self._create(auth_password=AUTH_PASSWORD, priv_password=PRIV_PASSWORD)
        DeviceCheck.objects.filter(pk=created.data["id"]).update(secret=b"junk")

        listed = self.client.get(self.url)
        self.assertEqual(listed.status_code, 200)
        self.assertFalse(listed.data[0]["has_auth_password"])
        self.assertFalse(listed.data[0]["has_priv_password"])
        self.assertFalse(listed.data[0]["has_community"])

    def test_a_label_called_user_is_refused_as_a_reserved_name(self):
        refused = self.client.post(self.url, {"kind": "snmp", "params": _v3_params(oids=[
                                       {"oid": "1.3.6.1.2.1.1.5.0", "label": "user"}]),
                                   "auth_password": AUTH_PASSWORD,
                                   "priv_password": PRIV_PASSWORD}, format="json")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(refused.data["error"], "oids: user is a reserved label")


class SnmpV3SignedListTests(TestCase):
    def setUp(self):
        self.poller = make_host("p1", "tok-p1-" + "c" * 32, poller=True)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")

    def _check(self, **body):
        params = body.pop("params", _v3_params())
        secret = json.dumps({"auth": body.pop("auth", AUTH_PASSWORD),
                             "priv": body.pop("priv", PRIV_PASSWORD)})
        return DeviceCheck.objects.create(device=self.device, kind=DeviceCheck.Kind.SNMP,
                                          params=dict(params),
                                          secret=encrypt_secret(secret))

    def _params_of(self, check) -> dict:
        listed = signed_check_list(self.poller)
        by_id = {entry["id"]: entry["params"] for entry in listed["checks"]}
        return by_id[str(check.id)]

    def test_a_v3_entry_carries_the_secrets_and_no_priv_flag(self):
        check = self._check(params=_v3_params(port=1161))
        params = self._params_of(check)
        self.assertEqual(params, {"host": "10.0.0.5", "version": 3, "user": "monitorUser",
                                  "auth": "sha", "port": 1161,
                                  "auth_password": AUTH_PASSWORD,
                                  "priv_password": PRIV_PASSWORD})
        self.assertIsInstance(params["version"], int)
        self.assertNotIn("priv", params)

    def test_an_authNoPriv_entry_has_no_privacy_password(self):
        check = self._check(params=_v3_params(priv=False), priv=None)
        params = self._params_of(check)
        self.assertEqual(params["version"], 3)
        self.assertNotIn("priv_password", params)
        self.assertNotIn("priv", params)
        self.assertEqual(params["auth_password"], AUTH_PASSWORD)

    def test_a_check_whose_secret_is_garbage_is_left_out(self):
        self._check()
        broken = self._check()
        DeviceCheck.objects.filter(pk=broken.id).update(secret=b"junk")

        listed = signed_check_list(self.poller)
        ids = [entry["id"] for entry in listed["checks"]]
        self.assertNotIn(str(broken.id), ids)
        self.assertEqual(len(ids), 1)

    def test_a_v3_secret_that_holds_nothing_useful_is_left_out(self):
        for secret in (encrypt_secret(json.dumps({"priv": PRIV_PASSWORD})),
                       encrypt_secret("public"),
                       encrypt_secret("{}")):
            with self.subTest(secret=secret):
                check = self._check()
                DeviceCheck.objects.filter(pk=check.id).update(secret=secret)
                listed = signed_check_list(self.poller)
                self.assertEqual(listed["checks"], [])

    def test_the_params_of_a_handed_out_check_are_never_written_back(self):
        check = self._check()
        params = self._params_of(check)
        self.assertIn("auth_password", params)
        check.refresh_from_db()
        self.assertEqual(check.params, _v3_params())
