"""Checks on agent-less devices (S9, phase 02): validation, the signed hand-off to a poller in
the check-in response, result ingest, and the device-check API."""

import base64
import json
from datetime import timedelta
from unittest import skipUnless
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from nacl.exceptions import BadSignatureError
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.device_checks import (assigned_poller, checks_for, validate_interval,
                                      validate_params)
from apps.hosts.models import CheckResult, DeviceCheck, Host, UnmanagedDevice
from vigil import scoping
from vigil.signing import get_signing_key


def make_host(hostname, token, poller=False, status=Host.Status.ONLINE):
    return Host.objects.create(hostname=hostname, agent_token=token, status=status,
                               mode=Host.Mode.MONITOR, agent_poller=poller)


def make_device(name="sw-01", site_id=None):
    return UnmanagedDevice.objects.create(name=name, device_type="switch", site_id=site_id)


def make_check(device, poller=None, kind=DeviceCheck.Kind.ICMP, params=None):
    return DeviceCheck.objects.create(device=device, kind=kind,
                                      params=params or {"host": "10.0.0.1"},
                                      poller=poller)


def checkin(client, host, body=None):
    return client.post("/api/v1/checkin", body or {"hostname": host.hostname},
                       content_type="application/json",
                       HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}")


def iso(delta=timedelta(0)):
    return (timezone.now() - delta).isoformat()


