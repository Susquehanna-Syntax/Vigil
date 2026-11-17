"""An SNMP check's interface table as samples and rates (S9, phase 05d): the
``interfaces`` param, ingest into ``InterfaceSample``, retention with the other
metric points, and the two read endpoints that work the rates out on read.

The GET side of an SNMP check — params other than ``interfaces``, the community
column, the latest-values list answer — is apps.hosts.test_device_check_snmp;
nothing here repeats it, so an edit there that touches interfaces fails one of
the two files and never both.
"""

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.crypto import encrypt_secret
from apps.hosts.device_checks import store_check_results, validate_params
from apps.hosts.models import (
    CheckResult,
    DeviceCheck,
    Host,
    InterfaceSample,
    UnmanagedDevice,
)

COMMUNITY = "s3cret-community"

#: A counter value a sample carries when the test is not about counters at all.
_OCTETS = 1_000_000


def make_host(hostname, token, poller=False):
    return Host.objects.create(hostname=hostname, agent_token=token, status=Host.Status.ONLINE,
                               mode=Host.Mode.MONITOR, agent_poller=poller)


def _snmp_params(**extra):
    params = {"host": "10.0.0.5"}
    params.update(extra)
    return params


def interface(index=2, name="eth2", up=True, speed_mbps=1000, in_octets=1000, out_octets=2000,
              **extra):
    entry = {"index": index, "name": name, "up": up, "speed_mbps": speed_mbps,
             "in_octets": in_octets, "out_octets": out_octets}
    entry.update(extra)
    return entry


class InterfacesParamTests(TestCase):
    def test_a_walk_is_stored_and_its_absence_or_false_is_not(self):
        self.assertEqual(validate_params("snmp", _snmp_params(interfaces=True)),
                         {"host": "10.0.0.5", "interfaces": True})
        self.assertEqual(validate_params("snmp", _snmp_params(interfaces=False)),
                         {"host": "10.0.0.5"})
        self.assertEqual(validate_params("snmp", _snmp_params()), {"host": "10.0.0.5"})

    def test_a_non_boolean_is_refused_with_one_sentence(self):
        for bad in ("yes", 1, None, [True]):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                validate_params("snmp", _snmp_params(interfaces=bad))
            self.assertEqual(str(caught.exception), "interfaces must be true or false")

    def test_interfaces_is_a_reserved_label(self):
        with self.assertRaises(ValueError) as caught:
            validate_params("snmp", _snmp_params(oids=[{"oid": "1.3.6.1.2.1.1.5.0",
                                                        "label": "interfaces"}]))
        self.assertEqual(str(caught.exception), "oids: interfaces is a reserved label")


