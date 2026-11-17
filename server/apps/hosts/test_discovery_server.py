"""A discovery scan's server half (S9, phase 06b): the signed hand-off to one
poller in its check-in response, the answer that comes back as hosts, and the
timeout that fails a scan nobody answered.

The agent side is apps/agent/discovery.py (06a) and the API/UI come in 06c;
nothing here needs either — a queued row and a check-in are the whole interface.
"""

import base64
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from nacl.exceptions import BadSignatureError
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.crypto import encrypt_secret
from apps.hosts.discovery import MAX_FOUND, signed_scan, store_scan_result
from apps.hosts.models import DiscoveredHost, DiscoveryScan, Host
from vigil import scoping
from vigil.signing import get_signing_key
from vigil.testsupport import require_business_module

COMMUNITY = "s3cret-community"
CIDR = "192.0.2.0/24"


def make_host(hostname, token, poller=False):
    return Host.objects.create(hostname=hostname, agent_token=token, status=Host.Status.ONLINE,
                               mode=Host.Mode.MONITOR, agent_poller=poller)


def make_scan(poller, **extra):
    fields = {"cidr": CIDR, "ports": [22, 80, 443]}
    fields.update(extra)
    return DiscoveryScan.objects.create(poller=poller, **fields)


def checkin(client, host, body=None):
    payload = {"hostname": host.hostname}
    payload.update(body or {})
    return client.post("/api/v1/checkin", payload, content_type="application/json",
                       HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}")


def found(ip, **extra):
    entry = {"ip": ip, "alive_by": "ping", "latency_ms": 1.5}
    entry.update(extra)
    return entry


class SignedScanTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.poller = make_host("p1", "tok-p1-" + "h" * 32, poller=True)
        self.other = make_host("p2", "tok-p2-" + "i" * 32, poller=True)
        self.plain = make_host("plain", "tok-plain-" + "j" * 32)

    def _payload(self, body):
        return {"v": 1, "type": "discovery-scan", "agent": self.poller.token_fingerprint,
                "issued": body["scan_issued"], "scan": body["scan"]}

    def test_a_queued_scan_is_handed_to_its_poller_and_becomes_sent(self):
        scan = make_scan(self.poller)
        body = checkin(self.client, self.poller).json()
        self.assertEqual(body["scan"]["id"], str(scan.id))
        self.assertEqual(body["scan"]["cidr"], CIDR)
        self.assertEqual(body["scan"]["ports"], [22, 80, 443])
        scan.refresh_from_db()
        self.assertEqual(scan.state, DiscoveryScan.State.SENT)
        self.assertIsNotNone(scan.sent_at)

    def test_the_signature_covers_this_agent_and_this_scan(self):
        make_scan(self.poller)
        body = checkin(self.client, self.poller).json()
        get_signing_key().verify_key.verify(
            json.dumps(self._payload(body), sort_keys=True).encode(),
            base64.b64decode(body["scan_sig"]))

    def test_the_signature_does_not_verify_for_another_agent(self):
        make_scan(self.poller)
        body = checkin(self.client, self.poller).json()
        forged = self._payload(body) | {"agent": self.other.token_fingerprint}
        with self.assertRaises(BadSignatureError):
            get_signing_key().verify_key.verify(
                json.dumps(forged, sort_keys=True).encode(),
                base64.b64decode(body["scan_sig"]))

    def test_the_community_rides_only_when_one_was_stored(self):
        plain_scan = make_scan(self.poller)
        body = checkin(self.client, self.poller).json()
        self.assertNotIn("community", body["scan"])
        plain_scan.delete()
        make_scan(self.poller, secret=encrypt_secret(COMMUNITY))
        scan = DiscoveryScan.objects.get()
        # The community is in `secret` and nowhere else on the row.
        self.assertNotIn(COMMUNITY, str([scan.cidr, scan.ports, scan.error, scan.state]))
        body = checkin(self.client, self.poller).json()
        self.assertEqual(body["scan"]["community"], COMMUNITY)

    def test_a_scan_sent_earlier_is_handed_out_again_while_fresh(self):
        scan = make_scan(self.poller, state=DiscoveryScan.State.SENT,
                         sent_at=timezone.now() - timedelta(minutes=29))
        body = checkin(self.client, self.poller).json()
        self.assertEqual(body["scan"]["id"], str(scan.id))
        scan.refresh_from_db()
        self.assertEqual(scan.state, DiscoveryScan.State.SENT)

    def test_a_scan_nobody_answered_for_half_an_hour_fails_and_is_not_sent(self):
        scan = make_scan(self.poller, state=DiscoveryScan.State.SENT,
                         sent_at=timezone.now() - timedelta(minutes=31))
        body = checkin(self.client, self.poller).json()
        self.assertNotIn("scan", body)
        scan.refresh_from_db()
        self.assertEqual(scan.state, DiscoveryScan.State.FAILED)
        self.assertEqual(scan.error, "The poller did not answer.")
        self.assertIsNotNone(scan.finished_at)

    def test_another_pollers_scan_is_never_handed_out_or_touched(self):
        theirs = make_scan(self.other)
        body = checkin(self.client, self.poller).json()
        self.assertNotIn("scan", body)
        theirs.refresh_from_db()
        self.assertEqual(theirs.state, DiscoveryScan.State.QUEUED)

    def test_a_host_that_is_not_a_poller_gets_no_scan_key(self):
        make_scan(self.poller)  # a scan exists; its poller is somebody else
        body = checkin(self.client, self.plain).json()
        self.assertNotIn("scan", body)
        self.assertNotIn("scan_sig", body)

    def test_the_oldest_scan_goes_first_and_the_next_only_when_it_is_done(self):
        first = make_scan(self.poller)
        second = make_scan(self.poller)
        DiscoveryScan.objects.filter(pk=first.pk).update(created_at=timezone.now()
                                                         - timedelta(minutes=5))
        for _ in range(2):
            body = checkin(self.client, self.poller).json()
            self.assertEqual(body["scan"]["id"], str(first.id))
        store_scan_result(self.poller, {"id": str(first.id), "found": []})
        body = checkin(self.client, self.poller).json()
        self.assertEqual(body["scan"]["id"], str(second.id))

    def test_an_undecryptable_community_fails_the_scan_and_sends_nothing(self):
        scan = make_scan(self.poller, secret=b"not a fernet token")
        body = checkin(self.client, self.poller).json()
        self.assertNotIn("scan", body)
        scan.refresh_from_db()
        self.assertEqual(scan.state, DiscoveryScan.State.FAILED)
        self.assertEqual(scan.error, "The SNMP community could not be read; enter it again.")

    def test_nothing_at_all_comes_for_a_poller_with_no_scan(self):
        self.assertEqual(signed_scan(self.poller), {})
        self.assertEqual(signed_scan(self.plain), {})


class StoreScanResultTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.poller = make_host("p1", "tok-p1-" + "h" * 32, poller=True)
        self.other = make_host("p2", "tok-p2-" + "i" * 32, poller=True)
        self.scan = make_scan(self.poller, state=DiscoveryScan.State.SENT,
                              sent_at=timezone.now())

    def _rows(self):
        return list(DiscoveredHost.objects.filter(scan=self.scan).order_by("ip"))

    def test_a_result_stores_the_hosts_and_finishes_the_scan(self):
        stored = store_scan_result(self.poller, {
            "id": str(self.scan.id), "scanned": 254, "truncated": True,
            "found": [found("192.0.2.7", open_ports=[22, 80]),
                      {"ip": "192.0.2.9", "alive_by": "tcp", "latency_ms": 0.25,
                       "name": "gw", "sys_name": "gw-01", "sys_descr": "Linux"}]})
        self.assertTrue(stored)
        self.assertEqual([r.ip for r in self._rows()], ["192.0.2.7", "192.0.2.9"])
        first = self._rows()[0]
        self.assertEqual(first.alive_by, "ping")
        self.assertEqual(first.open_ports, [22, 80])
        self.assertEqual(first.name, "")
        second = self._rows()[1]
        self.assertEqual((second.alive_by, second.latency_ms, second.sys_name),
                         ("tcp", 0.25, "gw-01"))
        self.assertEqual(second.sys_descr, "Linux")
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.state, DiscoveryScan.State.DONE)
        self.assertEqual(self.scan.scanned, 254)
        self.assertTrue(self.scan.truncated)
        self.assertIsNotNone(self.scan.finished_at)

    def test_junk_is_dropped_or_cleaned_and_the_ip_stays_in_range(self):
        long_name = "n" * 300
        store_scan_result(self.poller, {
            "id": str(self.scan.id),
            "found": [found("192.0.2.1"), found("192.0.2.1"), found("198.51.100.4"),
                      found("not-an-ip"), found("300.1.1.1"), "just a string",
                      {"ip": "192.0.2.20"},
                      found("192.0.2.30", alive_by="udp", latency_ms=-1, name=long_name,
                             open_ports=[0, 70000, "22", True, 80, 443, 22, 53, 993, 636,
                                         8080, 8443, 9090, 11211, 27017, 5672, 15672, 9200])],
        })
        rows = {r.ip: r for r in self._rows()}
        self.assertEqual(sorted(rows), ["192.0.2.1", "192.0.2.20", "192.0.2.30"])
        self.assertIsNone(rows["192.0.2.20"].latency_ms)
        cleaned = rows["192.0.2.30"]
        self.assertEqual(cleaned.alive_by, "tcp")
        self.assertIsNone(cleaned.latency_ms)
        self.assertEqual(cleaned.name, "n" * 100)
        # Bad ports dropped, repeats collapsed, sorted, cut to the cap of 16.
        self.assertEqual(cleaned.open_ports, [22, 53, 80, 443, 636, 993, 5672, 8080, 8443,
                                              9090, 9200, 11211, 15672, 27017])
        # Fifteen ports is below the cap of 16; sixteen stay cut to sixteen.
        store_scan_result(self.poller, {
            "id": str(self.scan.id), "found": [found("192.0.2.40")]})  # done: ignored
        capped = make_scan(self.poller, state=DiscoveryScan.State.SENT, sent_at=timezone.now())
        store_scan_result(self.poller, {
            "id": str(capped.id),
            "found": [found("192.0.2.41", open_ports=list(range(1, 21)))]})
        self.assertEqual(capped.hosts.get().open_ports, list(range(1, 17)))
        self.assertEqual(self.scan.hosts.count(), 3)

    def test_at_most_a_thousand_and_twenty_four_hosts_are_stored(self):
        # A /24 cannot yield more than its addresses, so the cap is shown on a
        # range that could: only the first MAX_FOUND entries may become rows.
        wide = make_scan(self.poller, cidr="10.0.0.0/16", state=DiscoveryScan.State.SENT,
                         sent_at=timezone.now())
        # The cap counts entries, junk included: 45 unusable entries ahead of
        # the good ones leave exactly MAX_FOUND - 45 rows.
        store_scan_result(self.poller, {
            "id": str(wide.id), "found": [{"ip": None}] * 40 + ["a string"] * 5
            + [{"ip": f"10.0.{i // 256}.{i % 256}"} for i in range(MAX_FOUND)]})
        self.assertEqual(wide.hosts.count(), MAX_FOUND - 45)

    def test_a_missing_or_bad_scanned_and_truncated_are_cleaned(self):
        for bad, expected in ((None, 0), ("many", 0), (True, 0), (-5, 0), (7, 7)):
            scan = make_scan(self.poller, state=DiscoveryScan.State.SENT,
                             sent_at=timezone.now())
            store_scan_result(self.poller, {"id": str(scan.id), "scanned": bad,
                                            "truncated": "yes", "found": []})
            scan.refresh_from_db()
            self.assertEqual((scan.scanned, scan.truncated), (expected, False))

    def test_a_result_for_another_pollers_or_unknown_scan_changes_nothing(self):
        theirs = make_scan(self.other, state=DiscoveryScan.State.SENT, sent_at=timezone.now())
        for payload in ({"id": str(theirs.id), "found": [found("192.0.2.5")]},
                        {"id": "1b4e28ba-2fa1-11d2-883f-00a0c983d478",
                         "found": [found("192.0.2.5")]},
                        {"found": [found("192.0.2.5")]},
                        {"id": 42, "found": []},
                        ["a list is not a result"],
                        None):
            with self.subTest(payload=payload):
                self.assertFalse(store_scan_result(self.poller, payload))
        theirs.refresh_from_db()
        self.assertEqual(theirs.state, DiscoveryScan.State.SENT)
        self.assertEqual(DiscoveredHost.objects.count(), 0)

    def test_a_finished_scan_keeps_its_first_answer(self):
        store_scan_result(self.poller, {"id": str(self.scan.id), "found": [found("192.0.2.5")]})
        self.assertFalse(store_scan_result(self.poller,
                                           {"id": str(self.scan.id), "found": []}))
        self.assertEqual(self.scan.hosts.count(), 1)
        self.assertFalse(store_scan_result(self.poller,
                                           {"id": str(self.scan.id), "error": "too late"}))
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.state, DiscoveryScan.State.DONE)
        self.assertEqual(self.scan.error, "")

    def test_an_error_result_fails_the_scan_with_the_text_cut_to_200(self):
        long = "e" * 400
        self.assertTrue(store_scan_result(self.poller, {"id": str(self.scan.id), "error": long}))
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.state, DiscoveryScan.State.FAILED)
        self.assertEqual(self.scan.error, "e" * 200)
        self.assertIsNotNone(self.scan.finished_at)
        self.assertEqual(self.scan.hosts.count(), 0)

    def test_the_answer_rides_a_check_in_and_nothing_else_is_stored_for_a_plain_agent(self):
        body = checkin(self.client, self.poller,
                       {"scan_result": {"id": str(self.scan.id), "scanned": 3,
                                        "found": [found("192.0.2.11")]}}).json()
        self.assertNotIn("scan", body)  # the scan is done; nothing left to hand out
        self.assertEqual([r.ip for r in self._rows()], ["192.0.2.11"])


