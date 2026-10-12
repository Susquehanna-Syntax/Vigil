"""The network snapshot a check-in carries (14.4): stored as written, with
whatever a broken or hostile agent cannot be allowed to put in the database
dropped on the way in."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host, HostNetwork

NETWORK = {
    "interfaces": [
        {"name": "eth0", "up": True, "mac": "aa:bb:cc:dd:ee:ff",
         "addrs": [{"family": "ipv4", "address": "10.0.0.5", "prefix": 24},
                   {"family": "ipv6", "address": "fe80::12", "prefix": 64}]},
        {"name": "veth9", "up": False, "mac": "", "addrs": []},
    ],
    "listening": [{"proto": "tcp", "address": "0.0.0.0", "port": 22, "process": "sshd"},
                  {"proto": "udp", "address": "::", "port": 5353, "process": "avahi"}],
    "connections": [{"proto": "tcp", "direction": "out", "remote_ip": "10.0.0.7",
                     "remote_port": 5432, "local_port": 0, "process": "python3", "count": 3},
                    {"proto": "tcp", "direction": "in", "remote_ip": "10.0.0.9",
                     "remote_port": 0, "local_port": 22, "process": "sshd", "count": 2}],
    "partial": False,
}


class NetworkIngestTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="tok-" + "z" * 32,
                                        status=Host.Status.ONLINE)
        self.agent = APIClient()
        user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(user)

    def _checkin(self, body):
        return self.agent.post("/api/v1/checkin", body, format="json",
                               HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")

    def test_checkin_stores_the_network(self):
        self.assertEqual(self._checkin({"network": NETWORK}).status_code, 200)
        row = HostNetwork.objects.get(host=self.host)
        self.assertEqual([i["name"] for i in row.interfaces], ["eth0", "veth9"])
        self.assertEqual(row.interfaces[0]["addrs"][0],
                         {"family": "ipv4", "address": "10.0.0.5", "prefix": 24})
        self.assertIs(row.interfaces[1]["up"], False)
        self.assertEqual([(l["proto"], l["port"]) for l in row.listening],
                         [("tcp", 22), ("udp", 5353)])
        self.assertEqual([(c["direction"], c["remote_ip"], c["count"])
                          for c in row.connections],
                         [("out", "10.0.0.7", 3), ("in", "10.0.0.9", 2)])
        self.assertFalse(row.partial)
        self.assertIsNotNone(row.collected_at)

    def test_bad_values_are_dropped(self):
        body = {
            "interfaces": ["not-a-dict",
                           {"name": "eth0", "addrs": [
                               {"family": "ipv4", "address": "10.0.0.999", "prefix": 24},
                               {"family": "ipv4", "address": "10.0.0.5", "prefix": 999},
                               "junk"]}],
            "listening": [{"proto": "icmp", "address": "0.0.0.0", "port": 22},
                          {"proto": "tcp", "address": "0.0.0.0", "port": 70000},
                          {"proto": "tcp", "address": "not-an-ip", "port": 80},
                          {"proto": "tcp", "address": "0.0.0.0", "port": 443,
                           "process": "nginx"}],
            "connections": [{"proto": "tcp", "direction": "sideways",
                             "remote_ip": "10.0.0.7", "remote_port": 5432,
                             "local_port": 0, "count": 1},
                            {"proto": "tcp", "direction": "out", "remote_ip": "10.0.0.8",
                             "remote_port": 443, "local_port": 0, "count": 0},
                            {"proto": "tcp", "direction": "out", "remote_ip": "10.0.0.9",
                             "remote_port": 80, "local_port": 0, "count": 10 ** 9}],
            "partial": "yes",
        }
        self.assertEqual(self._checkin({"network": body}).status_code, 200)
        row = HostNetwork.objects.get(host=self.host)
        self.assertEqual(len(row.interfaces), 1)
        self.assertEqual(row.interfaces[0]["addrs"],
                         [{"family": "ipv4", "address": "10.0.0.5", "prefix": None}],
                         "a prefix out of range is unknown, not a rejection of the address")
        self.assertEqual([(l["proto"], l["port"]) for l in row.listening], [("tcp", 443)])
        self.assertEqual([(c["remote_ip"], c["count"]) for c in row.connections],
                         [("10.0.0.8", 1), ("10.0.0.9", 100000)],
                         "count 0 means one, and a huge one is capped")
        self.assertIs(row.partial, False, "partial is a bool, not whatever came in")

    def test_absent_key_keeps_the_last_snapshot(self):
        self._checkin({"network": NETWORK})
        before = HostNetwork.objects.get(host=self.host)
        self._checkin({"metrics": []})
        after = HostNetwork.objects.get(host=self.host)
        self.assertEqual(after.listening, before.listening)
        self.assertEqual(after.collected_at, before.collected_at)

    def test_a_later_checkin_replaces_the_snapshot(self):
        self._checkin({"network": NETWORK})
        self._checkin({"network": {"interfaces": [], "listening": [],
                                   "connections": [], "partial": True}})
        row = HostNetwork.objects.get(host=self.host)
        self.assertEqual(row.interfaces, [])
        self.assertTrue(row.partial)
