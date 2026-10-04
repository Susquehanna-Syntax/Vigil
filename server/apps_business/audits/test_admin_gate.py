"""Admin gate tests — the audit trail is a Business feature, gated per request."""

import base64
import json
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from nacl.signing import SigningKey

from vigil import licensing

from .models import AuditEvent

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
class AuditAdminGateTests(TestCase):
    CHANGE_LIST = "/admin/business_audits/auditevent/"
    CHANGE = "/admin/business_audits/auditevent/%s/change/"

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "boss", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        licensing.reload()

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    def test_a_free_superuser_cannot_open_the_audit_changelist(self):
        AuditEvent.objects.create(username="auditee", action="host.approved",
                                  target="web-01")
        resp = self.client.get(self.CHANGE_LIST, follow=True)
        self.assertNotEqual(resp.status_code, 200, "changelist opened while unlicensed")
        body = resp.content.decode()
        self.assertFalse("auditee" in body,
                         "changelist body leaks the seeded event while unlicensed")

    def test_a_free_superuser_does_not_see_audits_on_the_admin_index(self):
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertFalse("business_audits/auditevent" in body,
                         "admin index advertises the audit trail while unlicensed")

    def test_a_licensed_superuser_can_open_the_audit_changelist(self):
        licensing.set_license(make_blob())
        AuditEvent.objects.create(username="auditee", action="host.approved",
                                  target="web-01")
        resp = self.client.get(self.CHANGE_LIST)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue("auditee" in resp.content.decode(),
                        "licensed changelist does not show the seeded event")

    def test_the_trail_stays_append_only_when_licensed(self):
        licensing.set_license(make_blob())
        event = AuditEvent.objects.create(username="auditee", action="host.approved",
                                          target="web-01")
        add_resp = self.client.get(self.CHANGE_LIST + "add/")
        self.assertNotEqual(add_resp.status_code, 200,
                            "add view opened despite the append-only guard")
        change_resp = self.client.get(self.CHANGE % event.pk)
        # Django's change view renders read-only when only view permission holds,
        # so a superuser still gets a 200 page; the guard is the absence of any
        # save controls, which is what append-only means to a viewer.
        self.assertFalse('name="_save"' in change_resp.content.decode(),
                         "change view offers a save button on an append-only trail")
        post_resp = self.client.post(self.CHANGE % event.pk, {
            "username": "auditee", "action": "host.approved", "target": "web-01",
            "user": self.admin.pk, "id": event.pk,
            "_continue": "Continue",
        })
        self.assertNotEqual(post_resp.status_code, 302,
                            "a save was accepted on an append-only trail")

    def test_a_lapsed_licence_closes_the_changelist_without_a_restart(self):
        licensing.set_license(make_blob())
        AuditEvent.objects.create(username="auditee", action="host.approved",
                                  target="web-01")
        open_resp = self.client.get(self.CHANGE_LIST)
        self.assertEqual(open_resp.status_code, 200)
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()
        lapsed_resp = self.client.get(self.CHANGE_LIST, follow=True)
        self.assertNotEqual(lapsed_resp.status_code, 200,
                            "changelist still open after the licence lapsed in-process")
