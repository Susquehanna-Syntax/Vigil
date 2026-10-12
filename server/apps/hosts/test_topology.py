"""The topology API (14.4): one response with hosts, addresses, ports,
firewalls, stacks, subnets and the links between machines.

QA-32 draws this JSON directly, so the keys, the ordering and the merge rules
are pinned here rather than left to whatever the builder happens to produce.
"""
import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import (
    DockerContainer,
    Host,
    HostFirewall,
    HostNetwork,
    UnmanagedDevice,
)
from apps.hosts.topology import build_topology, topology
from apps_business.sites.models import HostSiteAssignment, Site

ETH0 = {"name": "eth0", "up": True, "mac": "aa:bb:cc:dd:ee:ff",
        "addrs": [{"family": "ipv4", "address": "10.0.0.5", "prefix": 24}]}
DB0 = {"name": "db0", "up": True, "mac": "",
       "addrs": [{"family": "ipv4", "address": "10.0.0.6", "prefix": 24}]}
DOCKER0 = {"name": "docker0", "up": True, "mac": "",
           "addrs": [{"family": "ipv4", "address": "172.17.0.1", "prefix": 16}]}

#: A→B's postgres session as each of its two ends reports it: two agents, two
#: rows, one connection. That is exactly what the link merge has to get right.
A_OUT = {"proto": "tcp", "direction": "out", "remote_ip": "10.0.0.6", "remote_port": 5432,
         "local_port": 41234, "process": "python3", "count": 3}
B_IN = {"proto": "tcp", "direction": "in", "remote_ip": "10.0.0.5", "remote_port": 41234,
        "local_port": 5432, "process": "postgres", "count": 3}


def iface(address, prefix=24, name="eth0"):
    return {"name": name, "up": True, "mac": "",
            "addrs": [{"family": "ipv4", "address": address, "prefix": prefix}]}


def make_host(hostname, ip=None, status=Host.Status.ONLINE, tags=None):
    return Host.objects.create(hostname=hostname, agent_token="tok-" + uuid4().hex,
                               status=status, ip_address=ip, tags=tags or [])


def make_network(host, interfaces=None, listening=None, connections=None, partial=False):
    return HostNetwork.objects.create(
        host=host, interfaces=interfaces if interfaces is not None else [],
        listening=listening or [], connections=connections or [], partial=partial,
        collected_at=timezone.now() - timedelta(minutes=5))


