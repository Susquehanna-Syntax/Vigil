"""A key that may write: the flag, the two locks, and what a write returns.

The positive tests are the ones Jackil depends on; the negative ones are the
reason the flag exists — a helpdesk holding a key must not be able to empty
the inventory, and the two admins who decide that are whoever set the flag and
whoever created the key.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import UnmanagedDevice

from .models import IntegrationKey
from .test_integration import PING, UNKNOWN_HOST, _admin, _authed, _key

KEYS = "/api/v1/jackil/integration-keys/"
DEVICES = "/api/v1/jackil/integration/devices/"
PRINTERS = {"name": "Front desk printer", "device_type": "printer",
            "location": "Lobby", "serial": "PRN-1"}


def _operator(name):
    user = get_user_model().objects.create_user(name, password="pw")
    UserProfile.objects.create(user=user, role=Role.OPERATOR)
    return user


def _writable_key(name="writable", user=None):
    """A key whose creator has been given the write flag — the state Jackil's
    inventory assistant needs, and the only one that lets it write."""
    key, raw = _key(name=name, user=user or _admin(f"{name}-admin"))
    key.can_write_devices = True
    key.save(update_fields=["can_write_devices"])
    return key, raw


def _device_url(pk):
    return f"{DEVICES}{pk}/"


class DeviceWriteTests(TestCase):
    def setUp(self):
        self.admin = _admin("inventory-admin")
        self.key, self.raw = _writable_key(user=self.admin)
        self.client = _authed(self.raw)

    def _facts(self, body):
        return {row["label"]: row["value"] for row in body["facts"]}

    def test_create_update_delete_with_the_flag(self):
        created = self.client.post(DEVICES, PRINTERS, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(UnmanagedDevice.objects.count(), 1)
        device = UnmanagedDevice.objects.get()
        self.assertEqual(str(device.id), body["id"])
        self.assertEqual(body["type"], "device")
        self.assertEqual(body["label"], "Front desk printer")
        self.assertEqual(self._facts(body), {"Type": "Printer", "Location": "Lobby",
                                            "Serial": "PRN-1"})

        patched = self.client.patch(_device_url(device.id),
                                    {"name": "Printer 2", "serial": "PRN-2"},
                                    format="json")
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(self._facts(patched.json()),
                         {"Type": "Printer", "Location": "Lobby", "Serial": "PRN-2"})
        device.refresh_from_db()
        self.assertEqual((device.name, device.serial), ("Printer 2", "PRN-2"))
        # A partial update leaves the fields it never mentioned alone.
        self.assertEqual(device.location, "Lobby")

        deleted = self.client.delete(_device_url(device.id))
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(deleted.content, b"")
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

    def test_extra_facts_round_trip(self):
        created = self.client.post(DEVICES, {"name": "NAS", "device_type": "nas",
                                             "extra": {"Rack": "B4"}}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(self._facts(created.json()), {"Rack": "B4", "Type": "NAS"})

    def test_the_write_response_is_the_detail_response(self):
        """Jackil reads a device from one shape whichever way it arrived, so
        the create response is the detail response with a 201 on it."""
        created = self.client.post(DEVICES, PRINTERS, format="json").json()
        device = UnmanagedDevice.objects.get()
        detail = self.client.get(f"/api/v1/jackil/integration/device/{device.id}/").json()
        self.assertEqual(created, detail)
        patched = self.client.patch(_device_url(device.id), {"name": "PRN"},
                                    format="json").json()
        self.assertEqual({k: v for k, v in patched.items() if k != "label"},
                         {k: v for k, v in detail.items() if k != "label"})

    def test_flag_off_is_403_and_nothing_changes(self):
        self.key.can_write_devices = False
        self.key.save(update_fields=["can_write_devices"])
        for call in (lambda: self.client.post(DEVICES, PRINTERS, format="json"),
                     lambda: self.client.patch(_device_url(UNKNOWN_HOST), {"name": "x"},
                                               format="json"),
                     lambda: self.client.delete(_device_url(UNKNOWN_HOST))):
            with self.subTest():
                response = call()
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(),
                                 {"error": "This key may not change devices"})
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

    def test_non_admin_creator_is_403_even_with_the_flag(self):
        _, raw = _writable_key(name="desk", user=_operator("oper"))
        client = _authed(raw)

        response = client.post(DEVICES, PRINTERS, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "This key may not change devices"})
        self.assertEqual(UnmanagedDevice.objects.count(), 0)
        # The same key reads exactly as well as it did.
        self.assertEqual(client.get(PING).status_code, 200)

    def test_revoked_key_is_401(self):
        device = UnmanagedDevice.objects.create(name="Printer", device_type="printer")
        self.key.revoked_at = now()
        self.key.save(update_fields=["revoked_at"])

        for call in (lambda: self.client.post(DEVICES, PRINTERS, format="json"),
                     lambda: self.client.patch(_device_url(device.id), {"name": "x"},
                                               format="json"),
                     lambda: self.client.delete(_device_url(device.id))):
            with self.subTest():
                self.assertEqual(call().status_code, 401)
        self.assertEqual(UnmanagedDevice.objects.count(), 1)
        device.refresh_from_db()
        self.assertEqual(device.name, "Printer")

    def test_session_user_cannot_use_the_write_routes(self):
        """A logged-in admin is not a key: the write routes belong to the
        integration surface, and nothing but a key opens them."""
        session = APIClient()
        session.force_login(self.admin)
        for call in (lambda: session.post(DEVICES, PRINTERS, format="json"),
                     lambda: session.patch(_device_url(UNKNOWN_HOST), {"name": "x"},
                                           format="json"),
                     lambda: session.delete(_device_url(UNKNOWN_HOST))):
            with self.subTest():
                self.assertIn(call().status_code, (401, 403))
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

    def test_bad_payload_is_400(self):
        for payload in ({"name": "Thing", "device_type": "container"},
                        {"name": "Thing", "device_type": "printer", "extra": "Rack=B4"},
                        {"name": "   ", "device_type": "printer"},
                        {"device_type": "printer"}):
            with self.subTest(payload=sorted(payload)):
                response = self.client.post(DEVICES, payload, format="json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

        device = UnmanagedDevice.objects.create(name="Printer", device_type="printer")
        bad = self.client.patch(_device_url(device.id), {"device_type": "toaster"},
                                format="json")
        self.assertEqual(bad.status_code, 400)
        device.refresh_from_db()
        self.assertEqual(device.device_type, "printer")

    def test_unknown_device_is_404(self):
        for response in (self.client.patch(_device_url(UNKNOWN_HOST), {"name": "x"},
                                           format="json"),
                         self.client.delete(_device_url(UNKNOWN_HOST)),
                         self.client.patch(_device_url("not-a-uuid"), {"name": "x"},
                                           format="json"),
                         self.client.delete(_device_url("not-a-uuid"))):
            with self.subTest(status=response.status_code):
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"error": "Not found"})

    def test_ping_lists_device_write_only_when_allowed(self):
        self.assertEqual(self.client.get(PING).json()["capabilities"],
                         ["devices", "host_facts", "device_write"])

        self.key.can_write_devices = False
        self.key.save(update_fields=["can_write_devices"])
        self.assertEqual(self.client.get(PING).json()["capabilities"],
                         ["devices", "host_facts"])

        _, operator_raw = _writable_key(name="desk", user=_operator("oper-creator"))
        self.assertEqual(_authed(operator_raw).get(PING).json()["capabilities"],
                         ["devices", "host_facts"])

    def test_reads_still_work_for_a_writing_key(self):
        device = UnmanagedDevice.objects.create(name="Printer", device_type="printer")
        response = self.client.get(f"/api/v1/jackil/integration/device/{device.id}/")
        self.assertEqual(response.status_code, 200)

    def test_get_on_a_write_route_is_405(self):
        """The read door is the detail route, not this one; a GET here is a
        caller that has the URL wrong rather than a grant it lacks."""
        device = UnmanagedDevice.objects.create(name="Printer", device_type="printer")
        self.assertEqual(self.client.get(DEVICES).status_code, 405)
        self.assertEqual(self.client.get(_device_url(device.id)).status_code, 405)


class KeyFlagEndpointTests(TestCase):
    def setUp(self):
        self.admin = _admin("boss")
        self.operator = _operator("oper")
        self.client = APIClient()

    def _login(self, user):
        self.client.force_login(user)
        return self.client

    def _toggle(self, pk, enabled):
        return self.client.post(f"{KEYS}{pk}/devices/", {"enabled": enabled},
                                format="json")

    def test_only_admins_toggle_the_flag(self):
        key, _ = _key(user=self.admin)
        self._login(self.operator)
        self.assertEqual(self._toggle(key.pk, True).status_code, 403)
        key.refresh_from_db()
        self.assertFalse(key.can_write_devices)

        self._login(self.admin)
        flipped = self._toggle(key.pk, True)
        self.assertEqual(flipped.status_code, 200, flipped.content)
        self.assertTrue(flipped.json()["can_write_devices"])
        key.refresh_from_db()
        self.assertTrue(key.can_write_devices)

        back = self._toggle(key.pk, False)
        self.assertFalse(back.json()["can_write_devices"])

    def test_another_admins_invisible_key_is_404(self):
        """Whose keys an admin may see is ``_visible_keys``' decision — the
        same one revoke already makes — and a key outside it is 404 here too,
        so the toggle cannot probe for a key on another site."""
        mine, _ = _key(name="mine", user=self.admin)
        theirs, _ = _key(name="theirs", user=_admin("peer-admin"))
        self._login(self.admin)
        self.addCleanup(mock.patch("apps.jackil.views._visible_keys",
                                   return_value=IntegrationKey.objects.filter(pk=mine.pk)
                                   ).start())

        listed = self.client.get(KEYS).json()["keys"]
        self.assertEqual([row["id"] for row in listed], [mine.pk])

        hidden = self.client.post(f"{KEYS}{theirs.pk}/devices/", {"enabled": True},
                                  format="json")
        self.assertEqual(hidden.status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.can_write_devices)

    def test_missing_or_unusable_enabled_is_400(self):
        key, _ = _key(user=self.admin)
        self._login(self.admin)
        for body in ({}, {"enabled": "yes"}, {"enabled": None},
                     {"enabled": "true"}, {"enabled": 1}):
            with self.subTest(body=body):
                response = self.client.post(f"{KEYS}{key.pk}/devices/", body,
                                            format="json")
                self.assertEqual(response.status_code, 400, response.content)
        key.refresh_from_db()
        self.assertFalse(key.can_write_devices)

    def test_key_rows_never_contain_the_hash_or_plaintext(self):
        """The mint response is the one place the plaintext appears; every row
        shape that carries the new field carries no credential with it."""
        self._login(self.admin)
        created = self.client.post(KEYS, {"name": "Front desk"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        raw = created.json()["key"]
        self.assertFalse(created.json()["can_write_devices"])

        pk = created.json()["id"]
        for row in [self._toggle(pk, True).json(), self._toggle(pk, False).json(),
                    *self.client.get(KEYS).json()["keys"]]:
            with self.subTest(keys=sorted(row)):
                self.assertIn("can_write_devices", row)
                self.assertNotIn("key", row)
                self.assertNotIn("key_hash", row)
                self.assertNotIn(raw, str(row))