class ValidateParamsTests(TestCase):
    def test_good_input_is_cleaned_and_unknown_keys_dropped(self):
        self.assertEqual(validate_params("icmp", {"host": "10.0.0.1", "count": 3, "junk": 1}),
                         {"host": "10.0.0.1", "count": 3})
        self.assertEqual(validate_params("tcp", {"host": "db.lan", "port": 5432}),
                         {"host": "db.lan", "port": 5432})
        self.assertEqual(
            validate_params("http", {"url": "https://wiki.lan/x", "expect_status": [200, 301],
                                     "verify_tls": False, "expect_cert_days": 14}),
            {"url": "https://wiki.lan/x", "expect_status": [200, 301],
             "verify_tls": False, "expect_cert_days": 14})
        self.assertEqual(validate_params("dns", {"name": "example.com", "expect": ["93.184.216.34"]}),
                         {"name": "example.com", "expect": ["93.184.216.34"]})

    def test_refusals_raise_value_error(self):
        refused = [
            ("http", {"url": "ftp://files.lan/"}),
            ("http", {"url": "https://"}),
            ("icmp", {"host": "-rf"}),
            ("icmp", {"host": "10.0.0.1", "count": 11}),
            ("tcp", {"host": "10.0.0.1", "port": 0}),
            ("dns", {"name": "x y"}),
            ("dns", {"name": "example.com", "expect": "1.2.3.4"}),
            ("snmp", {"host": "10.0.0.1", "version": "1"}),  # 05b: only v2c, host alone is valid
        ]
        for kind, params in refused:
            with self.subTest(kind=kind, params=params), self.assertRaises(ValueError):
                validate_params(kind, params)

    def test_interval_must_be_30_to_86400(self):
        self.assertEqual(validate_interval(60), 60)
        for bad in (10, 86401, True, "60"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_interval(bad)


class UrlCredentialTests(TestCase):
    def test_an_http_url_with_a_user_name_or_password_is_refused(self):
        for url in ("https://admin:hunter2@router.lan/", "http://admin@printer.lan/status"):
            with self.subTest(url), self.assertRaises(ValueError) as caught:
                validate_params("http", {"url": url})
            self.assertIn("user name or password", str(caught.exception))


class PollerFlagTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = make_host("p1", "tok-p1-" + "a" * 32)

    def test_poller_true_sets_the_flag(self):
        checkin(self.client, self.host, {"hostname": "p1", "poller": True})
        self.host.refresh_from_db()
        self.assertTrue(self.host.agent_poller)

    def test_absent_key_leaves_the_flag_alone(self):
        self.host.agent_poller = True
        self.host.save()
        checkin(self.client, self.host, {"hostname": "p1"})
        self.host.refresh_from_db()
        self.assertTrue(self.host.agent_poller)

    def test_a_non_boolean_is_ignored(self):
        checkin(self.client, self.host, {"hostname": "p1", "poller": "true"})
        self.host.refresh_from_db()
        self.assertFalse(self.host.agent_poller)

    def test_the_dashboard_cannot_set_it(self):
        admin = get_user_model().objects.create_user(username="admin")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        client = APIClient()
        client.force_authenticate(admin)
        client.patch(f"/api/v1/hosts/{self.host.id}/tags/",
                     {"tags": ["x"], "agent_poller": True}, format="json")
        self.host.refresh_from_db()
        self.assertFalse(self.host.agent_poller)


class SignedCheckListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.device = make_device()
        self.poller = make_host("p1", "tok-p1-" + "b" * 32, poller=True)
        self.check = make_check(self.device, params={"host": "10.0.0.7", "count": 2})

    def test_a_poller_gets_its_check_with_a_signature_that_verifies(self):
        body = checkin(self.client, self.poller).json()
        self.assertEqual(body["checks"], [{"id": str(self.check.id), "kind": "icmp",
                                           "params": {"host": "10.0.0.7", "count": 2},
                                           "interval": 60}])
        payload = {"v": 1, "type": "check-list", "agent": self.poller.token_fingerprint,
                   "issued": body["checks_issued"], "checks": body["checks"]}
        get_signing_key().verify_key.verify(
            json.dumps(payload, sort_keys=True).encode(),
            base64.b64decode(body["checks_sig"]))

    def test_a_signature_does_not_verify_for_another_agent(self):
        body = checkin(self.client, self.poller).json()
        other = {"v": 1, "type": "check-list", "agent": "e" * 64,
                 "issued": body["checks_issued"], "checks": body["checks"]}
        with self.assertRaises(BadSignatureError):
            get_signing_key().verify_key.verify(
                json.dumps(other, sort_keys=True).encode(),
                base64.b64decode(body["checks_sig"]))

    def test_a_host_that_is_not_a_poller_gets_no_checks_key(self):
        plain = make_host("plain", "tok-plain-" + "c" * 32)
        body = checkin(self.client, plain).json()
        self.assertNotIn("checks", body)
        self.assertNotIn("checks_sig", body)

    def test_the_checks_are_sorted_by_id(self):
        make_check(self.device, params={"host": "10.0.0.8"})
        make_check(self.device, params={"host": "10.0.0.9"})
        body = checkin(self.client, self.poller).json()
        ids = [c["id"] for c in body["checks"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), 3)


@skipUnless(settings.BUSINESS_INSTALLED, "needs the vigil-business package")
class SiteAssignmentTests(TestCase):
    def test_a_poller_of_another_site_does_not_get_the_check(self):
        from apps_business.sites.models import HostSiteAssignment, Site

        site_a = Site.objects.create(name="A", slug="a")
        site_b = Site.objects.create(name="B", slug="b")
        device = make_device("sw-a", site_id=site_a.id)
        check = make_check(device)
        poller_a = make_host("pa", "tok-pa-" + "d" * 32, poller=True)
        poller_b = make_host("pb", "tok-pb-" + "e" * 32, poller=True)
        HostSiteAssignment.objects.create(host=poller_a, site=site_a)
        HostSiteAssignment.objects.create(host=poller_b, site=site_b)
        with scoping.system(reason="test"):
            self.assertEqual(assigned_poller(check), poller_a)
            self.assertEqual([c.id for c in checks_for(poller_a)], [check.id])
            self.assertEqual(checks_for(poller_b), [])

    def test_a_global_device_goes_only_to_a_global_poller(self):
        """Every site sees a global device, so one site's poller must not be
        the one reporting on it."""
        from apps_business.sites.models import HostSiteAssignment, Site

        site_b = Site.objects.create(name="B", slug="b")
        check = make_check(make_device("website"))  # site_id None: global
        poller_b = make_host("pb", "tok-pb-" + "e" * 32, poller=True)
        HostSiteAssignment.objects.create(host=poller_b, site=site_b)
        with scoping.system(reason="test"):
            self.assertIsNone(assigned_poller(check))
            self.assertEqual(checks_for(poller_b), [])
            poller_g = make_host("pg", "tok-pg-" + "f" * 32, poller=True)  # unassigned: global
            self.assertEqual(assigned_poller(check), poller_g)
            self.assertEqual([c.id for c in checks_for(poller_g)], [check.id])


class AssignmentTests(TestCase):
    def setUp(self):
        self.device = make_device()
        self.p1 = make_host("p1", "tok-p1-" + "f" * 32, poller=True)
        self.p2 = make_host("p2", "tok-p2-" + "g" * 32, poller=True)
        self.check = make_check(self.device)

    def test_the_lowest_id_poller_is_assigned_and_moves_when_it_goes_offline(self):
        lowest, other = sorted([self.p1, self.p2], key=lambda h: h.id)
        with scoping.system(reason="test"):
            self.assertEqual(assigned_poller(self.check), lowest)
            self.assertEqual(checks_for(other), [])
            lowest.status = Host.Status.OFFLINE
            lowest.save()
            self.assertEqual(assigned_poller(self.check), other)
            self.assertEqual([c.id for c in checks_for(other)], [self.check.id])

    def test_a_pinned_poller_wins_while_it_is_eligible(self):
        pinned = self.p2 if sorted([self.p1, self.p2], key=lambda h: h.id)[0] == self.p1 else self.p1
        self.check.poller = pinned
        self.check.save()
        with scoping.system(reason="test"):
            self.assertEqual(assigned_poller(self.check), pinned)

    def test_a_disabled_check_is_not_handed_out(self):
        self.check.enabled = False
        self.check.save()
        with scoping.system(reason="test"):
            self.assertEqual(checks_for(self.p1), [])
            self.assertEqual(checks_for(self.p2), [])


class CheckResultIngestTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.device = make_device()
        self.poller = make_host("p1", "tok-p1-" + "h" * 32, poller=True)
        self.other = make_host("p2", "tok-p2-" + "i" * 32, poller=True)
        self.mine = make_check(self.device, poller=self.poller)
        self.theirs = make_check(self.device, poller=self.other)

    def _row(self, check_id, **extra):
        row = {"check": str(check_id), "ok": True, "latency_ms": 12.5,
               "detail": "ok", "at": iso(timedelta(seconds=5))}
        row.update(extra)
        return row

    def test_results_stored_only_for_checks_this_poller_is_given(self):
        checkin(self.client, self.poller, {"hostname": "p1", "check_results": [
            self._row(self.mine.id),
            self._row(self.theirs.id),
            self._row(uuid4()),
        ]})
        stored = CheckResult.objects.all()
        self.assertEqual(stored.count(), 1)
        self.assertEqual(stored.get().device_check, self.mine)
        self.assertEqual(stored.get().latency_ms, 12.5)

    def test_detail_is_cut_to_300_and_extra_keys_kept(self):
        checkin(self.client, self.poller, {"hostname": "p1", "check_results": [
            self._row(self.mine.id, detail="x" * 400, status_code=200, body={"nested": 1})]})
        result = CheckResult.objects.get()
        self.assertEqual(len(result.detail), 300)
        self.assertEqual(result.extra, {"status_code": 200})

    def test_a_future_or_stale_time_is_stored_as_now(self):
        checkin(self.client, self.poller, {"hostname": "p1", "check_results": [
            self._row(self.mine.id, at=iso(-timedelta(days=1))),
            self._row(self.mine.id, at=iso(timedelta(days=1))),
        ]})
        for result in CheckResult.objects.all():
            self.assertLess(abs((timezone.now() - result.time).total_seconds()), 60)

    def test_a_recent_time_is_kept(self):
        at = timezone.now() - timedelta(minutes=10)
        checkin(self.client, self.poller, {"hostname": "p1", "check_results": [
            self._row(self.mine.id, at=at.isoformat())]})
        self.assertEqual(CheckResult.objects.get().time.replace(microsecond=0),
                         at.replace(microsecond=0))

    def test_more_than_500_results_are_truncated_to_500(self):
        rows = [self._row(self.mine.id) for _ in range(600)]
        checkin(self.client, self.poller, {"hostname": "p1", "check_results": rows})
        self.assertEqual(CheckResult.objects.filter(device_check=self.mine).count(), 500)


class _AdminMixin:
    def setUp(self):
        self.admin = get_user_model().objects.create_user(username="admin")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.viewer = get_user_model().objects.create_user(username="viewer")
        UserProfile.objects.create(user=self.viewer, role=Role.VIEWER)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.device = make_device()
        self.poller = make_host("p1", "tok-p1-" + "j" * 32, poller=True)
        self.base = f"/api/v1/hosts/devices/{self.device.id}/checks/"


class DeviceListCheckStateTests(_AdminMixin, TestCase):
    """The dot on a device row: the list answers with each device's check_state,
    in a number of queries that does not grow with the inventory."""

    URL = "/api/v1/hosts/devices/"

    def _states(self):
        resp = self.client.get(self.URL)
        return {row["name"]: row["check_state"] for row in resp.data}, resp

    def _result(self, check, ok, minutes_ago=0, latency=12.34):
        CheckResult.objects.create(device_check=check, time=timezone.now() - timedelta(minutes=minutes_ago),
                                   ok=ok, latency_ms=latency if ok else None,
                                   detail="ok" if ok else "refused")

    def test_up_down_and_none_come_back_correctly(self):
        healthy = make_device("healthy")
        ailing = make_device("ailing")
        make_device("untouched")
        self._result(make_check(healthy, params={"host": "10.0.0.1"}), True)
        ok_too = make_check(healthy, kind=DeviceCheck.Kind.DNS, params={"name": "a.lan"})
        self._result(ok_too, True, minutes_ago=5)
        fine = make_check(ailing, params={"host": "10.0.0.2"})
        broke = make_check(ailing, kind=DeviceCheck.Kind.TCP, params={"host": "10.0.0.2", "port": 443})
        self._result(fine, True, minutes_ago=9)
        self._result(broke, True, minutes_ago=60)
        self._result(broke, False, minutes_ago=1)     # its latest result failed
        states, resp = self._states()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(states["healthy"], "up")
        self.assertEqual(states["ailing"], "down")
        self.assertIsNone(states["untouched"])

    def test_only_the_latest_result_of_a_check_counts(self):
        device = make_device("recovered")
        check = make_check(device)
        self._result(check, False, minutes_ago=40)
        self._result(check, True, minutes_ago=2)
        states, _ = self._states()
        self.assertEqual(states["recovered"], "up")

    def test_a_disabled_check_that_failed_does_not_hold_a_device_down(self):
        device = make_device("off-check")
        off = make_check(device, params={"host": "10.0.0.9"})
        self._result(off, False)
        on = make_check(device, kind=DeviceCheck.Kind.DNS, params={"name": "b.lan"})
        self._result(on, True)
        DeviceCheck.objects.filter(pk=off.pk).update(enabled=False)
        states, _ = self._states()
        self.assertEqual(states["off-check"], "up")

    def test_enabled_checks_with_no_results_say_nothing(self):
        device = make_device("unpolled")
        make_check(device)
        states, _ = self._states()
        self.assertIsNone(states["unpolled"])

    def test_the_state_costs_a_bounded_number_of_queries(self):
        """Three devices with results, then two more: the count must not move.

        The naive read is one query per check; a Subquery keeps it to the list
        plus a fixed handful however big the inventory grows.
        """
        for name in ("q-a", "q-b", "q-c"):
            device = make_device(name)
            for kind in (DeviceCheck.Kind.ICMP, DeviceCheck.Kind.HTTP):
                check = make_check(device, kind=kind, params={"host": "10.0.0.7"} if kind == "icmp"
                                   else {"url": "http://x.lan/"})
                self._result(check, True)
        self.client.get(self.URL)          # warm any lazy connection work
        # One per-list set, whatever the inventory holds: the devices, their
        # enabled checks with each one's newest result (a Subquery inside the
        # SELECT, not a round trip per check), which of those succeeded, and two
        # reads of the caller's own site grants.
        with self.assertNumQueries(4):
            states, _ = self._states()
        self.assertEqual(states["q-a"], "up")

        for name in ("q-d", "q-e"):
            device = make_device(name)
            self._result(make_check(device), True)
        with self.assertNumQueries(4):
            states, _ = self._states()
        self.assertEqual(states["q-e"], "up")

    def test_the_list_says_whether_checks_may_be_written(self):
        make_device("sw")
        _, resp = self._states()
        self.assertTrue(resp.data[0]["can_edit_checks"])
        self.client.force_authenticate(self.viewer)
        _, resp = self._states()
        self.assertFalse(resp.data[0]["can_edit_checks"])


class DeviceCheckApiTests(_AdminMixin, TestCase):
    def test_create_list_patch_and_delete(self):
        created = self.client.post(self.base, {"kind": "tcp", "params": {"host": "10.0.0.2", "port": 22},
                                               "interval_seconds": 60}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        check_id = created.data["id"]
        self.assertIsNone(created.data["latest"])

        listed = self.client.get(self.base).data
        self.assertEqual([c["id"] for c in listed], [check_id])
        self.assertIsNone(listed[0]["latest"])

        CheckResult.objects.create(device_check_id=check_id, time=timezone.now(), ok=False,
                                   latency_ms=None, detail="refused")
        latest = self.client.get(self.base).data[0]["latest"]
        self.assertFalse(latest["ok"])
        self.assertEqual(latest["detail"], "refused")

        patched = self.client.patch(f"{self.base}{check_id}/", {"enabled": False}, format="json")
        self.assertEqual(patched.status_code, 200)
        self.assertFalse(patched.data["enabled"])

        deleted = self.client.delete(f"{self.base}{check_id}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(DeviceCheck.objects.filter(pk=check_id).exists())

    def test_results_with_hours_newest_last(self):
        check = make_check(self.device)
        now = timezone.now()
        CheckResult.objects.create(device_check=check, time=now - timedelta(hours=2), ok=True,
                                   detail="older")
        CheckResult.objects.create(device_check=check, time=now - timedelta(minutes=1), ok=True,
                                   detail="newer")
        CheckResult.objects.create(device_check=check, time=now - timedelta(hours=30), ok=True,
                                   detail="too old")
        url = f"{self.base}{check.id}/results/"
        rows = self.client.get(url + "?hours=24").data
        self.assertEqual([r["detail"] for r in rows], ["older", "newer"])
        self.assertEqual(len(self.client.get(url + "?hours=48").data), 3)

    def test_hours_outside_1_to_720_is_400(self):
        check = make_check(self.device)
        url = f"{self.base}{check.id}/results/"
        for bad in ("0", "721", "abc"):
            with self.subTest(hours=bad):
                self.assertEqual(self.client.get(f"{url}?hours={bad}").status_code, 400)

    def test_bad_params_are_400_with_one_sentence(self):
        resp = self.client.post(self.base, {"kind": "tcp", "params": {"host": "10.0.0.2", "port": 0}},
                                format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("port", resp.data["error"])
        resp = self.client.post(self.base, {"kind": "icmp", "params": {"host": "10.0.0.2"},
                                            "interval_seconds": 10}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_a_poller_outside_the_site_is_400(self):
        not_poller = make_host("plain", "tok-plain-" + "k" * 32)
        resp = self.client.post(self.base, {"kind": "icmp", "params": {"host": "10.0.0.2"},
                                            "poller": str(not_poller.id)}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_a_pinned_poller_is_stored(self):
        resp = self.client.post(self.base, {"kind": "icmp", "params": {"host": "10.0.0.2"},
                                            "poller": str(self.poller.id)}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["poller"], str(self.poller.id))

    def test_a_non_admin_gets_403_on_writes(self):
        check = make_check(self.device)
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.post(self.base, {"kind": "icmp", "params": {"host": "10.0.0.2"}},
                                          format="json").status_code, 403)
        self.assertEqual(self.client.patch(f"{self.base}{check.id}/", {"enabled": False},
                                           format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"{self.base}{check.id}/").status_code, 403)
        self.assertEqual(self.client.get(self.base).status_code, 200)


class CheckResultRetentionTests(TestCase):
    """Check results follow the metrics retention, hypertable or not."""

    def test_old_results_are_pruned_with_the_metrics(self):
        from datetime import timedelta
        from unittest import mock

        from django.utils.timezone import now

        from apps.alerts import tasks as alert_tasks
        from apps.hosts.models import CheckResult, DeviceCheck, UnmanagedDevice

        device = UnmanagedDevice.objects.create(name="core-switch")
        check = DeviceCheck.objects.create(device=device, kind="icmp", params={"host": "10.0.0.1"})
        old = CheckResult.objects.create(device_check=check, time=now() - timedelta(days=400),
                                         ok=True, detail="old")
        fresh = CheckResult.objects.create(device_check=check, time=now(), ok=True, detail="new")
        for hypertable in (False, True):
            with self.subTest(hypertable=hypertable), \
                    mock.patch.object(alert_tasks, "_metricpoint_is_hypertable", return_value=hypertable):
                alert_tasks.prune_old_metric_points()
                self.assertFalse(CheckResult.objects.filter(pk=old.pk).exists())
                self.assertTrue(CheckResult.objects.filter(pk=fresh.pk).exists())
