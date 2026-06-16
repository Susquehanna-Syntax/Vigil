from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host


class MetricHistoryParamGuardTests(TestCase):
    """Untrusted query params must 400, never 500."""

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32, status=Host.Status.ONLINE,
        )
        self.base = f"/api/v1/metrics/{self.host.id}/cpu/usage/"

    def test_valid_request_ok(self):
        self.assertEqual(self.client.get(self.base).status_code, 200)

    def test_non_numeric_limit_is_400(self):
        self.assertEqual(self.client.get(self.base + "?limit=abc").status_code, 400)

    def test_malformed_timestamp_is_400(self):
        self.assertEqual(self.client.get(self.base + "?from=notadate").status_code, 400)

    def test_limit_is_capped(self):
        # Over-cap limit shouldn't error; it's clamped server-side.
        self.assertEqual(self.client.get(self.base + "?limit=999999").status_code, 200)
