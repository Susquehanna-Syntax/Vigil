"""The core host endpoints phase 42 typed: each body against the serializer its
``@extend_schema`` names, with data present so a list's item shape is checked."""
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host, UnmanagedDevice
from vigil.schema_check import assert_shape

from .api_schema import (
    HostsAdConfigSerializer,
    HostsDeviceSerializer,
    HostsErrorSerializer,
    HostsHostSerializer,
)


class HostsCoreApiSchemaTests(TestCase):

    def setUp(self):
        admin = get_user_model().objects.create_user("p42admin", password="x")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(admin)
        self.host = Host.objects.create(hostname="web-42", agent_token="p42a" * 8,
                                        status=Host.Status.ONLINE, os="Ubuntu 24.04",
                                        ip_address="10.0.0.42", tags=["prod"])
        self.device = UnmanagedDevice.objects.create(name="edge-router", device_type="router",
                                                     ip_address="10.0.0.1")

    def ok(self, resp, serializer, status=200):
        self.assertEqual(resp.status_code, status, resp.content)
        assert_shape(self, resp.json(), serializer)

    def test_hosts_read_and_retag_as_documented(self):
        self.ok(self.api.get("/api/v1/hosts/"), HostsHostSerializer(many=True))
        self.ok(self.api.get(f"/api/v1/hosts/{self.host.id}/"), HostsHostSerializer)
        self.ok(self.api.patch(f"/api/v1/hosts/{self.host.id}/tags/", {"tags": ["prod", "web"]},
                               format="json"), HostsHostSerializer)
        self.ok(self.api.patch(f"/api/v1/hosts/{self.host.id}/tags/", {"tags": "web"},
                               format="json"), HostsErrorSerializer, 400)
        self.ok(self.api.get(f"/api/v1/hosts/{uuid.uuid4()}/"), HostsErrorSerializer, 404)

    def test_rejecting_a_pending_host_returns_the_host(self):
        pending = Host.objects.create(hostname="new-42", agent_token="p42b" * 8,
                                      status=Host.Status.PENDING)
        self.ok(self.api.post(f"/api/v1/hosts/{pending.id}/reject/"), HostsHostSerializer)
        # Rejected now, so a second rejection finds nothing pending.
        self.ok(self.api.post(f"/api/v1/hosts/{pending.id}/reject/"), HostsErrorSerializer, 404)

    def test_unmanaged_devices_as_documented(self):
        self.ok(self.api.get("/api/v1/hosts/devices/"), HostsDeviceSerializer(many=True))
        self.ok(self.api.post("/api/v1/hosts/devices/",
                              {"name": "lobby-printer", "device_type": "printer"},
                              format="json"), HostsDeviceSerializer, 201)
        # DRF's field errors: keys are the refused fields, so no fixed shape.
        refused = self.api.post("/api/v1/hosts/devices/", {"device_type": "printer"},
                                format="json")
        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIsInstance(refused.json()["name"], list)
        self.ok(self.api.get(f"/api/v1/hosts/devices/{self.device.id}/"), HostsDeviceSerializer)
        self.ok(self.api.patch(f"/api/v1/hosts/devices/{self.device.id}/",
                               {"location": "Rack B4"}, format="json"), HostsDeviceSerializer)
        self.ok(self.api.get(f"/api/v1/hosts/devices/{uuid.uuid4()}/"), HostsErrorSerializer, 404)

    def test_the_directory_settings_as_documented(self):
        self.ok(self.api.get("/api/v1/hosts/ad/"), HostsAdConfigSerializer)
