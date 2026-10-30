from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import UnmanagedDevice


class UnmanagedDeviceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        # Adding a device is admin-only, like editing one (SEC-1).
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
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

    def test_non_admin_cannot_edit_or_delete_a_device(self):
        viewer = get_user_model().objects.create_user("watcher", password="pw")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        viewer_client = APIClient()
        viewer_client.force_authenticate(viewer)
        dev = UnmanagedDevice.objects.create(name="Core Switch", location="Rack 1")

        patch = viewer_client.patch(f"/api/v1/hosts/devices/{dev.id}/",
                                    {"location": "Stolen"}, format="json")
        self.assertEqual(patch.status_code, 403, patch.data)
        self.assertEqual(viewer_client.delete(f"/api/v1/hosts/devices/{dev.id}/").status_code,
                         403)

        dev.refresh_from_db()
        self.assertEqual(dev.location, "Rack 1")
        self.assertTrue(UnmanagedDevice.objects.filter(pk=dev.id).exists())

    def test_a_viewer_may_still_read_a_device(self):
        viewer = get_user_model().objects.create_user("reader", password="pw")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        viewer_client = APIClient()
        viewer_client.force_authenticate(viewer)
        dev = UnmanagedDevice.objects.create(name="NAS", device_type="nas")

        self.assertEqual(viewer_client.get("/api/v1/hosts/devices/").status_code, 200)
        detail = viewer_client.get(f"/api/v1/hosts/devices/{dev.id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["name"], "NAS")

    def test_new_fields_round_trip(self):
        resp = self.client.post("/api/v1/hosts/devices/", {
            "name": "Edge router", "device_type": "router", "serial": "ER-0042",
            "warranty_end": "2027-04-30", "owner": "dana@example.com",
            "extra": {"Rack": "B4"},
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

        got = self.client.get(f"/api/v1/hosts/devices/{resp.data['id']}/")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data["serial"], "ER-0042")
        self.assertEqual(got.data["warranty_end"], "2027-04-30")
        self.assertEqual(got.data["owner"], "dana@example.com")
        self.assertEqual(got.data["extra"], {"Rack": "B4"})

    def test_extra_must_be_a_small_dict_of_strings(self):
        dev = UnmanagedDevice.objects.create(name="NAS", device_type="nas")
        for extra in (["Rack"], {str(n): "v" for n in range(51)}, {"Rack": 4}):
            with self.subTest(extra=str(extra)[:24]):
                resp = self.client.patch(f"/api/v1/hosts/devices/{dev.id}/",
                                         {"extra": extra}, format="json")
                self.assertEqual(resp.status_code, 400)
                self.assertIn("extra", resp.data)
