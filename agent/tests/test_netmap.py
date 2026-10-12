"""The network map (14.4): interface addresses, listening sockets and live
connections, from a faked psutil, and the check-in that carries them."""
import tests._safety_net  # noqa: F401
import socket
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
import psutil
from vigil_agent import client, netmap
from vigil_agent.config import AgentConfig


AF_LINK = psutil.AF_LINK


#: psutil's own row shapes, so attribute access is what the code sees.
Addr = namedtuple("Addr", "family address netmask broadcast ptp")
Laddr = namedtuple("Laddr", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")
Stats = namedtuple("Stats", "isup duplex speed mtu flags")


def v4(address, netmask="255.255.255.0"):
    return Addr(socket.AF_INET, address, netmask, None, None)


def v6(address, netmask="ffff:ffff:ffff:ffff::"):
    return Addr(socket.AF_INET6, address, netmask, None, None)


def mac(address):
    return Addr(AF_LINK, address, None, None, None)


def listen(proto, ip, port, pid=100):
    kind = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    return Conn(3, socket.AF_INET, kind, Laddr(ip, port), None, psutil.CONN_LISTEN, pid)


def established(ip, port, local_port, pid=200):
    return Conn(4, socket.AF_INET, socket.SOCK_STREAM, Laddr("10.0.0.5", local_port),
                Laddr(ip, port), psutil.CONN_ESTABLISHED, pid)


def idle():
    return Conn(5, socket.AF_INET, socket.SOCK_STREAM, Laddr("10.0.0.5", 41000),
                Laddr("10.0.0.9", 22), psutil.CONN_NONE, 200)


INTERFACES = {
    "eth0": [v4("10.0.0.5"), v6("fe80::12%eth0"), mac("aa:bb:cc:dd:ee:ff")],
    "lo": [v4("127.0.0.1", "255.0.0.0"), v6("::1", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")],
}
STATS = {"eth0": Stats(True, 2, 1000, 1500, "up"), "lo": Stats(True, 0, 0, 65536, "up")}


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "ok", "tasks": []}


class NetmapTests(unittest.TestCase):
    def _collect(self, addrs=INTERFACES, stats=STATS, conns=()):
        with patch.object(netmap.psutil, "net_if_addrs", return_value=addrs), \
                patch.object(netmap.psutil, "net_if_stats", return_value=stats), \
                patch.object(netmap.psutil, "net_connections", return_value=list(conns)), \
                patch.object(netmap.psutil, "Process",
                             lambda pid: type("P", (), {"name": lambda self: "sshd"})()):
            return netmap.collect_network()

    def test_interfaces_skip_loopback_and_compute_prefix(self):
        out = self._collect()
        self.assertEqual([i["name"] for i in out["interfaces"]], ["eth0", "lo"])
        eth0 = out["interfaces"][0]
        self.assertTrue(eth0["up"])
        self.assertEqual(eth0["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(eth0["addrs"], [{"family": "ipv4", "address": "10.0.0.5", "prefix": 24},
                                         {"family": "ipv6", "address": "fe80::12", "prefix": 64}],
                         "the %zone suffix is stripped")
        self.assertEqual(out["interfaces"][1]["addrs"], [], "127.0.0.1 and ::1 are not topology")
        self.assertEqual(out["interfaces"][1]["mac"], "", "lo has no hardware address here")

    def test_listening_and_connections_are_aggregated(self):
        conns = [listen("tcp", "0.0.0.0", 22),
                 listen("tcp", "0.0.0.0", 22, pid=101),      # same socket, two threads
                 listen("udp", "0.0.0.0", 5353),
                 established("10.0.0.9", 51000, 22),
                 established("10.0.0.9", 51001, 22),
                 established("10.0.0.7", 5432, 45000),
                 established("10.0.0.7", 5432, 45001),
                 established("10.0.0.7", 5432, 45002),
                 established("127.0.0.1", 8080, 45100),      # this machine talking to itself
                 idle()]
        out = self._collect(conns=conns)
        self.assertEqual([(r["proto"], r["port"]) for r in out["listening"]],
                         [("tcp", 22), ("udp", 5353)], "deduped and sorted by port")
        self.assertEqual(out["listening"][1]["address"], "0.0.0.0")
        self.assertEqual(out["listening"][1]["process"], "sshd")
        self.assertEqual([(r["direction"], r["remote_ip"], r["remote_port"],
                           r["local_port"], r["count"]) for r in out["connections"]],
                         [("out", "10.0.0.7", 5432, 0, 3), ("in", "10.0.0.9", 0, 22, 2)],
                         "most-used first; inbound counts on the local port")
        self.assertFalse(out["partial"])

    def test_access_denied_is_partial(self):
        with patch.object(netmap.psutil, "net_if_addrs", return_value=INTERFACES), \
                patch.object(netmap.psutil, "net_if_stats", return_value=STATS), \
                patch.object(netmap.psutil, "net_connections",
                             side_effect=psutil.AccessDenied(1, "denied")):
            out = netmap.collect_network()
        self.assertTrue(out["partial"])
        self.assertEqual([i["name"] for i in out["interfaces"]], ["eth0", "lo"],
                         "the addresses need no privilege")
        self.assertEqual(out["listening"], [])
        self.assertEqual(out["connections"], [])

    def test_udp_without_a_bound_port_is_not_listening(self):
        out = self._collect(conns=[Conn(9, socket.AF_INET, socket.SOCK_DGRAM,
                                        Laddr("0.0.0.0", 0), None, psutil.CONN_NONE, 1)])
        self.assertEqual(out["listening"], [])


class CheckinNetworkTests(unittest.TestCase):
    def setUp(self):
        self.sent = {}

        def fake_post(url, json=None, **kwargs):
            self.sent.clear()
            self.sent.update(json or {})
            return _Resp()

        p = patch.object(client.requests, "post", fake_post)
        p.start()
        self.addCleanup(p.stop)

    def _config(self, tmp):
        return AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                           data_dir=Path(tmp), mode="monitor")

    def test_checkin_sends_network_only_when_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            client.checkin(self._config(tmp), metrics=[])
            self.assertNotIn("network", self.sent, "absent means keep the last snapshot")
            client.checkin(self._config(tmp), metrics=[], network={"interfaces": [],
                                                                   "partial": True})
            self.assertEqual(self.sent["network"], {"interfaces": [], "partial": True})


if __name__ == "__main__":
    unittest.main()