class InterfaceIngestTests(TestCase):
    """A poller's result line carries the walk; the sample rows follow it."""

    def setUp(self):
        self.poller = make_host("p1", "tok-p1-" + "h" * 32, poller=True)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.check = DeviceCheck.objects.create(
            device=self.device, kind=DeviceCheck.Kind.SNMP,
            params=_snmp_params(interfaces=True), secret=encrypt_secret(COMMUNITY),
            poller=self.poller)
        self.icmp = DeviceCheck.objects.create(
            device=self.device, kind=DeviceCheck.Kind.ICMP, params={"host": "10.0.0.6"},
            poller=self.poller)

    def _store(self, check=None, **row):
        line = {"check": str((check or self.check).id), "ok": True, "latency_ms": 4.0,
                "detail": "up", **row}
        return store_check_results(self.poller, [line])

    def test_the_samples_are_stored_with_their_result_time(self):
        at = timezone.now() - timedelta(seconds=5)
        self.assertEqual(self._store(interfaces=[interface(), interface(index=3, name="eth3")],
                                     at=at.isoformat()), 1)
        samples = InterfaceSample.objects.order_by("if_index")
        self.assertEqual([s.if_index for s in samples], [2, 3])
        self.assertEqual(samples[0].name, "eth2")
        self.assertTrue(samples[0].up)
        self.assertEqual(samples[0].speed_mbps, 1000)
        self.assertEqual((samples[0].in_octets, samples[0].out_octets), (1000, 2000))
        for sample in samples:
            self.assertEqual(sample.time.replace(microsecond=0), at.replace(microsecond=0))

    def test_a_malformed_entry_is_dropped_and_the_good_ones_survive(self):
        self._store(interfaces=[
            "not a dict",
            interface(index="3"),
            interface(index=True),
            interface(index=0),
            interface(index=2147483648),
            interface(index=4, name=None, up="yes", in_octets=True,
                     out_octets=9223372036854775808),
            interface(index=4, name="duplicate"),
            interface(index=5, name="x" * 140),
        ])
        samples = InterfaceSample.objects.order_by("if_index")
        self.assertEqual([s.if_index for s in samples], [4, 5])
        first = samples[0]
        self.assertEqual(first.name, "")          # a name that is not text is no name
        self.assertIsNone(first.up)               # and so is an up that is not true or false
        self.assertIsNone(first.in_octets)        # a bool is not a counter
        self.assertIsNone(first.out_octets)       # past the signed range: dropped, not wrapped
        self.assertEqual(samples[1].name, "x" * 100)

    def test_more_than_sixty_four_interfaces_are_cut_to_the_first_64(self):
        self._store(interfaces=[interface(index=n) for n in range(1, 100)])
        self.assertEqual(InterfaceSample.objects.count(), 64)
        self.assertTrue(InterfaceSample.objects.filter(if_index=64).exists())

    def test_a_non_snmp_checks_interfaces_are_ignored(self):
        self._store(self.icmp, interfaces=[interface()])
        self.assertEqual(CheckResult.objects.count(), 1)
        self.assertEqual(InterfaceSample.objects.count(), 0)

    def test_a_result_without_interfaces_stores_no_samples(self):
        self._store()
        self.assertEqual(CheckResult.objects.count(), 1)
        self.assertEqual(InterfaceSample.objects.count(), 0)

    def test_the_walk_is_not_copied_into_the_results_extra(self):
        self._store(interfaces=[interface()])
        self.assertNotIn("interfaces", CheckResult.objects.get().extra)