def admin():
    user = get_user_model().objects.filter(username="topo-admin").first()
    if user is None:
        user = get_user_model().objects.create_user("topo-admin", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
    return user


def rows_between(payload, left, right):
    return [link for link in payload["links"] if {link["source"], link["target"]} == {left,
                                                                                      right}]


def host_row(payload, hostname):
    return next(h for h in payload["hosts"] if h["hostname"] == hostname)


class TopologyApiTests(TestCase):
    def setUp(self):
        self.user = admin()
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _get(self):
        response = self.api.get("/api/v1/hosts/topology/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    @staticmethod
    def _row(payload, hostname):
        return next(h for h in payload["hosts"] if h["hostname"] == hostname)

    def test_hosts_carry_ips_ports_firewall_and_stacks(self):
        host = make_host("web-01", ip="10.0.0.5", tags=["web"])
        make_network(host, interfaces=[ETH0, DOCKER0],
                     listening=[{"proto": "tcp", "address": "0.0.0.0", "port": 22,
                                 "process": "sshd"},
                                {"proto": "tcp", "address": "127.0.0.1", "port": 5432,
                                 "process": "postgres"}])
        HostFirewall.objects.create(
            host=host, tool="ufw", enabled=True,
            defaults={"incoming": "deny", "outgoing": "allow"},
            rules=[{"port": 22, "protocol": "tcp", "action": "allow"},
                   {"port": 80, "protocol": "tcp", "action": "allow"}])
        DockerContainer.objects.create(host=host, container_id="c1", name="db", stack="shop",
                                       state="running",
                                       ports=[{"private": 5432, "public": 5432, "type": "tcp"}],
                                       networks=[{"name": "shop_default", "ip": "172.20.0.3"}])
        DockerContainer.objects.create(host=host, container_id="c2", name="solo",
                                       state="running")

        payload = self._get()
        row = self._row(payload, "web-01")
        self.assertEqual(row["primary_ip"], "10.0.0.5")
        self.assertEqual(row["ips"], ["10.0.0.5", "172.17.0.1"])
        self.assertEqual(row["tags"], ["web"])
        self.assertEqual(payload["tags"], [{"name": "web", "count": 1}])
        self.assertEqual(row["site_id"], str(Site.objects.global_site().id))

        by_port = {entry["port"]: entry for entry in row["listening"]}
        self.assertEqual(sorted(by_port), [22, 5432])
        self.assertIs(by_port[22]["public"], True)
        self.assertIs(by_port[5432]["public"], False)
        self.assertEqual(by_port[22]["process"], "sshd")

        self.assertEqual(row["firewall"]["tool"], "ufw")
        self.assertIs(row["firewall"]["enabled"], True)
        self.assertEqual(row["firewall"]["incoming"], "deny")
        self.assertEqual(row["firewall"]["allows"], [22, 80])
        self.assertEqual(row["firewall"]["rules"], 2)

        stacks = row["stacks"]
        self.assertEqual([s["name"] for s in stacks], ["shop", ""])
        shop = stacks[0]["containers"][0]
        self.assertEqual((shop["name"], shop["state"]), ("db", "running"))
        self.assertEqual(shop["ports"], ["5432/tcp"])
        self.assertEqual(shop["published"], [5432])
        self.assertEqual(shop["networks"], ["shop_default"])
        self.assertEqual(stacks[1]["containers"][0]["name"], "solo")
        self.assertIsNotNone(row["network_at"])
        self.assertIs(row["partial"], False)

    def test_links_between_hosts_and_devices_merge_both_ends(self):
        app = make_host("app", ip="10.0.0.5")
        db = make_host("db", ip="10.0.0.6")
        device = UnmanagedDevice.objects.create(name="core-switch", device_type="switch",
                                                ip_address="10.0.0.1")
        make_network(app, interfaces=[ETH0], connections=[A_OUT, {
            "proto": "tcp", "direction": "out", "remote_ip": "10.0.0.1", "remote_port": 161,
            "local_port": 40001, "process": "curl", "count": 1}])
        make_network(db, interfaces=[DB0], connections=[B_IN])

        payload = self._get()
        app_id, db_id, dev_id = str(app.id), str(db.id), str(device.id)
        self.assertEqual(rows_between(payload, app_id, db_id),
                         [{"source": app_id, "target": db_id, "ports": [5432], "count": 3}])
        self.assertEqual([(link["source"], link["target"], link["ports"], link["count"])
                          for link in payload["links"] if link["target"] == dev_id],
                         [(app_id, dev_id, [161], 1)])
        self.assertEqual(payload["devices"],
                         [{"id": dev_id, "name": "core-switch", "type": "switch",
                           "ip": "10.0.0.1"}])

    def test_external_and_subnets(self):
        app = make_host("app", ip="10.0.0.5")
        db = make_host("db", ip="10.0.0.6")
        router = UnmanagedDevice.objects.create(name="edge", device_type="router",
                                                ip_address="10.0.0.1")
        # docker0 is 172.17.0.1/16 and a compose bridge out of the same pool;
        # neither is a segment the map draws, whatever the machine's own LAN
        # looks like — this fleet's LAN happens to be 10.0.0.0/24.
        make_network(app, interfaces=[ETH0, DOCKER0],
                     connections=[{"proto": "tcp", "direction": "out", "remote_ip": "1.2.3.4",
                                   "remote_port": 443, "local_port": 43210,
                                   "process": "curl", "count": 12},
                                  {"proto": "tcp", "direction": "out", "remote_ip": "10.0.0.1",
                                   "remote_port": 443, "local_port": 43211,
                                   "process": "curl", "count": 5}])
        make_network(db, interfaces=[DB0,
                                     {"name": "br-9f2c1a4d", "up": True, "mac": "",
                                      "addrs": [{"family": "ipv4", "address": "172.20.0.1",
                                                 "prefix": 16}]}])

        payload = self._get()
        self.assertEqual(self._row(payload, "app")["external"],
                         [{"ip": "1.2.3.4", "count": 12, "ports": [443]}])
        self.assertEqual(self._row(payload, "db")["external"], [])
        # The gateway is in the fleet registry, so its line lands on that node
        # instead of being counted among the destinations outside the fleet.
        self.assertEqual([(link["source"], link["target"], link["ports"], link["count"])
                          for link in payload["links"] if link["target"] == str(router.id)],
                         [(str(app.id), str(router.id), [443], 5)])

        by_cidr = {s["cidr"]: s["host_ids"] for s in payload["subnets"]}
        self.assertEqual(sorted(by_cidr["10.0.0.0/24"]),
                         sorted([str(app.id), str(db.id)]))
        self.assertNotIn("172.17.0.0/16", by_cidr)
        self.assertNotIn("172.20.0.0/16", by_cidr)

    def test_a_host_without_network_data_still_appears(self):
        make_host("quiet", ip="10.9.9.9")

        row = self._row(self._get(), "quiet")
        self.assertEqual(row["ips"], ["10.9.9.9"])
        self.assertEqual(row["primary_ip"], "10.9.9.9")
        self.assertIsNone(row["network_at"])
        self.assertIsNone(row["firewall"])
        self.assertEqual(row["listening"], [])
        self.assertEqual(row["stacks"], [])
        self.assertEqual(row["external"], [])
        self.assertIs(row["partial"], False)

    def test_pending_and_rejected_hosts_are_left_out(self):
        make_host("live")
        make_host("new-machine", status=Host.Status.PENDING)
        make_host("nope", status=Host.Status.REJECTED)

        self.assertEqual({h["hostname"] for h in self._get()["hosts"]}, {"live"})

    def test_query_count_does_not_grow(self):
        def seed(count):
            Host.objects.all().delete()
            for index in range(count):
                host = make_host(f"h{index}", ip=f"10.0.0.{10 + index}", tags=["t"])
                make_network(host, interfaces=[iface(f"10.0.0.{10 + index}")],
                             listening=[{"proto": "tcp", "address": "0.0.0.0", "port": 22,
                                         "process": "sshd"}],
                             connections=[{"proto": "tcp", "direction": "out",
                                           "remote_ip": "1.2.3.4", "remote_port": 443,
                                           "local_port": 40000 + index, "process": "curl",
                                           "count": 1}])
                DockerContainer.objects.create(host=host, container_id=f"c{index}",
                                               name=f"c{index}", stack="s", state="running")
                HostFirewall.objects.create(host=host, tool="ufw", enabled=True)

        seed(2)
        self._get()
        with CaptureQueriesContext(connection) as first:
            self._get()
        seed(6)
        six = self._get()
        with CaptureQueriesContext(connection) as second:
            again = self._get()

        self.assertEqual(len(six["hosts"]), 6)
        self.assertEqual(len(again["hosts"]), 6)
        self.assertEqual(len(first), len(second),
                         f"query count grew with the host count: {len(first)} → {len(second)}")

    def test_unauthenticated_callers_are_refused(self):
        self.assertEqual(APIClient().get("/api/v1/hosts/topology/").status_code, 403)

    def test_sites_are_the_boxes_the_map_draws(self):
        """Sites come from scoping.sites_for_hosts, which puts an unassigned
        host in Global — so every host has a site to sit in, and a site with no
        host in it is not a box."""
        site = Site.objects.create(name="HQ", slug="hq")
        host = make_host("hq-01", ip="10.0.0.5")
        HostSiteAssignment.objects.create(host=host, site=site)
        make_host("somewhere-else", ip="10.0.0.6")
        Site.objects.create(name="Empty Campus", slug="empty-campus")

        payload = self._get()
        self.assertEqual([s["name"] for s in payload["sites"]], ["Global", "HQ"])
        self.assertEqual(self._row(payload, "hq-01")["site_id"], str(site.id))
        self.assertEqual(self._row(payload, "somewhere-else")["site_id"],
                         str(Site.objects.global_site().id))

    def test_response_is_json_serialisable_end_to_end(self):
        """build_topology hands the view plain JSON types; a UUID or an
        ipaddress object sneaking into the dict would serialise into something
        the browser cannot use."""
        host = make_host("web-01", ip="10.0.0.5", tags=["web"])
        make_network(host, interfaces=[ETH0],
                     listening=[{"proto": "tcp", "address": "0.0.0.0", "port": 22,
                                 "process": "sshd"}],
                     connections=[{"proto": "tcp", "direction": "in", "remote_ip": "1.2.3.4",
                                   "remote_port": 51000, "local_port": 22,
                                   "process": "sshd", "count": 4}])
        UnmanagedDevice.objects.create(name="edge", device_type="router", ip_address="10.0.0.1")

        payload = json.loads(json.dumps(build_topology(admin())))
        self.assertEqual(sorted(payload), ["devices", "hosts", "links", "sites", "subnets",
                                           "tags"])
        row = self._row(payload, "web-01")
        self.assertEqual(row["id"], str(host.id))
        self.assertEqual(row["external"], [{"ip": "1.2.3.4", "count": 4, "ports": [51000]}])


class LinkMergeTests(TestCase):
    """One TCP session is reported by both of its ends. The map needs one
    line, with one count, and the two ends' ports are not the same number."""

    @classmethod
    def setUpTestData(cls):
        cls.user = admin()
        cls.app = make_host("app", ip="10.0.0.5")
        cls.db = make_host("db", ip="10.0.0.6")
        cls.app_id, cls.db_id = str(cls.app.id), str(cls.db.id)

    def _link(self, app_connections, db_connections):
        HostNetwork.objects.all().delete()
        make_network(self.app, interfaces=[ETH0], connections=app_connections)
        make_network(self.db, interfaces=[DB0], connections=db_connections)
        return rows_between(build_topology(self.user), self.app_id, self.db_id)

    def test_one_connection_seen_from_both_ends_counts_once(self):
        self.assertEqual(self._link([A_OUT], [B_IN]),
                         [{"source": self.app_id, "target": self.db_id,
                           "ports": [5432], "count": 3}])

    def test_the_heavier_end_wins_when_the_reports_disagree(self):
        self.assertEqual([link["count"] for link in self._link([A_OUT], [dict(B_IN, count=7)])],
                         [7])

    def test_two_sessions_on_one_port_sum_their_counts(self):
        """Distinct (source, target, port) observations of *different* source
        ports are different sockets; those counts add, which is what keeps a
        real fan-out from being flattened into one connection."""
        first = dict(A_OUT, local_port=41234)
        second = dict(A_OUT, local_port=41235)
        self.assertEqual(self._link([first, second], [dict(B_IN, count=3)]),
                         [{"source": self.app_id, "target": self.db_id,
                           "ports": [5432], "count": 6}])

    def test_a_containers_veth_address_is_not_a_second_machine(self):
        """The host's own bridge address is how you reach a published port, so
        the line goes to the host. The veth inside the bridge is a container's
        address and identifies nothing on the map."""
        veth_out = {"proto": "tcp", "direction": "out", "remote_ip": "172.17.0.3",
                    "remote_port": 5432, "local_port": 42000, "process": "python3",
                    "count": 2}
        HostNetwork.objects.all().delete()
        make_network(self.app, interfaces=[ETH0], connections=[veth_out])
        make_network(self.db,
                     interfaces=[DB0,
                                 {"name": "docker0", "up": True, "mac": "",
                                  "addrs": [{"family": "ipv4", "address": "172.17.0.1",
                                             "prefix": 16}]},
                                 {"name": "veth2a91c3f@if7", "up": True, "mac": "",
                                  "addrs": [{"family": "ipv4", "address": "172.17.0.3",
                                             "prefix": 16}]}],
                     connections=[])

        payload = build_topology(self.user)
        db = next(h for h in payload["hosts"] if h["hostname"] == "db")
        self.assertEqual(db["ips"], ["10.0.0.6", "172.17.0.1", "172.17.0.3"])
        self.assertEqual(rows_between(payload, self.app_id, self.db_id), [])
        self.assertEqual(host_row(payload, "app")["external"],
                         [{"ip": "172.17.0.3", "count": 2, "ports": [5432]}])


class TopologyViewTests(TestCase):
    """The view function is what the URL resolves to."""

    def test_the_view_returns_the_builders_dict(self):
        user = admin()
        request = APIRequestFactory().get("/api/v1/hosts/topology/")
        force_authenticate(request, user=user)
        self.assertEqual(set(topology(request).data),
                         {"sites", "tags", "subnets", "hosts", "devices", "links"})
