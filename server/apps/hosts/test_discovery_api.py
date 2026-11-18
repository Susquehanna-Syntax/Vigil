"""The discovery API (S9, phase 06d): who may queue a scan, what an answer says,
and which found hosts become devices.

The scan's own half — the signed hand-off, the poller's answer, the timeout — is
apps.hosts.test_discovery_server; nothing here repeats it, so a finished scan is
built by writing the rows the poller's answer would have written. The community
is asserted absent from every answer by bytes: the one secret a scan carries must
not come back out through any of the three reads.

What Community answers is what these routes answer here; sites themselves, and who
sees whose scan across them, are vigil.test_site_isolation's business.
"""

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.crypto import decrypt_secret, encrypt_secret
from apps.hosts.discovery import DEFAULT_PORTS, validate_cidr, validate_ports
from apps.hosts.models import DiscoveredHost, DiscoveryScan, Host, UnmanagedDevice
from vigil.testsupport import require_business_module

#: A private /24 with room for the found hosts. 198.51.100.0/24 is documentation
#: space, so no test can reach a machine that exists.
CIDR = "198.51.100.0/24"

COMMUNITY = "s3cret-community"

SCANS = "/api/v1/hosts/discovery/scans/"
POLLERS = "/api/v1/hosts/discovery/pollers/"


def make_host(hostname, poller=True, **extra):
    fields = {"status": Host.Status.ONLINE, "mode": Host.Mode.MONITOR}
    fields.update(extra)
    return Host.objects.create(hostname=hostname,
                               agent_token="tok-" + hostname + "-" + "h" * 24,
                               agent_poller=poller, **fields)


def make_user(username, role):
    user = get_user_model().objects.create_user(username=username)
    UserProfile.objects.create(user=user, role=role)
    return user


