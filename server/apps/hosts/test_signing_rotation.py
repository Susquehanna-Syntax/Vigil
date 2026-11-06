"""Rotating the task-signing key without re-enrolling the fleet (sprint M3 phase 3).

The active key introduces the next one with a signed statement at check-in;
agents acknowledge it; `signing_key rotate` switches once they all have, a
minute ahead so every worker changes together; a key that was ever active is
never offered again.
"""
import base64
import io
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey
from rest_framework.test import APIClient

from apps.hosts.models import Host, SigningKeyEpoch
from vigil import signing

OLD = base64.b64encode(b"\x01" * 32).decode()
NEW = base64.b64encode(b"\x02" * 32).decode()


def pub(seed_b64):
    return base64.b64encode(bytes(SigningKey(base64.b64decode(seed_b64)).verify_key)).decode()


class _Rotation(TestCase):
    seeds = {"VIGIL_SIGNING_KEY_SEED": OLD, "VIGIL_SIGNING_KEY_NEXT_SEED": NEW}

    def setUp(self):
        patcher = override_settings(**self.seeds)
        patcher.enable()
        self.addCleanup(patcher.disable)
        signing.reset_cache()
        self.addCleanup(signing.reset_cache)
        self.client = APIClient()
        self.host = Host.objects.create(hostname="web-01", agent_token="tok-" + "r" * 32,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
                                        last_checkin=timezone.now())

    def checkin(self, host=None, **body):
        host = host or self.host
        resp = self.client.post("/api/v1/checkin", {"hostname": host.hostname, **body}, format="json",
                                HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def after(self, seconds):
        """Pretend `seconds` have passed, past the epoch cache too."""
        later = timezone.now() + timedelta(seconds=seconds)
        signing.reset_cache()
        return mock.patch("django.utils.timezone.now", return_value=later)


class StatementTests(_Rotation):
    def test_the_next_key_is_introduced_by_the_active_one(self):
        body = self.checkin()
        self.assertEqual(body["public_key"], pub(OLD))
        statement = body["key_rotation"]
        self.assertEqual(statement["next"], pub(NEW))
        sig = base64.b64decode(statement["sig"])
        VerifyKey(base64.b64decode(pub(OLD))).verify(signing.rotation_payload(pub(NEW)), sig)
        with self.assertRaises(BadSignatureError):   # not self-signed by the newcomer
            VerifyKey(base64.b64decode(pub(NEW))).verify(signing.rotation_payload(pub(NEW)), sig)

    def test_the_statement_cannot_be_taken_for_a_task_or_relay_list(self):
        payload = signing.rotation_payload(pub(NEW))
        self.assertIn(b'"type": "key-rotation"', payload)

    def test_an_ack_is_recorded_and_junk_is_not(self):
        self.checkin(key_ack=pub(NEW))
        self.host.refresh_from_db()
        self.assertEqual(self.host.signing_key_ack, pub(NEW))
        for junk in ("x" * 44, 12, pub(NEW)[:-1], ["a"]):
            self.checkin(key_ack=junk)
            self.host.refresh_from_db()
            self.assertEqual(self.host.signing_key_ack, pub(NEW), junk)


@override_settings(VIGIL_SIGNING_KEY_SEED=OLD, VIGIL_SIGNING_KEY_NEXT_SEED="")
class NoRotationTests(TestCase):
    def setUp(self):
        signing.reset_cache()
        self.addCleanup(signing.reset_cache)

    def test_without_a_next_seed_nothing_is_offered(self):
        self.assertIsNone(signing.rotation_statement())
        host = Host.objects.create(hostname="h", agent_token="tok-" + "n" * 32,
                                   status=Host.Status.ONLINE)
        body = APIClient().post("/api/v1/checkin", {"hostname": "h"}, format="json",
                                HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}").json()
        self.assertNotIn("key_rotation", body)
        self.assertEqual(body["public_key"], pub(OLD))

    def test_rotate_refuses_without_a_next_key(self):
        with self.assertRaises(CommandError):
            call_command("signing_key", "rotate", stdout=io.StringIO())


class RotateTests(_Rotation):
    def test_rotate_waits_for_every_recent_host(self):
        late = Host.objects.create(hostname="late-1", agent_token="tok-" + "l" * 32,
                                   status=Host.Status.ONLINE, last_checkin=timezone.now())
        Host.objects.create(hostname="gone-1", agent_token="tok-" + "g" * 32, status=Host.Status.OFFLINE,
                            last_checkin=timezone.now() - timedelta(days=30))
        Host.objects.create(hostname="pending-1", agent_token="tok-" + "p" * 32,
                            status=Host.Status.PENDING, last_checkin=timezone.now())
        self.checkin(key_ack=pub(NEW))
        with self.assertRaisesMessage(CommandError, "late-1"):
            call_command("signing_key", "rotate", stdout=io.StringIO())
        self.assertFalse(SigningKeyEpoch.objects.exists())
        self.checkin(late, key_ack=pub(NEW))
        out = io.StringIO()
        call_command("signing_key", "rotate", stdout=out)   # gone-1 and pending-1 are not waited for
        self.assertIn(pub(NEW), out.getvalue())

    def test_force_switches_and_names_the_stragglers(self):
        out = io.StringIO()
        call_command("signing_key", "rotate", "--force", stdout=out)
        self.assertIn("web-01", out.getvalue())

    def test_the_switch_lands_a_minute_later_for_everyone(self):
        self.checkin(key_ack=pub(NEW))
        call_command("signing_key", "rotate", stdout=io.StringIO())
        signing.reset_cache()
        self.assertEqual(signing.get_public_key_b64(), pub(OLD))          # not yet
        with self.after(signing.SWITCH_DELAY_SECONDS + 1):
            self.assertEqual(signing.get_public_key_b64(), pub(NEW))
            body = self.checkin()
            self.assertEqual(body["public_key"], pub(NEW))
            self.assertNotIn("key_rotation", body)                      # nothing more to offer

    def test_a_retired_key_is_never_offered_again(self):
        # After the switch the operator tidies .env: the new seed becomes the
        # current one and the old seed is left in NEXT by mistake.
        signing.switch_to_next(delay_seconds=0)
        with override_settings(VIGIL_SIGNING_KEY_SEED=NEW, VIGIL_SIGNING_KEY_NEXT_SEED=OLD), self.after(1):
            self.assertEqual(signing.get_public_key_b64(), pub(NEW))
            self.assertIsNone(signing.next_signing_key())
            self.assertNotIn("key_rotation", self.checkin())

    def test_losing_the_active_seed_is_refused_loudly(self):
        signing.switch_to_next(delay_seconds=0)
        with override_settings(VIGIL_SIGNING_KEY_SEED=OLD, VIGIL_SIGNING_KEY_NEXT_SEED=""), self.after(1):
            with self.assertRaisesMessage(ImproperlyConfigured, "neither"):
                signing.get_signing_key()

    def test_status_reports_acks_and_history(self):
        self.checkin(key_ack=pub(NEW))
        out = io.StringIO()
        call_command("signing_key", "status", stdout=out)
        self.assertIn("Acknowledged by 1 of 1", out.getvalue())
        signing.switch_to_next(delay_seconds=0)
        out = io.StringIO()
        with self.after(1):
            call_command("signing_key", "status", stdout=out)
        self.assertIn("History", out.getvalue())
        self.assertIn("the key in use before the first switch", out.getvalue())

    def test_tasks_are_signed_by_the_active_key(self):
        import json
        from types import SimpleNamespace
        task = SimpleNamespace(id="t", host_id="h", action="check_service", params={}, nonce="n",
                               ttl_seconds=60)
        payload = json.dumps({"id": "t", "host_id": "h", "action": "check_service", "params": {},
                              "nonce": "n", "ttl_seconds": 60, "dispatched_at": "2026-11-06T00:00:00+00:00",
                              "agent": "f" * 64, "v": 2}, sort_keys=True).encode()
        before = base64.b64decode(signing.sign_task_v2(task, "2026-11-06T00:00:00+00:00", "f" * 64))
        VerifyKey(base64.b64decode(pub(OLD))).verify(payload, before)
        signing.switch_to_next(delay_seconds=0)
        with self.after(1):
            after = base64.b64decode(signing.sign_task_v2(task, "2026-11-06T00:00:00+00:00", "f" * 64))
        VerifyKey(base64.b64decode(pub(NEW))).verify(payload, after)
        with self.assertRaises(BadSignatureError):
            VerifyKey(base64.b64decode(pub(OLD))).verify(payload, after)
