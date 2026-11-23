"""SSH config backup as a device check kind (S9 08b), server side.

The server makes the key pair and keeps the private half encrypted as the
check's secret; only a poller gets it, inside the signed check list, and only
while the license grants config backup. A result's output never goes into
CheckResult: its digest does, and the text goes out on the
``device_config_fetched`` hook for Business to keep. The first host key a
poller reports is pinned.
"""

import hashlib
from unittest import mock
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from apps.hosts.device_checks import (signed_check_list, ssh_private_key, store_check_results,
                                      validate_params)
from apps.hosts.models import CheckResult, DeviceCheck, Host, UnmanagedDevice
from vigil import hooks

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTestsOnly0123456789abcdefgh"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAnotherHostKeyForTestsOnly0123456789abcd"
CONFIG = "hostname sw-01\ninterface Gi0/1\n description uplink\n"
LICENSED = mock.patch("vigil.licensing.has_feature", side_effect=lambda name: name == "config_backup")
UNLICENSED = mock.patch("vigil.licensing.has_feature", return_value=False)


def _params(**extra):
    params = {"host": "10.0.0.1", "username": "backup", "command": "show running-config"}
    params.update(extra)
    return params


class SshParamsTests(TestCase):
    def test_a_minimal_check_is_cleaned_and_the_default_port_not_stored(self):
        self.assertEqual(validate_params("ssh", _params(port=22, junk=1)), _params())
        self.assertEqual(validate_params("ssh", _params(port=2222))["port"], 2222)

    def test_a_host_key_is_kept_normalised_and_an_empty_one_dropped(self):
        spaced = HOST_KEY.replace(" ", "   ")
        self.assertEqual(validate_params("ssh", _params(host_key=spaced))["host_key"], HOST_KEY)
        self.assertNotIn("host_key", validate_params("ssh", _params(host_key="")))

    def test_a_private_or_public_key_in_params_never_survives(self):
        cleaned = validate_params("ssh", _params(private_key="-----BEGIN OPENSSH PRIVATE KEY-----",
                                                 public_key="ssh-ed25519 AAAA"))
        self.assertEqual(cleaned, _params())

    def test_refusals_say_one_sentence(self):
        cases = [
            ({"host": "-oProxyCommand=x", "username": "a", "command": "x"},
             "host must be a host name or address"),
            (_params(username="root;id"), "username must be 1 to 64 letters, digits, '.', '_' or '-'"),
            (_params(username=""), "username must be 1 to 64 letters, digits, '.', '_' or '-'"),
            (_params(command=""), "command must be one line of 1 to 500 characters"),
            (_params(command="show run\nreload"), "command must be one line of 1 to 500 characters"),
            (_params(command="x" * 501), "command must be one line of 1 to 500 characters"),
            (_params(port=70000), "port must be a whole number from 1 to 65535"),
            (_params(host_key="ssh-dss AAAA"), "host_key must be '<type> <base64>', as in a known_hosts line"),
            (_params(host_key="ssh-ed25519 not*base64"),
             "host_key must be '<type> <base64>', as in a known_hosts line"),
        ]
        for params, message in cases:
            with self.subTest(params=params), self.assertRaises(ValueError) as caught:
                validate_params("ssh", params)
            self.assertEqual(str(caught.exception), message)


class SshCheckApiTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_user(username="admin-" + uuid4().hex[:8])
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(admin)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.url = f"/api/v1/hosts/devices/{self.device.id}/checks/"

    def _create(self, **body):
        payload = {"kind": "ssh", "params": _params()}
        payload.update(body)
        with LICENSED:
            return self.client.post(self.url, payload, format="json")

    def _patch(self, check_id, body, licensed=True):
        with (LICENSED if licensed else UNLICENSED):
            return self.client.patch(f"{self.url}{check_id}/", body, format="json")

    def test_create_makes_a_key_pair_and_answers_only_the_public_half(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.data)
        check = DeviceCheck.objects.get(pk=resp.data["id"])
        private = decrypt_secret(check.secret)
        self.assertTrue(private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"))
        public = resp.data["params"]["public_key"]
        self.assertTrue(public.startswith("ssh-ed25519 "))
        self.assertTrue(resp.data["has_key"])
        self.assertNotIn(private.encode(), resp.content)
        self.assertNotIn(b"PRIVATE KEY", self.client.get(self.url).content)

    def test_the_public_key_matches_the_private_one(self):
        from cryptography.hazmat.primitives import serialization

        resp = self._create()
        check = DeviceCheck.objects.get(pk=resp.data["id"])
        key = serialization.load_ssh_private_key(decrypt_secret(check.secret).encode(), None)
        public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH).decode()
        self.assertEqual(resp.data["params"]["public_key"].rsplit(" ", 1)[0], public)

    def test_without_the_license_an_ssh_check_cannot_be_made(self):
        with UNLICENSED:
            resp = self.client.post(self.url, {"kind": "ssh", "params": _params()}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "SSH config backup needs a Business license")
        self.assertFalse(DeviceCheck.objects.exists())

    def test_nor_can_another_check_be_turned_into_one(self):
        check = DeviceCheck.objects.create(device=self.device, kind="icmp", params={"host": "10.0.0.1"})
        resp = self._patch(check.id, {"kind": "ssh", "params": _params(),
                                      "interval_seconds": 3600}, licensed=False)
        self.assertEqual(resp.status_code, 400)
        check.refresh_from_db()
        self.assertEqual(check.kind, "icmp")

    def test_an_existing_one_can_still_be_disabled_after_the_license_lapses(self):
        created = self._create()
        resp = self._patch(created.data["id"], {"enabled": False}, licensed=False)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["enabled"])

    def test_it_runs_daily_by_default_and_never_more_than_every_15_minutes(self):
        self.assertEqual(self._create().data["interval_seconds"], 86400)
        resp = self._create(interval_seconds=600)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"],
                         "an SSH check runs at most every 15 minutes (interval_seconds 900 or more)")
        self.assertEqual(self._create(interval_seconds=900).status_code, 201)

    def test_a_patch_keeps_the_key_pair_unless_asked_to_rotate(self):
        created = self._create()
        check = DeviceCheck.objects.get(pk=created.data["id"])
        secret, public = check.secret, created.data["params"]["public_key"]
        kept = self._patch(check.id, {"params": _params(command="cat /conf/config.xml")})
        self.assertEqual(kept.status_code, 200, kept.data)
        self.assertEqual(kept.data["params"]["public_key"], public)
        self.assertEqual(kept.data["params"]["command"], "cat /conf/config.xml")
        check.refresh_from_db()
        self.assertEqual(check.secret, secret)

        rotated = self._patch(check.id, {"rotate_key": True})
        self.assertEqual(rotated.status_code, 200, rotated.data)
        self.assertNotEqual(rotated.data["params"]["public_key"], public)
        check.refresh_from_db()
        self.assertNotEqual(decrypt_secret(check.secret), decrypt_secret(secret))

    def test_rotate_key_must_be_a_boolean(self):
        created = self._create()
        resp = self._patch(created.data["id"], {"rotate_key": "yes"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "rotate_key must be true or false")

    def test_a_pinned_host_key_survives_an_edit_that_does_not_mention_it(self):
        created = self._create(params=_params(host_key=HOST_KEY))
        check_id = created.data["id"]
        resp = self._patch(check_id, {"params": _params(command="show startup-config")})
        self.assertEqual(resp.data["params"]["host_key"], HOST_KEY)
        # An empty one unpins, so the next run learns the device's key again.
        resp = self._patch(check_id, {"params": _params(host_key="")})
        self.assertNotIn("host_key", resp.data["params"])

    def test_moving_the_check_to_another_host_drops_the_pin(self):
        created = self._create(params=_params(host_key=HOST_KEY))
        resp = self._patch(created.data["id"], {"params": _params(host="10.0.0.2")})
        self.assertNotIn("host_key", resp.data["params"])

    def test_a_check_changed_away_from_ssh_forgets_the_key(self):
        created = self._create()
        resp = self._patch(created.data["id"], {"kind": "icmp", "params": {"host": "10.0.0.1"}})
        self.assertEqual(resp.status_code, 200, resp.data)
        check = DeviceCheck.objects.get(pk=created.data["id"])
        self.assertEqual(check.secret, b"")
        self.assertFalse(resp.data["has_key"])


class SshHandOffTests(TestCase):
    def setUp(self):
        self.poller = Host.objects.create(hostname="p1", agent_token="tok-p1-" + "b" * 32,
                                          status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
                                          agent_poller=True)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
        self.check = DeviceCheck.objects.create(
            device=self.device, kind=DeviceCheck.Kind.SSH, interval_seconds=86400,
            params=_params(public_key="ssh-ed25519 AAAA vigil-config-backup"),
            secret=encrypt_secret(self.key))

    def test_the_private_key_travels_in_the_signed_entry_without_the_public_one(self):
        with LICENSED:
            entry = signed_check_list(self.poller)["checks"][0]
        self.assertEqual(entry["params"], {**_params(), "private_key": self.key})
        self.check.refresh_from_db()
        self.assertNotIn("private_key", self.check.params)

    def test_a_lapsed_license_stops_the_hand_off(self):
        with UNLICENSED:
            self.assertEqual(signed_check_list(self.poller)["checks"], [])

    def test_a_key_that_no_longer_decrypts_is_left_out(self):
        self.check.secret = b"not a fernet token"
        self.check.save()
        self.assertIsNone(ssh_private_key(self.check))
        with LICENSED:
            self.assertEqual(signed_check_list(self.poller)["checks"], [])


class SshResultTests(TestCase):
    def setUp(self):
        self.poller = Host.objects.create(hostname="p1", agent_token="tok-p1-" + "c" * 32,
                                          status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
                                          agent_poller=True)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.check = DeviceCheck.objects.create(
            device=self.device, kind=DeviceCheck.Kind.SSH, interval_seconds=86400,
            params=_params(), secret=encrypt_secret("-----BEGIN OPENSSH PRIVATE KEY-----\n"))
        self.fetched = []
        self.handler = lambda **payload: self.fetched.append(payload)
        hooks.subscribe("device_config_fetched", self.handler)
        self.addCleanup(lambda: hooks._subscribers["device_config_fetched"].remove(self.handler))

    def _row(self, **extra):
        row = {"check": str(self.check.id), "at": timezone.now().isoformat(), "ok": True,
               "latency_ms": 812.0, "detail": "3 lines", "output": CONFIG,
               "sha256": hashlib.sha256(CONFIG.encode()).hexdigest(),
               "size": len(CONFIG.encode()), "host_key": HOST_KEY}
        row.update(extra)
        return row

    def test_the_output_goes_to_the_hook_and_only_its_digest_is_stored(self):
        self.assertEqual(store_check_results(self.poller, [self._row()]), 1)
        result = CheckResult.objects.get()
        self.assertTrue(result.ok)
        self.assertNotIn("output", result.extra)
        self.assertNotIn("host_key", result.extra)
        self.assertEqual(result.extra["sha256"], hashlib.sha256(CONFIG.encode()).hexdigest())
        self.assertEqual(result.extra["size"], len(CONFIG.encode()))
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual(self.fetched[0]["output"], CONFIG)
        self.assertEqual(self.fetched[0]["check"].id, self.check.id)
        self.assertEqual(self.fetched[0]["time"], result.time)

    def test_the_first_reported_host_key_is_pinned_and_a_later_one_ignored(self):
        store_check_results(self.poller, [self._row()])
        self.check.refresh_from_db()
        self.assertEqual(self.check.params["host_key"], HOST_KEY)
        store_check_results(self.poller, [self._row(host_key=OTHER_KEY)])
        self.check.refresh_from_db()
        self.assertEqual(self.check.params["host_key"], HOST_KEY)

    def test_a_refused_login_still_pins_the_key_but_fetches_nothing(self):
        store_check_results(self.poller, [self._row(ok=False, detail="Permission denied (publickey).",
                                                    output=None, sha256=None, size=None)])
        self.check.refresh_from_db()
        self.assertEqual(self.check.params["host_key"], HOST_KEY)
        self.assertEqual(self.fetched, [])
        self.assertFalse(CheckResult.objects.get().ok)

    def test_output_that_does_not_match_its_digest_is_not_kept(self):
        store_check_results(self.poller, [self._row(sha256="0" * 64)])
        result = CheckResult.objects.get()
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "the output did not match its digest")
        self.assertEqual(self.fetched, [])

    def test_a_garbled_host_key_is_not_pinned(self):
        store_check_results(self.poller, [self._row(host_key="ssh-ed25519 ***")])
        self.check.refresh_from_db()
        self.assertNotIn("host_key", self.check.params)

    def test_output_over_the_cap_is_not_kept(self):
        big = "x" * 1_000_001
        store_check_results(self.poller, [self._row(output=big,
                                                    sha256=hashlib.sha256(big.encode()).hexdigest())])
        self.assertFalse(CheckResult.objects.get().ok)
        self.assertEqual(self.fetched, [])

    def test_a_non_ssh_result_with_an_output_key_is_untouched(self):
        icmp = DeviceCheck.objects.create(device=self.device, kind="icmp", params={"host": "10.0.0.1"})
        store_check_results(self.poller, [{"check": str(icmp.id), "ok": True, "detail": "up",
                                           "output": "x"}])
        self.assertEqual(self.fetched, [])