class DiscoverySiteScopeTests(TestCase):
    """A scan belongs to its site like a device does, through ``site_id``."""

    def setUp(self):
        # The Business sites app is what makes two sites at all; Free has none.
        require_business_module()
        from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole

        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        self.dana = get_user_model().objects.create_user("dana")
        UserProfile.objects.create(user=self.dana, role=Role.ADMIN)
        UserSiteRole.objects.create(user=self.dana, site=west, role=Role.ADMIN)
        self.mine = make_host("p-west", "tok-pw-" + "k" * 32, poller=True)
        self.theirs = make_host("p-lab", "tok-pl-" + "l" * 32, poller=True)
        HostSiteAssignment.objects.create(host=self.mine, site=west)
        HostSiteAssignment.objects.create(host=self.theirs, site=lab)
        with scoping.system(reason="test"):
            self.mine_scan = make_scan(self.mine, site_id=west.pk)
            theirs = make_scan(self.theirs, site_id=lab.pk)
            DiscoveredHost.objects.create(scan=theirs, ip="192.0.2.5", alive_by="ping")

    def test_a_scan_of_another_site_is_invisible(self):
        with scoping.as_user(self.dana):
            self.assertEqual([s.pk for s in DiscoveryScan.objects.all()], [self.mine_scan.pk])
            self.assertEqual(DiscoveredHost.objects.count(), 0)

    def test_handing_out_a_scan_is_instance_wide_work(self):
        # The other poller is authenticated by its own token: its site's scan
        # rides its own check-in, and stays that site's row.
        body = signed_scan(self.theirs)
        self.assertIn("scan", body)
        self.assertTrue(store_scan_result(
            self.theirs, {"id": str(body["scan"]["id"]), "found": [found("192.0.2.6")]}))
        theirs = DiscoveryScan.objects.unscoped(reason="test: both sites' scans").get(
            poller=self.theirs)
        self.assertEqual(theirs.state, DiscoveryScan.State.DONE)
        self.assertEqual(sorted(h.ip for h in theirs.hosts.all()), ["192.0.2.5", "192.0.2.6"])

    def test_the_check_in_of_another_sites_poller_still_carries_its_own_scan(self):
        body = checkin(self.client, self.theirs).json()
        self.assertEqual(body["scan"]["id"],
                         str(DiscoveryScan.objects.unscoped(reason="test: the lab scan").get(
                             poller=self.theirs).id))
