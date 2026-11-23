"""FreeBSD's pkg is a software source like dpkg or rpm (sprint M8): its items
are stored, its outdated packages are the host's pending updates, and an app
step through it is guarded to hosts that have it."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.software.guards import source_guard
from apps.software.models import PendingUpdate, SoftwareItem

TOKEN = "f" * 32


class PkgSourceTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="opnsense", agent_token=TOKEN,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        self.agent = APIClient()
        self.agent.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def test_pkg_items_are_stored_and_outdated_ones_are_pending_updates(self):
        payload = {"digest": "a" * 64, "collected_at": "2026-11-16T12:00:00Z", "errors": {}, "items": [
            {"source": "pkg", "id": "curl", "name": "curl", "version": "8.10.1", "latest": "8.11.0",
             "publisher": "sunpoet@FreeBSD.org"},
            {"source": "pkg", "id": "py311-requests", "name": "py311-requests",
             "version": "2.32.3", "latest": None, "publisher": ""},
        ]}
        resp = self.agent.post("/api/v1/checkin", {"metrics": [], "software": payload}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(sorted(SoftwareItem.objects.filter(host=self.host)
                                .values_list("package_id", flat=True)),
                         ["curl", "py311-requests"])
        self.assertEqual(list(PendingUpdate.objects.filter(host=self.host)
                              .values_list("key", flat=True)), ["curl"])

    def test_an_app_step_through_pkg_is_guarded_to_pkg_hosts(self):
        self.assertEqual(source_guard("pkg"), 'agent.pkg_manager == "pkg"')
