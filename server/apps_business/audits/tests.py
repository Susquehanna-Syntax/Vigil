"""Audit log tests — recording is unconditional, viewing is Business."""

import base64
import json
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from vigil import hooks, licensing

from .models import AuditEvent, record

SK = SigningKey.generate()
PUB = base64.b64encode(SK.verify_key.encode()).decode()


def make_blob():
    claims = {
        "instance": licensing.instance_id(), "org": "t", "seats": 4,
        "sites": None, "exp": int(time.time()) + 86400, "iat": int(time.time()),
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(SK.sign(payload).signature)}"


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=PUB)
class AuditTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "boss", password="x", is_staff=True)
        self.client.force_login(self.admin)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def test_recording_needs_no_license(self):
        record("host.approved", user=self.admin, target="web-01")
        e = AuditEvent.objects.get(action="host.approved")
        self.assertEqual(e.username, "boss")
        self.assertEqual(e.target, "web-01")

    def test_hooks_feed_the_trail(self):
        from .apps import wire
        wire()  # other tests may have hooks.clear()ed; re-wiring is idempotent

        class FakeHost:
            hostname = "db-01"
        hooks.emit("host_approved", host=FakeHost(), approved_by=self.admin)
        self.assertTrue(AuditEvent.objects.filter(
            action="host.approved", target="db-01").exists())

    def test_login_and_failed_login_are_audited(self):
        c = self.client_class()
        c.post("/login/", {"username": "boss", "password": "WRONG"})
        self.assertTrue(AuditEvent.objects.filter(action="auth.login_failed",
                                                  target="boss").exists())

    def test_viewer_is_402_without_license(self):
        resp = self.client.get("/api/v1/audits/")
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(resp.json()["feature"], "audit_log")

    def test_viewer_and_filters_with_license(self):
        licensing.set_license(make_blob())
        record("host.approved", user=self.admin, target="a")
        record("host.rejected", user=self.admin, target="b")
        resp = self.client.get("/api/v1/audits/", {"action": "host.approved"})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        self.assertEqual([r["action"] for r in rows], ["host.approved"])

    def test_csv_export_with_license(self):
        licensing.set_license(make_blob())
        record("task.completed", target="reboot web-01")
        resp = self.client.get("/api/v1/audits/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])
        self.assertIn(b"task.completed", resp.content)

    def test_non_admin_cannot_view_even_licensed(self):
        licensing.set_license(make_blob())
        get_user_model().objects.create_user("viewer", password="x")
        c = self.client_class()
        c.login(username="viewer", password="x")
        self.assertEqual(c.get("/api/v1/audits/").status_code, 403)

    def test_record_never_raises(self):
        # detail with something JSON can't serialize must not blow up the caller
        record("weird", detail_obj=object())
        # nothing to assert beyond "we got here"; the row may or may not exist
