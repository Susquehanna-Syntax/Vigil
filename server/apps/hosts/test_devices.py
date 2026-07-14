from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import UnmanagedDevice


class UnmanagedDeviceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)

    def test_create_and_list_device(self):
        resp = self.client.post("/api/v1/hosts/devices/", {
            "name": "Core Switch",
            "device_type": "switch",
            "ip_address": "10.0.0.1",
            "mac_address": "aa:bb:cc:dd:ee:ff",
            "vendor": "Ubiquiti",
            "location": "Rack 1",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["device_type_label"], "Switch")

        listed = self.client.get("/api/v1/hosts/devices/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data), 1)
        self.assertEqual(listed.data[0]["name"], "Core Switch")

    def test_create_requires_name(self):
        resp = self.client.post("/api/v1/hosts/devices/", {
            "name": "   ", "device_type": "router",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.data)

    def test_invalid_device_type_rejected(self):
        resp = self.client.post("/api/v1/hosts/devices/", {
            "name": "Mystery", "device_type": "toaster",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("device_type", resp.data)

    def test_update_device(self):
        dev = UnmanagedDevice.objects.create(name="NAS", device_type="nas")
        resp = self.client.patch(
            f"/api/v1/hosts/devices/{dev.id}/",
            {"location": "Closet", "notes": "8TB RAID"}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        dev.refresh_from_db()
        self.assertEqual(dev.location, "Closet")
        self.assertEqual(dev.notes, "8TB RAID")

    def test_delete_device(self):
        dev = UnmanagedDevice.objects.create(name="Old Printer", device_type="printer")
        resp = self.client.delete(f"/api/v1/hosts/devices/{dev.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(UnmanagedDevice.objects.filter(pk=dev.id).exists())

    def test_endpoints_require_auth(self):
        anon = APIClient()
        self.assertEqual(anon.get("/api/v1/hosts/devices/").status_code, 403)
        self.assertEqual(
            anon.post("/api/v1/hosts/devices/", {"name": "x"}, format="json").status_code,
            403,
        )
