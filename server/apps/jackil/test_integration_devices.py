"""Devices and richer host facts, through a key's eyes.

The device half is the shape Jackil's inventory pages read; the host half is
what its link preview draws. Both ride the same three endpoints as every
other kind, so this file only has to ask those endpoints.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host, UnmanagedDevice
from apps.metrics.models import MetricPoint

from .test_integration import RESOLVE, SEARCH, UNKNOWN_HOST, _admin, _authed, _detail, _key


class DeviceReadTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("inventory-admin"))
        self.device = UnmanagedDevice.objects.create(
            name="Core switch", device_type="switch", ip_address="10.0.0.9",
            mac_address="aa:bb:cc:dd:ee:ff", vendor="Ubiquiti", location="Rack 1",
            serial="CS-9001", warranty_end="2028-06-30", owner="dana@example.com",
            extra={"Rack": "B4", "Asset tag": "AST-11"})

    def _facts(self, url):
        body = self.client.get(url).json()
        return {row["label"]: row["value"] for row in body["facts"]}, body

    def test_search_finds_devices_by_serial(self):
        results = self.client.get(SEARCH, {"q": "CS-9001", "type": "device"}).json()["results"]
        self.assertEqual([row["type"] for row in results], ["device"])
        row = results[0]
        self.assertEqual(row["id"], str(self.device.id))
        self.assertEqual((row["label"], row["sub"], row["status"]),
                         ("Core switch", "Switch · 10.0.0.9", "Switch"))
        self.assertTrue(row["url"].endswith(f"#open/device/{self.device.id}"), row["url"])

    def test_resolve_and_detail_devices(self):
        resolved = self.client.get(RESOLVE, {"type": "device",
                                             "ids": str(self.device.id)}).json()
        self.assertEqual(list(resolved["items"]), [str(self.device.id)])

        facts, _ = self._facts(_detail("device", self.device.id))
        self.assertEqual(facts["Serial"], "CS-9001")
        self.assertEqual(facts["Owner"], "dana@example.com")
        self.assertEqual(facts["Warranty ends"], "2028-06-30")

    def test_the_extra_facts_come_last_in_key_order(self):
        facts = [row["label"] for row in
                 self.client.get(_detail("device", self.device.id)).json()["facts"]]
        self.assertEqual(facts[-2:], ["Asset tag", "Rack"])

    def test_empty_device_facts_are_omitted_not_blank(self):
        bare = UnmanagedDevice.objects.create(name="Printer", device_type="printer")
        facts, _ = self._facts(_detail("device", bare.id))
        self.assertEqual(sorted(facts), ["Type"], f"{facts} should hold only the type")

    def test_unknown_device_id_is_404(self):
        missing = self.client.get(_detail("device", UNKNOWN_HOST))
        malformed = self.client.get(_detail("device", "not-a-uuid"))
        for response in (missing, malformed):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"error": "Not found"})
        self.assertEqual(missing.content, malformed.content)


class HostFactTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("facts-admin"))
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32,
                                        os="Debian 13", ip_address="10.0.0.5")

    def _point(self, category, value, ago_minutes):
        MetricPoint.objects.create(host=self.host, category=category,
                                   metric="usage_percent", value=value,
                                   time=now() - timedelta(minutes=ago_minutes))

    def _body(self):
        return self.client.get(_detail("host", self.host.id)).json()

    def _facts(self):
        return {row["label"]: row["value"] for row in self._body()["facts"]}

    def test_host_detail_has_cpu_memory_alerts_and_series(self):
        self._point("cpu", 40.0, 10)
        self._point("cpu", 42.4, 5)
        self._point("memory", 61.0, 5)
        rule = AlertRule.objects.create(name="Hot", category="cpu", metric="cpu",
                                       operator="gt", threshold=90, severity="warning")
        Alert.objects.create(host=self.host, rule=rule, severity="warning",
                             message="web1 is hot")

        facts = self._facts()
        self.assertEqual(facts["CPU"], "42%")
        self.assertEqual(facts["Memory"], "61%")
        self.assertEqual(facts["Open alerts"], "1")

        series = self._body()["cpu_series"]
        self.assertEqual([point["v"] for point in series], [40.0, 42.4])
        self.assertEqual(sorted(series[0]), ["t", "v"])
        self.assertLess(series[0]["t"], series[1]["t"], "oldest first")

    def test_host_detail_without_metrics_omits_cpu_and_memory(self):
        facts = self._facts()
        self.assertNotIn("CPU", facts)
        self.assertNotIn("Memory", facts)
        self.assertEqual(facts["Open alerts"], "0")
        self.assertEqual(self._body()["cpu_series"], [])

    def test_a_stale_metric_is_not_reported_as_current(self):
        self._point("cpu", 71.0, 40)
        self.assertNotIn("CPU", self._facts())

    def test_a_resolved_alert_is_not_open(self):
        rule = AlertRule.objects.create(name="Cold", category="cpu", metric="cpu",
                                        operator="lt", threshold=1, severity="warning")
        Alert.objects.create(host=self.host, rule=rule, severity="warning",
                             message="settled", state=Alert.State.RESOLVED)
        self.assertEqual(self._facts()["Open alerts"], "0")

    def test_only_hosts_carry_a_cpu_series(self):
        device = UnmanagedDevice.objects.create(name="UPS", device_type="ups")
        self.assertNotIn("cpu_series",
                         self.client.get(_detail("device", device.id)).json())

    def test_the_series_is_thinned_to_sixty_points(self):
        for seconds in range(3540, -1, -59):
            MetricPoint.objects.create(host=self.host, category="cpu",
                                       metric="usage_percent", value=float(seconds),
                                       time=now() - timedelta(seconds=seconds))
        series = self._body()["cpu_series"]
        self.assertEqual(len(series), 60)
        self.assertLess(series[0]["t"], series[-1]["t"], "oldest first")


class PingCapabilityTests(TestCase):
    def test_ping_lists_capabilities(self):
        client = APIClient()
        _, raw = _key(user=_admin("ping-admin"))
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        body = client.get("/api/v1/jackil/integration/ping/").json()
        self.assertEqual(body["capabilities"], ["devices", "host_facts"])