def client_for(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def make_scan(poller, created_before=None, **extra):
    """A scan. ``created_before`` seconds ago: the list's order is by creation, so
    a test that cares about it has to say when."""
    fields = {"cidr": CIDR, "ports": [22, 80, 443]}
    fields.update(extra)
    if created_before is None:
        return DiscoveryScan.objects.create(poller=poller, **fields)
    scan = DiscoveryScan.objects.create(poller=poller, **fields)
    DiscoveryScan.objects.filter(pk=scan.pk).update(
        created_at=timezone.now() - timedelta(seconds=created_before))
    scan.refresh_from_db()
    return scan


def found_host(scan, ip, **extra):
    fields = {"alive_by": "ping", "latency_ms": 1.5, "open_ports": [22]}
    fields.update(extra)
    return DiscoveredHost.objects.create(scan=scan, ip=ip, **fields)


class CidrValidationTests(TestCase):
    """The server refuses first, so a bad request never queues work on a poller."""

    def test_a_private_network_is_normalised(self):
        self.assertEqual(validate_cidr("198.51.100.37/24"), "198.51.100.0/24")
        self.assertEqual(validate_cidr(" 10.20.30.40/22 "), "10.20.28.0/22")

    def test_each_refused_network_says_which_rule_it_broke(self):
        refused = [
            ("8.8.8.0/24", "only private networks can be scanned"),
            ("127.0.0.0/24", "only private networks can be scanned"),
            ("224.0.0.0/24", "only private networks can be scanned"),
            # 100.64/10 is a carrier's own address space: someone else's network,
            # which is the one thing a scan may never be pointed at. (Python calls
            # link-local and 240/4 private, so those pass; a scan of them simply
            # finds nobody.)
            ("100.64.0.0/10", "only private networks can be scanned"),
            ("2001:db8::/64", "only IPv4 networks can be scanned"),
            ("::1/128", "only IPv4 networks can be scanned"),
            # A /21 is 2048 addresses, one over the largest scan there is.
            ("10.0.0.0/21", "at most 1024 addresses (a /22) per scan"),
            ("garbage", "cidr must be a network such as 10.0.0.0/24"),
            ("10.0.0.0/33", "cidr must be a network such as 10.0.0.0/24"),
            ("10.0.0.300/24", "cidr must be a network such as 10.0.0.0/24"),
            (10, "cidr must be a network such as 10.0.0.0/24"),
            (None, "cidr must be a network such as 10.0.0.0/24"),
            (["10.0.0.0/24"], "cidr must be a network such as 10.0.0.0/24"),
        ]
        for raw, sentence in refused:
            with self.subTest(cidr=repr(raw)), self.assertRaises(ValueError) as caught:
                validate_cidr(raw)
            self.assertEqual(str(caught.exception), sentence)

    def test_a_private_boundary_is_still_scannable(self):
        # 172.16/12 and 192.168/16 are private, and the /22 is exactly 1024
        # addresses: both ends of the two rules that let a network through.
        for raw in ("172.20.5.5/23", "192.168.1.1/24", "10.0.0.0/22"):
            with self.subTest(cidr=raw):
                self.assertTrue(validate_cidr(raw))


class PortValidationTests(TestCase):
    def test_no_ports_means_the_default_set(self):
        self.assertEqual(validate_ports(None), DEFAULT_PORTS)
        for missing in ({}, {"cidr": CIDR}):
            with self.subTest(body=missing):
                self.assertEqual(validate_ports(missing.get("ports")), DEFAULT_PORTS)

    def test_ports_are_kept_in_order_and_without_repeats(self):
        self.assertEqual(validate_ports([443, 22, 443, 8080]), [443, 22, 8080])

    def test_sixteen_is_the_most_and_one_the_least(self):
        self.assertEqual(validate_ports(list(range(1, 17))), list(range(1, 17)))
        self.assertEqual(validate_ports([65535]), [65535])
        self.assertEqual(validate_ports([1]), [1])

    def test_a_refused_port_list_says_what_a_port_list_is(self):
        sentence = "ports must be a list of 1 to 16 port numbers"
        for bad in ([], list(range(1, 18)), [0], [65536], [True], ["22"], [22.0], 22,
                    {"ports": [22]}, "22"):
            with self.subTest(ports=bad), self.assertRaises(ValueError) as caught:
                validate_ports(bad)
            self.assertEqual(str(caught.exception), sentence)


class ScanCreateTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.client = client_for(self.admin)
        self.poller = make_host("p1")
        self.plain = make_host("web-1", poller=False)

    def test_an_admin_queues_a_scan_and_it_waits_for_its_poller(self):
        resp = self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR,
                                       "ports": [22, 80, 443]}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        scan = DiscoveryScan.objects.get()
        self.assertEqual(scan.state, DiscoveryScan.State.QUEUED)
        self.assertEqual(scan.poller, self.poller)
        self.assertEqual(scan.requested_by, self.admin)
        self.assertIsNone(scan.sent_at)
        self.assertEqual(resp.json()["id"], str(scan.id))
        self.assertEqual(resp.json()["poller"], {"id": str(self.poller.id),
                                                 "hostname": "p1"})

    def test_the_default_ports_are_stored_when_the_body_names_none(self):
        self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR},
                         content_type="application/json")
        self.assertEqual(DiscoveryScan.objects.get().ports, DEFAULT_PORTS)

    def test_a_community_is_stored_encrypted_and_only_its_presence_is_answered(self):
        resp = self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR,
                                        "community": COMMUNITY},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        scan = DiscoveryScan.objects.get()
        self.assertTrue(scan.secret)
        self.assertEqual(decrypt_secret(scan.secret), COMMUNITY)
        self.assertIn('"snmp":true', json_text(resp))

    def test_a_refused_community_says_what_a_community_is(self):
        for bad in ("x" * 65, "tab\there", 7, ["public"], True):
            with self.subTest(community=bad):
                resp = self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR,
                                                "community": bad},
                                        content_type="application/json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json()["error"],
                                 "community must be text of at most 64 characters")
        self.assertEqual(DiscoveryScan.objects.count(), 0)

    def test_an_empty_community_is_no_community(self):
        for empty in ("", None):
            with self.subTest(community=empty):
                resp = self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR,
                                                "community": empty},
                                        content_type="application/json")
                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertIn('"snmp":false', json_text(resp))
                # encrypt_secret("") is b"", which is the field's default: an
                # empty community stores no secret rather than one that says nothing.
                scan = _stored_scans().get(pk=resp.json()["id"])
                self.assertFalse(scan.secret)
                self.assertEqual(decrypt_secret(bytes(scan.secret)), "")

    def test_a_bad_cidr_or_port_list_is_refused_with_its_sentence(self):
        for field, body, sentence in (
            ("cidr", {"cidr": "8.8.8.0/24"}, "only private networks can be scanned"),
            ("ports", {"cidr": CIDR, "ports": [0]},
             "ports must be a list of 1 to 16 port numbers"),
        ):
            with self.subTest(field=field):
                resp = self.client.post(SCANS, dict({"poller": str(self.poller.id)}, **body),
                                        content_type="application/json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json()["error"], sentence)
        self.assertEqual(DiscoveryScan.objects.count(), 0)

    def test_a_host_that_cannot_poll_is_refused_as_if_it_were_no_host(self):
        for raw in (str(self.plain.id), str(uuid4()), None, "not-a-uuid", 7):
            with self.subTest(poller=raw):
                resp = self.client.post(SCANS, {"poller": raw, "cidr": CIDR},
                                        content_type="application/json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json()["error"], "poller must be a poller host")
        self.assertEqual(DiscoveryScan.objects.count(), 0)

    def test_a_fourth_waiting_scan_for_one_poller_is_refused(self):
        for _ in range(3):
            self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR},
                             content_type="application/json")
        resp = self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["error"], "This poller already has 3 scans waiting.")
        self.assertEqual(DiscoveryScan.objects.count(), 3)

    def test_a_finished_or_failed_scan_frees_its_poller_slot(self):
        for state in (DiscoveryScan.State.DONE, DiscoveryScan.State.FAILED):
            make_scan(self.poller, state=state)
        resp = self.client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_a_body_that_is_not_an_object_is_refused(self):
        resp = self.client.post(SCANS, ["not", "an", "object"], content_type="application/json")
        self.assertEqual(resp.status_code, 400, resp.content)


class ScanReadTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.poller = make_host("p1")
        self.scan = make_scan(self.poller, state=DiscoveryScan.State.DONE,
                              secret=encrypt_secret(COMMUNITY), requested_by=self.admin,
                              scanned=250, truncated=True, sent_at=timezone.now(),
                              finished_at=timezone.now())
        found_host(self.scan, "198.51.100.7", name="nas-01")
        found_host(self.scan, "198.51.100.9", alive_by="tcp", sys_name="gw-01")

    def test_the_list_answers_the_newest_first_with_what_a_row_needs(self):
        older = make_scan(self.poller, cidr="192.0.2.0/30", created_before=60)
        body = client_for(self.admin).get(SCANS).json()
        self.assertEqual([row["id"] for row in body], [str(self.scan.id), str(older.id)])
        newest = body[0]
        self.assertEqual(newest["cidr"], CIDR)
        self.assertEqual(newest["ports"], [22, 80, 443])
        self.assertEqual(newest["state"], "done")
        self.assertEqual((newest["scanned"], newest["truncated"], newest["found"]),
                         (250, True, 2))
        self.assertTrue(newest["snmp"])
        self.assertEqual(newest["requested_by"], self.admin.username)
        self.assertIsNotNone(newest["finished_at"])

    def test_the_list_is_fifty_deep(self):
        for _ in range(60):
            make_scan(self.poller, created_before=120)
        self.assertEqual(len(client_for(self.admin).get(SCANS).json()), 50)

    def test_the_detail_carries_each_found_host_once_and_its_device_or_null(self):
        known = UnmanagedDevice.objects.create(name="old-name", ip_address="198.51.100.7")
        body = client_for(self.admin).get(_scan_url(self.scan)).json()
        self.assertEqual([row["ip"] for row in body["hosts"]],
                         ["198.51.100.7", "198.51.100.9"])
        first, second = body["hosts"]
        self.assertEqual(first["known"], {"id": str(known.id), "name": "old-name"})
        self.assertEqual(first["alive_by"], "ping")
        self.assertEqual(first["latency_ms"], 1.5)
        self.assertEqual(first["open_ports"], [22])
        self.assertEqual(first["sys_name"], "")
        self.assertEqual(second["known"], None)
        self.assertEqual(second["sys_name"], "gw-01")
        for key in ("id", "poller", "cidr", "state", "found", "snmp"):
            self.assertIn(key, body)

    def test_no_answer_of_a_scan_carries_the_community(self):
        for call in (lambda c: c.get(SCANS), lambda c: c.get(_scan_url(self.scan))):
            with self.subTest(url=call.__name__):
                resp = call(client_for(self.admin))
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn(COMMUNITY.encode(), resp.content)
                self.assertNotIn(b"community", resp.content)
                self.assertNotIn(b'"secret"', resp.content)

    def test_an_unknown_or_malformed_scan_id_is_404(self):
        for raw in (str(uuid4()), "not-a-uuid"):
            with self.subTest(scan_id=raw):
                client = client_for(self.admin)
                self.assertEqual(client.get(f"{SCANS}{raw}/").status_code, 404)
                self.assertEqual(client.post(f"{SCANS}{raw}/adopt/", {},
                                             content_type="application/json").status_code, 404)


class PollerPickTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.poller = make_host("p1")
        self.quiet = make_host("p2", status=Host.Status.OFFLINE)
        self.plain = make_host("web-1", poller=False)

    def test_anonymous_reads_no_picker(self):
        self.assertIn(client_for().get(POLLERS).status_code, (401, 403))

    def test_the_picker_lists_the_pollers_with_their_site_and_state(self):
        body = client_for(self.admin).get(POLLERS).json()
        self.assertEqual([row["hostname"] for row in body], ["p1", "p2"])
        self.assertEqual(body[0]["id"], str(self.poller.id))
        self.assertIsNone(body[0]["site_id"])
        self.assertTrue(body[0]["online"])
        self.assertFalse(body[1]["online"])

    def test_a_host_that_cannot_poll_is_not_offered(self):
        body = client_for(self.admin).get(POLLERS).json()
        self.assertNotIn("web-1", [row["hostname"] for row in body])

    def test_only_an_administrator_may_scan_from_a_poller(self):
        # The dialog shows its scan form from this one field, so it answers the
        # same question POST of a scan does — an administrator who may write the
        # poller's site — rather than merely "is authenticated".
        viewer = make_user("viewer-" + uuid4().hex[:8], Role.VIEWER)
        for user, expected in ((self.admin, True), (viewer, False)):
            with self.subTest(user=user.username):
                body = client_for(user).get(POLLERS).json()
                self.assertEqual([row["can_scan"] for row in body],
                                 [expected, expected])


class RoleTests(TestCase):
    """Reads open to every role that sees devices; writes admin-only."""

    def setUp(self):
        self.poller = make_host("p1")
        self.done = make_scan(self.poller, state=DiscoveryScan.State.DONE)
        found_host(self.done, "198.51.100.7")

    def _writes(self, client):
        return {
            "POST scans": lambda: client.post(SCANS, {"poller": str(self.poller.id),
                                                      "cidr": CIDR},
                                              content_type="application/json"),
            "DELETE scan": lambda: client.delete(_scan_url(self.done)),
            "POST adopt": lambda: client.post(
                _scan_url(self.done) + "adopt/", {"hosts": [self.done.hosts.get().id]},
                content_type="application/json"),
        }

    def test_every_write_is_an_administrators_alone(self):
        for role in (Role.OPERATOR, Role.VIEWER):
            client = client_for(make_user(f"{role}-" + uuid4().hex[:8], role))
            for name, call in self._writes(client).items():
                with self.subTest(role=role, write=name):
                    resp = call()
                    self.assertEqual(resp.status_code, 403, resp.content)
                    self.assertEqual(resp.json()["error"], "Administrator access required.")

    def test_anonymous_gets_no_further_than_the_permission_class(self):
        client = client_for()
        for call in (lambda: client.get(SCANS), lambda: client.get(POLLERS),
                     lambda: client.post(SCANS, {}, content_type="application/json"),
                     lambda: client.delete(_scan_url(self.done))):
            with self.subTest(call=call):
                self.assertIn(call().status_code, (401, 403))

    def test_an_administrator_reads_a_scan_and_their_own_rows(self):
        admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        client = client_for(admin)
        self.assertEqual(client.get(_scan_url(self.done)).status_code, 200)
        self.assertEqual(len(client.get(_scan_url(self.done)).json()["hosts"]), 1)
        self.assertEqual([row["id"] for row in client.get(SCANS).json()],
                         [str(self.done.id)])
        self.assertEqual([row["hostname"] for row in client.get(POLLERS).json()], ["p1"])


class AdoptTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.client = client_for(self.admin)
        self.poller = make_host("p1")
        self.scan = make_scan(self.poller, state=DiscoveryScan.State.DONE)
        self.by_name = found_host(self.scan, "198.51.100.7", name="named-01")
        self.by_sys = found_host(self.scan, "198.51.100.9", sys_name="snmp-named",
                                 sys_descr="Acme NAS 5.2 build 1180")
        self.by_ip = found_host(self.scan, "198.51.100.11")

    def _adopt(self, hosts, **extra):
        body = {"hosts": hosts}
        body.update(extra)
        return self.client.post(_scan_url(self.scan) + "adopt/", body,
                                content_type="application/json")

    def test_adopting_creates_one_device_per_host_named_after_what_it_answered(self):
        resp = self._adopt([self.by_name.id, self.by_sys.id, self.by_ip.id])
        self.assertEqual(resp.status_code, 200, resp.content)
        devices = {d.ip_address: d for d in UnmanagedDevice.objects.all()}
        self.assertEqual(set(devices), {"198.51.100.7", "198.51.100.9", "198.51.100.11"})
        self.assertEqual(devices["198.51.100.7"].name, "named-01")
        self.assertEqual(devices["198.51.100.9"].name, "snmp-named")
        self.assertEqual(devices["198.51.100.11"].name, "198.51.100.11")
        self.assertEqual(resp.json()["skipped"], 0)
        self.assertEqual({row["ip_address"] for row in resp.json()["created"]}, set(devices))

    def test_snmp_wins_the_name_over_the_answer_and_the_device_keeps_its_type(self):
        found_host(self.scan, "198.51.100.13", name="arp-name", sys_name="snmp-name")
        self._adopt([self.by_name.id], device_type="switch")
        device = UnmanagedDevice.objects.get()
        self.assertEqual(device.name, "named-01")
        self.assertEqual(device.device_type, "switch")

    def test_a_device_is_left_alone_when_one_already_covers_that_address(self):
        known = UnmanagedDevice.objects.create(name="already-here", ip_address="198.51.100.7")
        resp = self._adopt([self.by_name.id, self.by_sys.id])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["skipped"], 1)
        self.assertEqual([row["ip_address"] for row in resp.json()["created"]],
                         ["198.51.100.9"])
        known.refresh_from_db()
        self.assertEqual(known.name, "already-here")
        self.assertEqual(UnmanagedDevice.objects.count(), 2)

    def test_the_note_says_which_scan_found_it_and_keeps_the_description(self):
        self._adopt([self.by_sys.id])
        device = UnmanagedDevice.objects.get()
        today = timezone.localtime(self.scan.created_at).strftime("%Y-%m-%d")
        self.assertEqual(device.notes,
                         f"Found by a discovery scan of {CIDR} on {today}."
                         "\nAcme NAS 5.2 build 1180")

    def test_the_note_is_the_scan_line_alone_without_a_description(self):
        self._adopt([self.by_ip.id])
        device = UnmanagedDevice.objects.get()
        self.assertNotIn("\n", device.notes)
        self.assertTrue(device.notes.startswith(f"Found by a discovery scan of {CIDR} on "))

    def test_a_long_name_is_cut_and_the_address_is_kept_when_nothing_has_a_name(self):
        blank = found_host(self.scan, "198.51.100.15", name="", sys_name="")
        self._adopt([blank.id])
        self.assertEqual(UnmanagedDevice.objects.get().name, "198.51.100.15")

    def test_a_scan_that_has_not_finished_cannot_be_adopted_from(self):
        queued = make_scan(self.poller)
        found_host(queued, "198.51.100.20")
        resp = self.client.post(_scan_url(queued) + "adopt/", {"hosts": [queued.hosts.get().id]},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["error"], "Only a finished scan can be adopted from.")

    def test_hosts_that_name_nothing_of_this_scan_are_refused(self):
        other = make_scan(self.poller, state=DiscoveryScan.State.DONE)
        foreign = found_host(other, "198.51.100.30")
        refused = {
            "empty": [],
            "only foreign hosts": [foreign.id],
            "not a list": self.by_name.id,
            "ids as text": ["7"],
            "a bool in the list": [True],
            "too many ids": list(range(257)),
        }
        for label, hosts in refused.items():
            with self.subTest(hosts=label):
                resp = self._adopt(hosts)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json()["error"], "hosts must name hosts of this scan")
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

    def test_hosts_of_another_scan_in_the_same_body_are_ignored_not_refused(self):
        other = make_scan(self.poller, state=DiscoveryScan.State.DONE)
        foreign = found_host(other, "198.51.100.30")
        resp = self._adopt([self.by_name.id, foreign.id])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(UnmanagedDevice.objects.count(), 1)
        self.assertEqual(UnmanagedDevice.objects.get().ip_address, "198.51.100.7")

    def test_a_device_type_that_is_not_one_of_their_own_is_refused(self):
        resp = self._adopt([self.by_name.id], device_type="toaster")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

    def test_adopting_twice_creates_nothing_the_second_time(self):
        self._adopt([self.by_name.id])
        resp = self._adopt([self.by_name.id])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"created": [], "skipped": 1})
        self.assertEqual(UnmanagedDevice.objects.count(), 1)


class ScanDeleteTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.client = client_for(self.admin)
        self.poller = make_host("p1")
        self.scan = make_scan(self.poller)
        found_host(self.scan, "198.51.100.7")

    def test_deleting_a_scan_takes_the_hosts_it_found_with_it(self):
        resp = self.client.delete(_scan_url(self.scan))
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertEqual(DiscoveryScan.objects.count(), 0)
        self.assertEqual(DiscoveredHost.objects.count(), 0)

    def test_the_pollers_and_the_devices_survive(self):
        device = UnmanagedDevice.objects.create(name="nas", ip_address="198.51.100.7")
        other = make_scan(self.poller)
        self.client.delete(_scan_url(self.scan))
        self.assertTrue(Host.objects.filter(pk=self.poller.pk).exists())
        self.assertTrue(UnmanagedDevice.objects.filter(pk=device.pk).exists())
        self.assertTrue(DiscoveryScan.objects.filter(pk=other.pk).exists())


class OneQueryForKnownDevicesTests(TestCase):
    """``known`` is worked out for the whole scan at once, not host by host."""

    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.poller = make_host("p1")
        self.scan = make_scan(self.poller, state=DiscoveryScan.State.DONE)
        for last in range(1, 21):
            found_host(self.scan, f"198.51.100.{last}")

    def test_the_detail_reads_the_devices_once(self):
        # The detail answers in four reads whoever asks: this session's scope, the
        # scan, its 20 found hosts, and the devices they could already be — one for
        # all of them, not one per host.
        with self.assertNumQueries(4):
            resp = client_for(self.admin).get(_scan_url(self.scan))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["hosts"]), 20)