class InterfaceApiTests(TestCase):
    """The two read endpoints: the table of what interfaces do now, and one's series."""

    def setUp(self):
        admin = get_user_model().objects.create_user(username="admin-" + uuid4().hex[:8])
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(admin)
        self.device = UnmanagedDevice.objects.create(name="sw-01", device_type="switch")
        self.check = DeviceCheck.objects.create(
            device=self.device, kind=DeviceCheck.Kind.SNMP,
            params=_snmp_params(interfaces=True), secret=encrypt_secret(COMMUNITY))
        self.icmp = DeviceCheck.objects.create(device=self.device, kind=DeviceCheck.Kind.ICMP,
                                               params={"host": "10.0.0.6"})
        self.url = f"/api/v1/hosts/devices/{self.device.id}/checks/{self.check.id}/interfaces/"

    def _sample(self, index, seconds_ago, name="eth2", up=True, speed_mbps=1000, check=None,
                **counters):
        """A sample carrying both counters unless a test names one differently."""
        return InterfaceSample.objects.create(
            device_check=check or self.check, time=timezone.now() - timedelta(seconds=seconds_ago),
            if_index=index, name=name, up=up, speed_mbps=speed_mbps,
            in_octets=counters.get("in_octets", _OCTETS),
            out_octets=counters.get("out_octets", _OCTETS))

    def test_the_table_answers_the_newest_sample_with_rates_over_300_seconds(self):
        # 3,000,000 octets more in 300 s is 80,000 bits a second... and half that out.
        self._sample(2, 300, in_octets=1_000_000, out_octets=2_000_000)
        self._sample(2, 0, in_octets=4_000_000, out_octets=3_500_000, name="eth2.100")
        self._sample(3, 0, in_octets=7, out_octets=8, name="vlan1", up=False, speed_mbps=None)
        answer = self.client.get(self.url)
        self.assertEqual(answer.status_code, 200)
        rows = answer.json()
        self.assertEqual([r["index"] for r in rows], [2, 3])
        self.assertEqual(rows[0]["name"], "eth2.100")
        self.assertEqual(rows[0]["in_bps"], 80_000)
        self.assertEqual(rows[0]["out_bps"], 40_000)
        self.assertTrue(rows[0]["up"])
        self.assertEqual(rows[0]["speed_mbps"], 1000)
        self.assertIsNone(rows[1]["in_bps"])    # no previous sample: no rate, not zero
        self.assertIsNone(rows[1]["out_bps"])
        self.assertFalse(rows[1]["up"])
        self.assertIsNone(rows[1]["speed_mbps"])

    def test_a_counter_that_went_backwards_or_is_missing_has_no_rate(self):
        # in_octets went down: a reboot reset the counter. out_octets is None
        # on the newest sample: this device stopped reporting it.
        self._sample(2, 300, in_octets=9_000_000, out_octets=1)
        self._sample(2, 0, in_octets=1_000, out_octets=None)
        rows = self.client.get(self.url).json()
        self.assertIsNone(rows[0]["in_bps"])    # a reboot reset the counter
        self.assertIsNone(rows[0]["out_bps"])   # and this device never reported out_octets

    def test_a_sample_outside_the_window_is_not_read(self):
        self._sample(2, 3600 * 30, in_octets=1, out_octets=1)
        self.assertEqual(self.client.get(self.url + "?hours=1").json(), [])

    def test_another_kind_of_check_has_no_interfaces(self):
        self._sample(2, 0, check=self.icmp)
        answer = self.client.get(
            f"/api/v1/hosts/devices/{self.device.id}/checks/{self.icmp.id}/interfaces/")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json(), [])

    def test_the_series_is_oldest_first_with_one_point_per_pair(self):
        # 500_000 octets in 100 s is 40_000 bits a second, then 3_000_000 in
        # 100 s is 240_000. The interface's two counters always move together.
        for seconds_ago, octets in ((300, 1_000_000), (200, 1_500_000), (100, 4_500_000)):
            self._sample(2, seconds_ago, in_octets=octets, out_octets=octets)
        self._sample(3, 100)  # another interface is not in this answer
        answer = self.client.get(self.url + "2/")
        self.assertEqual(answer.status_code, 200)
        body = answer.json()
        self.assertEqual(body["index"], 2)
        self.assertEqual(body["name"], "eth2")
        self.assertEqual([p["in_bps"] for p in body["series"]], [40_000, 240_000])
        self.assertEqual(len(body["series"]), 2)

    def test_a_missing_interface_or_check_is_404(self):
        self.assertEqual(self.client.get(self.url + "7/").status_code, 404)
        missing = uuid4()
        self.assertEqual(
            self.client.get(f"/api/v1/hosts/devices/{self.device.id}/checks/{missing}/interfaces/"
                            ).status_code, 404)
        self.assertEqual(self.client.get(self.url + "2/?hours=0").status_code, 400)
        self.assertEqual(self.client.get(self.url + "2/?hours=0").json()["error"],
                         "hours must be a whole number from 1 to 720")

    def test_the_community_is_in_neither_answer(self):
        self._sample(2, 300)
        self._sample(2, 0)
        for url in (self.url, self.url + "2/"):
            with self.subTest(url=url):
                answer = self.client.get(url)
                self.assertEqual(answer.status_code, 200)
                self.assertNotIn(COMMUNITY.encode(), answer.content)
                self.assertNotIn(b'"community"', answer.content)


class InterfaceRetentionTests(TestCase):
    def test_old_samples_are_pruned_with_the_other_metric_points(self):
        from apps.alerts import tasks as alert_tasks

        device = UnmanagedDevice.objects.create(name="core-switch")
        check = DeviceCheck.objects.create(device=device, kind="snmp", params=_snmp_params())
        old_time = timezone.now() - timedelta(days=400)
        old = InterfaceSample.objects.create(device_check=check, time=old_time,
                                             if_index=2, in_octets=1, out_octets=1)
        fresh = InterfaceSample.objects.create(device_check=check, time=timezone.now(),
                                               if_index=2, in_octets=2, out_octets=2)
        CheckResult.objects.create(device_check=check, time=old_time, ok=True)
        for hypertable in (False, True):
            with self.subTest(hypertable=hypertable), mock.patch.object(
                    alert_tasks, "_metricpoint_is_hypertable", return_value=hypertable):
                alert_tasks.prune_old_metric_points()
                self.assertFalse(InterfaceSample.objects.filter(pk=old.pk).exists())
                self.assertTrue(InterfaceSample.objects.filter(pk=fresh.pk).exists())
