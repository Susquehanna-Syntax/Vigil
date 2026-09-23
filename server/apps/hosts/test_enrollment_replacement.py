"""Re-enrolling a machine adopts its existing record instead of approving a second one.

Approval is TOTP-gated; the setup below follows the API-test style of the
TOTP-gated tests in tests.py (ForceUpdateAgentTests) verbatim.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.hosts.models import Host, HostInventory
from apps.metrics.models import MetricPoint


class ApproveReplacementTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        profile = UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.secret = generate_secret()
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.user)

        self.host_a = Host.objects.create(
            hostname="repro-host",
            agent_token="a" * 32 + "0",
            machine_id="mid-1",
            status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED,
            tags=["prod"],
            agent_version="1.0.0",
        )
        self.host_b = Host.objects.create(
            hostname="repro-host-2",
            agent_token="b" * 32 + "1",
            machine_id="mid-1",
            status=Host.Status.PENDING,
            mode=Host.Mode.MANAGED,
            agent_version="1.1.0",
        )

    def _totp(self):
        return generate_totp(self.secret)

    def _approve(self, host):
        return self.client.post(
            f"/api/v1/hosts/{host.id}/approve/",
            {"totp": self._totp()},
            format="json",
        )

    def test_approving_a_re_enrolment_keeps_the_original_record(self):
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Host.objects.count(), 1)
        survivor = Host.objects.get()
        self.assertEqual(survivor.pk, self.host_a.pk)
        self.assertEqual(survivor.agent_token, self.host_b.agent_token)

    def test_the_duplicate_row_is_gone_after_approval(self):
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Host.objects.filter(pk=self.host_b.pk).exists())

    def test_metric_history_follows_the_surviving_record(self):
        MetricPoint.objects.create(
            host=self.host_a, time=now(), category="cpu", metric="usage_percent", value=1.0
        )
        MetricPoint.objects.create(
            host=self.host_b, time=now(), category="cpu", metric="usage_percent", value=2.0
        )
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        survivor = Host.objects.get()
        self.assertEqual(
            MetricPoint.objects.filter(host=survivor).count(), 2,
            "both metric points must belong to the surviving record",
        )

    def test_tags_and_id_survive_the_replacement(self):
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        survivor = Host.objects.get()
        self.assertEqual(survivor.tags, ["prod"])
        self.assertEqual(resp.data["id"], str(self.host_a.pk))

    def test_a_first_enrolment_is_unaffected(self):
        other = Host.objects.create(
            hostname="solo",
            agent_token="c" * 32 + "2",
            machine_id="mid-solo",
            status=Host.Status.PENDING,
            mode=Host.Mode.MONITOR,
        )
        resp = self._approve(other)
        self.assertEqual(resp.status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.status, Host.Status.ONLINE)
        self.assertEqual(resp.data["id"], str(other.pk))

    def test_an_empty_machine_id_never_matches(self):
        self.host_a.machine_id = ""
        self.host_a.save()
        self.host_b.machine_id = ""
        self.host_b.save()
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Host.objects.count(), 2)
        b = Host.objects.get(pk=self.host_b.pk)
        self.assertEqual(b.status, Host.Status.ONLINE)

    def test_a_second_pending_row_for_the_same_machine_is_cleared(self):
        host_c = Host.objects.create(
            hostname="repro-host-stale",
            agent_token="d" * 32 + "3",
            machine_id="mid-1",
            status=Host.Status.PENDING,
            mode=Host.Mode.MONITOR,
        )
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Host.objects.count(), 1)
        self.assertFalse(Host.objects.filter(pk=host_c.pk).exists())
        self.assertEqual(Host.objects.get().pk, self.host_a.pk)

    def test_check_pending_reports_what_it_replaces(self):
        resp = self.client.post(
            "/api/v1/hosts/check-pending/",
            {"token": self.host_b.agent_token},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(resp.data["replaces"]["hostname"], self.host_a.hostname)
        self.assertEqual(resp.data["replaces"]["id"], str(self.host_a.pk))

    def test_check_pending_reports_null_when_nothing_is_replaced(self):
        solo = Host.objects.create(
            hostname="solo",
            agent_token="e" * 32 + "4",
            machine_id="mid-solo",
            status=Host.Status.PENDING,
            mode=Host.Mode.MONITOR,
        )
        resp = self.client.post(
            "/api/v1/hosts/check-pending/",
            {"token": solo.agent_token},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["replaces"])

    def test_inventory_follows_when_the_survivor_has_none(self):
        HostInventory.objects.create(host=self.host_b)
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        survivor = Host.objects.get()
        self.assertEqual(HostInventory.objects.filter(host=survivor).count(), 1)

    def test_inventory_of_the_survivor_is_kept(self):
        HostInventory.objects.create(host=self.host_a)
        inv_b = HostInventory.objects.create(host=self.host_b)
        resp = self._approve(self.host_b)
        self.assertEqual(resp.status_code, 200)
        survivor = Host.objects.get()
        self.assertEqual(HostInventory.objects.filter(host=survivor).count(), 1)
        self.assertFalse(HostInventory.objects.filter(pk=inv_b.pk).exists())