class SiteIsolationTests(TestCase):
    """Site B neither sees site A's scans nor queues one on A's poller."""

    def setUp(self):
        require_business_module()

        from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole

        west = Site.objects.create(name="West", slug=f"west-{uuid4().hex[:6]}")
        lab = Site.objects.create(name="Lab", slug=f"lab-{uuid4().hex[:6]}")
        self.west_poller = make_host("west-poller")
        self.lab_poller = make_host("lab-poller")
        HostSiteAssignment.objects.create(host=self.west_poller, site=west)
        HostSiteAssignment.objects.create(host=self.lab_poller, site=lab)

        # Dana administers West only; Bob administers the whole install.
        self.dana = make_user("dana-" + uuid4().hex[:8], Role.VIEWER)
        UserSiteRole.objects.create(user=self.dana, site=west, role=Role.ADMIN)
        self.bob = make_user("bob-" + uuid4().hex[:8], Role.ADMIN)
        self.lab_scan = make_scan(self.lab_poller, site_id=lab.pk)
        found_host(self.lab_scan, "198.51.100.7")

    def test_a_scan_of_another_site_reads_as_absent(self):
        resp = client_for(self.dana).get(_scan_url(self.lab_scan))
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_another_sites_scan_is_not_in_the_list(self):
        body = client_for(self.dana).get(SCANS).json()
        self.assertEqual(body, [])
        self.assertIn(str(self.lab_scan.id),
                      json_text(client_for(self.bob).get(SCANS)))

    def test_a_site_admin_cannot_queue_a_scan_on_another_sites_poller(self):
        resp = client_for(self.dana).post(SCANS, {"poller": str(self.lab_poller.id),
                                                  "cidr": CIDR},
                                          content_type="application/json")
        self.assertIn(resp.status_code, (400, 403, 404), resp.content)
        self.assertEqual(DiscoveryScan.objects.count(), 1)

    def test_an_install_admin_can(self):
        resp = client_for(self.bob).post(SCANS, {"poller": str(self.lab_poller.id),
                                                 "cidr": CIDR},
                                         content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        # Bob is an administrator of the whole install: the scan is Lab's.
        created = _stored_scans().latest("id")
        self.assertEqual(created.poller_id, self.lab_poller.pk)
        self.assertEqual(created.site_id, scoping_site_id(self.lab_poller))

    def test_a_site_admin_queues_on_their_own_poller_and_sees_only_that_scan(self):
        resp = client_for(self.dana).post(SCANS, {"poller": str(self.west_poller.id),
                                                  "cidr": CIDR},
                                          content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = client_for(self.dana).get(SCANS).json()
        self.assertEqual([row["id"] for row in body], [resp.json()["id"]])

    def test_another_sites_poller_is_not_offered_and_theirs_is(self):
        body = client_for(self.dana).get(POLLERS).json()
        self.assertEqual([row["hostname"] for row in body], ["west-poller"])
        self.assertEqual(body[0]["site_id"], str(scoping_site_id(self.west_poller)))

    def test_a_device_of_another_site_does_not_mark_a_found_host_known(self):
        UnmanagedDevice.objects.create(name="lab-nas", ip_address="198.51.100.7",
                                       site_id=scoping_site_id(self.lab_poller))
        scan = make_scan(self.west_poller, site_id=scoping_site_id(self.west_poller),
                         state=DiscoveryScan.State.DONE)
        found_host(scan, "198.51.100.7")
        body = client_for(self.dana).get(_scan_url(scan)).json()
        self.assertEqual(body["hosts"][0]["known"], None)

    def test_adopting_from_another_sites_scan_is_refused(self):
        resp = client_for(self.dana).post(
            _scan_url(self.lab_scan) + "adopt/",
            {"hosts": [self.lab_scan.hosts.get().id]}, content_type="application/json")
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(UnmanagedDevice.objects.count(), 0)

    def test_deleting_another_sites_scan_is_refused(self):
        resp = client_for(self.dana).delete(_scan_url(self.lab_scan))
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertTrue(DiscoveryScan.objects.filter(pk=self.lab_scan.pk).exists())


def _stored_scans():
    """Every scan there is, whoever's site may see it: Community has no site
    assignments, so a test cannot reach its own rows through the scope filter."""
    return DiscoveryScan.objects.unscoped(reason="test: read back what the "
                                                "endpoints stored")


def _scan_url(scan: DiscoveryScan) -> str:
    return f"{SCANS}{scan.id}/"


def json_text(resp) -> str:
    return resp.content.decode("utf-8")


def scoping_site_id(obj):
    """Whose site *obj* is, or None where there are none to belong to."""
    from vigil import scoping

    return scoping.site_id_of(obj)


class NoSitesTests(TestCase):
    """On Community a scan belongs to no site, and every role that sees devices
    reaches every poller and every scan: there are no sites to hold them back."""

    def setUp(self):
        self.admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        self.poller = make_host("p1")

    def test_a_scan_without_sites_carries_no_site_and_adopts_globally(self):
        client = client_for(self.admin)
        resp = client.post(SCANS, {"poller": str(self.poller.id), "cidr": CIDR},
                           content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        scan = _stored_scans().get()
        self.assertIsNone(scan.site_id)
        client.post(_scan_url(scan) + "adopt/", {"hosts": [_found(scan).id]},
                    content_type="application/json")
        device = UnmanagedDevice.objects.get()
        self.assertIsNone(device.site_id)
        self.assertEqual(device.ip_address, "198.51.100.7")


def _found(scan: DiscoveryScan):
    host = DiscoveredHost.objects.create(scan=scan, ip="198.51.100.7", alive_by="ping")
    scan.state = DiscoveryScan.State.DONE
    scan.save(update_fields=["state"])
    return host


class ListQueryCountTests(TestCase):
    """The list reads its scans, pollers, requesters and found counts together."""

    def test_the_list_costs_the_same_for_one_scan_or_ten(self):
        admin = make_user("admin-" + uuid4().hex[:8], Role.ADMIN)
        poller = make_host("p-list")
        client = client_for(admin)
        found_host(make_scan(poller), "198.51.100.1")
        with CaptureQueriesContext(connection) as one:
            client.get(SCANS)
        for i in range(9):
            found_host(make_scan(poller), f"198.51.100.{i + 2}")
        with CaptureQueriesContext(connection) as ten:
            resp = client.get(SCANS)
        self.assertEqual(len(resp.json()), 10)
        self.assertEqual([s["found"] for s in resp.json()], [1] * 10)
        self.assertEqual(len(ten), len(one))
