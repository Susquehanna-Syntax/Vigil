"""scan(): refusal before the first probe, who counts as alive, and the deadline.

Everything here is faked. No ping runs, no socket is opened, no SNMP reply is
read from a wire — netcheck.run_check, socket.create_connection,
socket.gethostbyaddr and V2cClient are all patched.
"""

import ipaddress
import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import netcheck
from vigil_agent.discovery import DEFAULT_PORTS, DiscoveryError, scan
from vigil_agent.discovery import SYSDESCR_OID as SYSDESCR

SYSNAME = netcheck.SYSNAME_OID


def ping_result(ok: bool, latency_ms: float | None = None) -> dict:
    return {"kind": "icmp", "ok": ok, "latency_ms": latency_ms}


class Fakes:
    """The three network calls and the SNMP client, all as patch objects."""

    def __init__(self, self_obj: unittest.TestCase) -> None:
        self.self_obj = self_obj
        self.pings: dict[str, dict] = {}
        self.connects: dict[tuple[str, int], str] = {}
        self.connect_calls: list[tuple[tuple[str, int], float]] = []
        self.names: dict[str, object] = {}
        self.snmp_values: dict[str, tuple[int, object]] = {}
        self.snmp_error: Exception | None = None
        self.snmp_hosts: list[str] = []
        self.clients: list[object] = []
        self.ping_error: Exception | None = None

    def __enter__(self):
        patches = [
            mock.patch("vigil_agent.discovery.netcheck.run_check", side_effect=self._run_check),
            mock.patch(
                "vigil_agent.discovery.socket.create_connection",
                side_effect=self._create_connection,
            ),
            mock.patch(
                "vigil_agent.discovery.socket.gethostbyaddr", side_effect=self._gethostbyaddr
            ),
            mock.patch("vigil_agent.discovery.V2cClient", side_effect=self._client),
        ]
        self.run_check = patches[0].start()
        self.create_connection = patches[1].start()
        self.gethostbyaddr = patches[2].start()
        self.client = patches[3].start()
        self.self_obj.addCleanup(lambda: [p.stop() for p in patches])
        return self

    # --- the fakes themselves

    def _run_check(self, check, *, timeout=1.0):
        host = check["host"]
        if self.ping_error is not None:
            raise self.ping_error
        return self.pings.get(host, ping_result(False))

    def _create_connection(self, address, *, timeout=None):
        self.connect_calls.append((tuple(address), timeout))
        outcome = self.connects.get(tuple(address), "silent")
        if outcome == "open":
            return mock.MagicMock()
        if outcome == "refused":
            raise ConnectionRefusedError
        raise TimeoutError

    def _gethostbyaddr(self, address):
        name = self.names.get(address, "")
        if isinstance(name, BaseException):
            raise name
        return (name, [], [])

    def _client(self, host, **kwargs):
        self.snmp_hosts.append(host)
        if self.snmp_error is not None:
            raise self.snmp_error

        class _Client:
            def __init__(self, values):
                self.values = values
                self.requested: list[str] = []

            def get(self, oids):
                self.requested = list(oids)
                return {oid: self.values[oid] for oid in oids}

        client = _Client(dict(self.snmp_values))
        self.clients.append(client)
        return client


class RefusalTests(unittest.TestCase):
    def test_every_refusal_raises_before_the_first_probe(self):
        bad = [
            "8.8.8.0/24",
            "10.0.0.0/21",
            "fd00::/120",
            "127.0.0.0/30",
            "224.0.0.0/30",
            "not-a-net",
        ]
        for cidr in bad:
            with self.subTest(cidr=cidr):
                fakes = Fakes(self).__enter__()
                with self.assertRaises(DiscoveryError):
                    scan(cidr)
                fakes.run_check.assert_not_called()
                fakes.create_connection.assert_not_called()

    def test_bad_port_lists_are_refused(self):
        bad = [[], [0], [70000], [True], list(range(22, 22 + 17))]
        for ports in bad:
            with self.subTest(ports=ports):
                fakes = Fakes(self).__enter__()
                with self.assertRaises(DiscoveryError):
                    scan("10.0.0.0/29", ports=ports)
                fakes.run_check.assert_not_called()

    def test_message_names_the_problem(self):
        fakes = Fakes(self).__enter__()
        with self.assertRaises(DiscoveryError) as caught:
            scan("8.8.8.0/24")
        self.assertEqual(str(caught.exception), "only private networks can be scanned")
        with self.assertRaises(DiscoveryError) as caught:
            scan("10.0.0.0/21")
        self.assertEqual(str(caught.exception), "at most 1024 addresses (a /22) per scan")
        with self.assertRaises(DiscoveryError) as caught:
            scan("fd00::/120")
        self.assertEqual(str(caught.exception), "only IPv4 networks can be scanned")
        fakes.run_check.assert_not_called()

        with self.assertRaises(DiscoveryError):
            scan("10.0.0.0/29", ports=["http"])
        fakes.run_check.assert_not_called()

    def test_a_full_slash_22_is_accepted(self):
        fakes = Fakes(self).__enter__()
        result = scan("10.0.0.0/22")
        self.assertEqual(result["cidr"], "10.0.0.0/22")
        self.assertEqual(result["scanned"], 1022)
        self.assertEqual(result["found"], [])
        self.assertIs(result["truncated"], False)
        fakes.client.assert_not_called()


