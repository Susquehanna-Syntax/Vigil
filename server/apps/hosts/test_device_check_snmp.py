"""SNMP v2c as a device check kind (S9, phase 05b): params validation, the community kept
encrypted on its own column, and the community inside the signed hand-off to a poller."""

import base64
import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from apps.hosts.device_checks import signed_check_list, validate_params
from apps.hosts.models import DeviceCheck, Host, UnmanagedDevice
from vigil.signing import get_signing_key

COMMUNITY = "s3cret-community"


def make_host(hostname, token, poller=False):
    return Host.objects.create(hostname=hostname, agent_token=token, status=Host.Status.ONLINE,
                               mode=Host.Mode.MONITOR, agent_poller=poller)


def _snmp_params(**extra):
    params = {"host": "10.0.0.5"}
    params.update(extra)
    return params


class SnmpParamsTests(TestCase):
    def test_a_minimal_check_needs_only_a_host(self):
        self.assertEqual(validate_params("snmp", _snmp_params()), {"host": "10.0.0.5"})

    def test_the_defaults_are_not_stored(self):
        self.assertEqual(
            validate_params("snmp", _snmp_params(port=161, version="2c")),
            {"host": "10.0.0.5"})
        self.assertEqual(
            validate_params("snmp", _snmp_params(port=1161, version="2c", oids=[])),
            {"host": "10.0.0.5", "port": 1161})

    def test_labelled_oids_are_cleaned(self):
        cleaned = validate_params("snmp", _snmp_params(oids=[
            {"oid": "1.3.6.1.2.1.2.2.1.10.1", "label": "in_octets", "junk": 1},
            {"oid": "1.3.6.1.2.1.4.20.1.1.10.0.0.1", "label": "addr"},
        ]))
        self.assertEqual(cleaned, {"host": "10.0.0.5", "oids": [
            {"oid": "1.3.6.1.2.1.2.2.1.10.1", "label": "in_octets"},
            {"oid": "1.3.6.1.2.1.4.20.1.1.10.0.0.1", "label": "addr"}]})

    def test_refusals_say_one_sentence(self):
        refused = [
            (_snmp_params(), None),  # placeholder replaced below, host alone is valid
            ({"host": "-rf"}, "host must be a host name or address"),
            (_snmp_params(port=0), "port must be a whole number from 1 to 65535"),
            (_snmp_params(port="161"), "port must be a whole number from 1 to 65535"),
            (_snmp_params(version="3"), "version must be 2c"),
            (_snmp_params(oids="1.3.6.1"), "oids must be a list of {oid, label}"),
            (_snmp_params(oids=[{"oid": 5, "label": "x"}]), "oids must be a list of {oid, label}"),
            (_snmp_params(oids=[{"oid": "1.3.6.9999999999999.1", "label": "x"}]),
             "oids: 1.3.6.9999999999999.1 is not a numeric OID"),
            (_snmp_params(oids=[{"oid": "sysName", "label": "x"}]),
             "oids: sysName is not a numeric OID"),
            (_snmp_params(oids=[{"oid": "1.3.6.1", "label": "In"}]),
             "oids: labels are lower-case letters, digits and _"),
            (_snmp_params(oids=[{"oid": "1.3.6.1", "label": "uptime_s"}]),
             "oids: uptime_s is a reserved label"),
            (_snmp_params(oids=[{"oid": "1.3.6.1", "label": "a"},
                                {"oid": "1.3.6.2", "label": "a"}]),
             "oids: a is a reserved label"),
        ]
        for params, message in refused[1:]:
            with self.subTest(params=params), self.assertRaises(ValueError) as caught:
                validate_params("snmp", params)
            self.assertEqual(str(caught.exception), message)

    def test_more_than_eight_oids_is_refused(self):
        oids = [{"oid": f"1.3.6.1.2.1.1.{n}.0", "label": f"o{n}"} for n in range(9)]
        with self.assertRaises(ValueError) as caught:
            validate_params("snmp", _snmp_params(oids=oids))
        self.assertEqual(str(caught.exception), "oids: at most 8")

    def test_a_community_in_params_does_not_survive(self):
        cleaned = validate_params("snmp", _snmp_params(community=COMMUNITY))
        self.assertNotIn("community", cleaned)
        self.assertEqual(cleaned, {"host": "10.0.0.5"})

    def test_an_unknown_kind_names_snmp(self):
        with self.assertRaises(ValueError) as caught:
            validate_params("snmpv3", _snmp_params())
        self.assertEqual(str(caught.exception), "kind must be icmp, tcp, http, dns or snmp")


class SnmpCommunityApiTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_user(username="admin-" + uuid4().hex[:8])
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(admin)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.url = f"/api/v1/hosts/devices/{self.device.id}/checks/"

    def _create(self, **body):
        payload = {"kind": "snmp", "params": _snmp_params()}
        payload.update(body)
        return self.client.post(self.url, payload, format="json")

    def test_the_community_is_stored_encrypted_and_never_shown(self):
        created = self._create(community=COMMUNITY)
        self.assertEqual(created.status_code, 201, created.data)
        check = DeviceCheck.objects.get(pk=created.data["id"])
        self.assertNotEqual(check.secret, COMMUNITY.encode())
        self.assertEqual(decrypt_secret(check.secret), COMMUNITY)
        self.assertTrue(created.data["has_community"])
        self.assertNotIn("community", created.json())

        listed = self.client.get(self.url)
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(listed.data[0]["has_community"])
        # device_check_detail is PATCH/DELETE only; a check comes back from the list.
        for resp in (created, listed):
            self.assertNotIn(COMMUNITY.encode(), resp.content)
            self.assertNotIn(b'"community"', resp.content)

    def test_create_needs_a_community(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "an SNMP check needs a community")

    def test_the_community_must_be_1_to_64_printable_characters(self):
        for bad in ("", "x" * 65, "ok\n", "café"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                self._check_fields(bad)
            self.assertEqual(str(caught.exception), "community must be 1 to 64 printable characters")

    def _check_fields(self, community):
        from apps.hosts.views.hosts import _check_fields

        return _check_fields(self.device, {"kind": "snmp", "params": _snmp_params(),
                                           "community": community})

    def test_a_patch_without_a_community_keeps_the_stored_one(self):
        created = self._create(community=COMMUNITY)
        detail = f"{self.url}{created.data['id']}/"
        patched = self.client.patch(detail, {"interval_seconds": 120}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        check = DeviceCheck.objects.get(pk=created.data["id"])
        self.assertEqual(decrypt_secret(check.secret), COMMUNITY)
        self.assertTrue(patched.data["has_community"])

    def test_a_patch_with_a_new_one_replaces_it(self):
        created = self._create(community=COMMUNITY)
        detail = f"{self.url}{created.data['id']}/"
        patched = self.client.patch(detail, {"community": "another-one"}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        check = DeviceCheck.objects.get(pk=created.data["id"])
        self.assertEqual(decrypt_secret(check.secret), "another-one")

    def test_a_patch_to_icmp_forgets_the_community(self):
        created = self._create(community=COMMUNITY)
        detail = f"{self.url}{created.data['id']}/"
        patched = self.client.patch(detail, {"kind": "icmp", "params": {"host": "10.0.0.5"}},
                                    format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        check = DeviceCheck.objects.get(pk=created.data["id"])
        self.assertEqual(check.secret, b"")
        self.assertFalse(patched.data["has_community"])

    def test_a_non_snmp_check_ignores_a_community(self):
        created = self.client.post(self.url, {"kind": "icmp", "params": {"host": "10.0.0.5"},
                                              "community": COMMUNITY}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        check = DeviceCheck.objects.get(pk=created.data["id"])
        self.assertEqual(check.secret, b"")
        self.assertFalse(created.data["has_community"])
        self.assertNotIn(COMMUNITY.encode(), created.content)


class SnmpSignedListTests(TestCase):
    def setUp(self):
        self.poller = make_host("p1", "tok-p1-" + "b" * 32, poller=True)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.params = _snmp_params(oids=[{"oid": "1.3.6.1.2.1.2.2.1.10.1", "label": "in_octets"}])
        self.check = DeviceCheck.objects.create(device=self.device, kind=DeviceCheck.Kind.SNMP,
                                                params=dict(self.params), secret=encrypt_secret(COMMUNITY))

    def test_the_community_travels_in_the_signed_entry_and_not_in_the_model(self):
        listed = signed_check_list(self.poller)
        entry = listed["checks"][0]
        self.assertEqual(entry["params"], {**self.params, "community": COMMUNITY})
        self.check.refresh_from_db()
        self.assertEqual(self.check.params, self.params)

    def test_the_signature_still_verifies_over_the_payload(self):
        listed = signed_check_list(self.poller)
        payload = {"v": 1, "type": "check-list", "agent": self.poller.token_fingerprint,
                   "issued": listed["checks_issued"], "checks": listed["checks"]}
        get_signing_key().verify_key.verify(
            json.dumps(payload, sort_keys=True).encode(),
            base64.b64decode(listed["checks_sig"]))

    def test_a_check_whose_community_no_longer_decrypts_is_left_out(self):
        self.check.secret = b"not a fernet token"
        self.check.save()
        listed = signed_check_list(self.poller)
        self.assertEqual(listed["checks"], [])