class SweepTests(unittest.TestCase):
    def test_who_counts_as_alive(self):
        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 4.0)}
        fakes.connects = {
            ("10.0.0.2", 22): "refused",
            ("10.0.0.2", 443): "open",
            ("10.0.0.3", 22): "refused",
        }
        result = scan("10.0.0.0/29", ports=[22, 443], workers=1)

        self.assertEqual(
            [host["ip"] for host in result["found"]],
            ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
        )
        self.assertEqual([host["alive_by"] for host in result["found"]], ["ping", "tcp", "tcp"])
        self.assertEqual(result["found"][0]["latency_ms"], 4.0)
        self.assertEqual(result["found"][1]["open_ports"], [443])
        self.assertEqual(result["found"][2]["open_ports"], [])
        self.assertEqual(result["scanned"], 6)
        self.assertEqual(result["cidr"], "10.0.0.0/29")

    def test_one_probe_that_raises_loses_only_that_address(self):
        fakes = Fakes(self).__enter__()

        def run_check(check, *, timeout=1.0):
            if check["host"] == "10.0.0.2":
                raise RuntimeError("boom")
            return ping_result(check["host"] == "10.0.0.1", 1.0)

        fakes.run_check.side_effect = run_check
        result = scan("10.0.0.0/29", workers=1)
        self.assertEqual([host["ip"] for host in result["found"]], ["10.0.0.1"])
        self.assertEqual(result["scanned"], 6)

    def test_default_ports_are_probed_on_every_address(self):
        fakes = Fakes(self).__enter__()
        scan("10.0.0.0/30", workers=1)
        probed = {address for address, _timeout in fakes.connect_calls}
        self.assertEqual(len(probed), 2 * len(DEFAULT_PORTS))
        self.assertIn(("10.0.0.1", 445), probed)


class NameTests(unittest.TestCase):
    def test_reverse_dns_fills_the_name(self):
        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 2.0)}
        fakes.names = {"10.0.0.1": "router.internal"}
        result = scan("10.0.0.0/29", workers=1)
        self.assertEqual(result["found"][0]["name"], "router.internal")

    def test_an_oserror_leaves_the_name_empty(self):
        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 2.0)}
        fakes.names = {"10.0.0.1": OSError("not found")}
        result = scan("10.0.0.0/29", workers=1)
        self.assertEqual(result["found"][0]["name"], "")

    def test_the_name_is_cleaned_and_cut(self):
        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 2.0)}
        fakes.names = {"10.0.0.1": "edge\x01-" + "x" * 300}
        result = scan("10.0.0.0/29", workers=1)
        name = result["found"][0]["name"]
        self.assertEqual(name, "edge-" + "x" * 95)
        self.assertEqual(len(name), 100)


class SnmpTests(unittest.TestCase):
    def test_a_community_asks_found_hosts_only(self):
        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 2.0)}
        fakes.connects = {("10.0.0.2", 22): "open"}
        fakes.snmp_values = {
            SYSNAME: (0x04, b"switch-1\n"),
            SYSDESCR: (0x04, b"Acme OS \x01 12.4"),
        }
        result = scan("10.0.0.0/29", ports=[22], community="public", workers=1)

        self.assertEqual(sorted(fakes.snmp_hosts), ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(
            [client.requested for client in fakes.clients],  # type: ignore[attr-defined]
            [[netcheck.SYSNAME_OID, SYSDESCR]] * 2,
        )
        by_ip = {host["ip"]: host for host in result["found"]}
        self.assertEqual(by_ip["10.0.0.1"]["sys_name"], "switch-1")
        self.assertEqual(by_ip["10.0.0.2"]["sys_descr"], "Acme OS  12.4")

    def test_without_a_community_the_client_is_never_built(self):
        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 2.0)}
        result = scan("10.0.0.0/29", workers=1)
        fakes.client.assert_not_called()
        self.assertEqual(result["found"][0]["sys_name"], "")
        self.assertEqual(result["found"][0]["sys_descr"], "")

    def test_an_snmp_error_leaves_both_empty(self):
        from vigil_agent.snmp import SnmpError

        fakes = Fakes(self).__enter__()
        fakes.pings = {"10.0.0.1": ping_result(True, 2.0)}
        fakes.snmp_error = SnmpError("no reply")
        result = scan("10.0.0.0/29", community="public", workers=1)
        self.assertEqual(result["found"][0]["sys_name"], "")
        self.assertEqual(result["found"][0]["sys_descr"], "")


class LimitTests(unittest.TestCase):
    def test_the_deadline_stops_the_sweep(self):
        fakes = Fakes(self).__enter__()
        # The scan's start, then each worker's check before it probes: the
        # first two addresses are probed, the clock then jumps past the deadline.
        stamps = itertools.chain([0.0, 0.0, 0.0], itertools.repeat(1e6))

        with mock.patch("vigil_agent.discovery.time.monotonic", side_effect=lambda: next(stamps)):
            result = scan("10.0.0.0/29", workers=1)

        hosts = list(ipaddress.ip_network("10.0.0.0/29").hosts())
        self.assertIs(result["truncated"], True)
        self.assertLess(result["scanned"], len(hosts))
        self.assertEqual(result["scanned"], 2)
        pings = sorted(call.args[0]["host"] for call in fakes.run_check.call_args_list)
        self.assertEqual(pings, ["10.0.0.1", "10.0.0.2"])

    def test_limits_are_clamped_not_rejected(self):
        fakes = Fakes(self).__enter__()
        scan("10.0.0.0/30", timeout=99, workers=0, deadline=10_000)
        self.assertTrue(fakes.connect_calls)
        for _address, timeout in fakes.connect_calls:
            self.assertEqual(timeout, 5.0)


if __name__ == "__main__":
    unittest.main()
