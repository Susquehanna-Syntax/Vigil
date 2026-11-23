"""The snmp check kind (S9, phase 05b): a v2c GET.

V2cClient is the one class this check uses and get() the only thing that leaves
the process, so the paths that depend on an agent's answer script that one
method with the (tag, value) tuples a real agent returns — including the two
exception values noSuchObject and noSuchInstance — and assert the whole result.
The wrong-community case is the exception: a timeout is the only thing a wrong
community can look like, and it must never say which. That one runs against the
fake UDP agent of tests/test_snmp.py for real.

The interface walk (`interfaces: true`, five walks joined on ifIndex, the
", interfaces unreadable" clause) is phase 05d's and lives in
tests.test_netcheck_snmp_interfaces. Nothing here asserts it — that fake client
would have to grow a walk() for it and both files would then own one answer.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import netcheck, snmp
from vigil_agent.netcheck import SYSNAME_OID, SYSUPTIME_OID, _snmp, run_check

from tests.test_snmp import _FakeAgent

COMMUNITY = "s3cret-community"
IF_IN_OCTETS_1 = "1.3.6.1.2.1.2.2.1.10.1"  # labelled as in_octets below
IP_ADDR = "1.3.6.1.2.1.4.20.1.1.10.0.0.1"  # labelled as addr below

OIDS = [
    {"oid": IF_IN_OCTETS_1, "label": "in_octets"},
    {"oid": IP_ADDR, "label": "addr"},
]

# What an agent answers our two fixed OIDs with. TimeTicks 243300 is 2433 seconds.
BASE = {SYSNAME_OID: (snmp.OCTET_STRING, b"switch-1"), SYSUPTIME_OID: (snmp.TimeTicks, 243_300)}

CALLS: list[list[str]] = []


def _client_class(responses: dict[str, tuple[int, object]], error: Exception | None):
    class _Client:
        def __init__(self, host, **kwargs):
            self.host = host
            self.kwargs = kwargs

        def get(self, oids):
            CALLS.append(list(oids))
            if error is not None:
                raise error
            return {oid: responses[oid] for oid in oids if oid in responses}

    return _Client


class SnmpCheckTests(unittest.TestCase):
    """Scripted answers: what the result looks like for each kind of reply."""

    def setUp(self):
        CALLS.clear()

    def tearDown(self):
        mock.patch.stopall()

    def _patch(self, responses, *, error=None):
        patcher = mock.patch.object(snmp, "V2cClient", _client_class(responses, error))
        patcher.start()

    def test_a_successful_check_reports_the_labelled_oids_and_its_detail(self):
        self._patch({**BASE, IF_IN_OCTETS_1: (snmp.Counter32, 4000),
                     IP_ADDR: (snmp.IpAddress, "10.0.0.1")})
        out = run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY,
                         "oids": OIDS}, timeout=2.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["target"], "router.lan")
        self.assertEqual(out["sys_name"], "switch-1")
        self.assertEqual(out["uptime_s"], 2433)      # hundredths of a second
        self.assertEqual(out["in_octets"], 4000)     # a Counter32 stays an int
        self.assertEqual(out["addr"], "10.0.0.1")    # an IpAddress stays a dotted quad
        self.assertEqual(out["detail"], "up 40m, switch-1")

    def test_one_get_of_the_fixed_oids_then_the_labels(self):
        self._patch(BASE)
        run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY, "oids": OIDS})
        self.assertEqual(CALLS, [[SYSNAME_OID, SYSUPTIME_OID, IF_IN_OCTETS_1, IP_ADDR]])

    def test_no_name_and_no_uptime_still_say_up(self):
        self._patch({SYSNAME_OID: (snmp.NULL, None), SYSUPTIME_OID: (snmp.noSuchObject, None)})
        out = run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY})
        self.assertTrue(out["ok"], out)
        self.assertIsNone(out["sys_name"])
        self.assertIsNone(out["uptime_s"])
        self.assertEqual(out["detail"], "up")

    def test_a_labelled_object_that_is_not_there_fails_naming_it(self):
        self._patch({**BASE, IF_IN_OCTETS_1: (snmp.noSuchInstance, None),
                     IP_ADDR: (snmp.noSuchObject, None)})
        out = run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY,
                         "oids": OIDS})
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "no such object: in_octets, addr")
        self.assertIsNone(out["in_octets"])
        self.assertIsNone(out["addr"])
        self.assertEqual(out["sys_name"], "switch-1")

    def test_a_protocol_error_is_the_detail_cut_to_200(self):
        self._patch({}, error=snmp.SnmpError("x" * 400))
        out = run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY})
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "x" * 200)

    def test_a_check_without_a_community_never_builds_a_client(self):
        self._patch({}, error=AssertionError("unreachable"))
        out = run_check({"kind": "snmp", "host": "router.lan"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "no SNMP community")
        self.assertIsNone(out["sys_name"])
        self.assertIsNone(out["uptime_s"])
        self.assertEqual(CALLS, [])

    def test_an_invalid_host_is_refused_with_the_kind_extras_present(self):
        out = run_check({"kind": "snmp", "host": "-bad", "community": COMMUNITY})
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "invalid host")
        self.assertIsNone(out["sys_name"])
        self.assertIsNone(out["uptime_s"])

    def test_the_community_never_appears_in_a_result(self):
        self._patch({**BASE, IF_IN_OCTETS_1: (snmp.Counter32, 4000),
                     IP_ADDR: (snmp.IpAddress, "10.0.0.1")})
        good = run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY,
                          "oids": OIDS})
        mock.patch.stopall()
        self._patch({}, error=snmp.SnmpTimeout("no SNMP response"))
        bad = run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY,
                         "oids": OIDS})
        self.assertTrue(good["ok"], good)
        self.assertFalse(bad["ok"])
        for out in (good, bad):
            self.assertNotIn(COMMUNITY, repr(out))

    def test_the_port_defaults_to_161_and_the_client_gets_what_we_promise(self):
        seen: list[tuple[str, dict]] = []

        class _Client:
            def __init__(self, host, **kwargs):
                seen.append((host, kwargs))

            def get(self, oids):
                return {oid: BASE[oid] for oid in oids if oid in BASE}

        with mock.patch.object(snmp, "V2cClient", _Client):
            run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY})
            first = seen[0]
            run_check({"kind": "snmp", "host": "router.lan", "community": COMMUNITY, "port": 1161})

        self.assertEqual(first[0], "router.lan")
        self.assertEqual(first[1]["port"], 161)
        self.assertEqual(seen[1][1]["port"], 1161)
        for _host, kwargs in seen:
            self.assertEqual(kwargs["community"], COMMUNITY)
            self.assertEqual(kwargs["retries"], 1)

    def test_run_check_routes_snmp_to_the_handler(self):
        # The real _snmp, reached through run_check: an invalid host proves the
        # snmp handler ran and none of the others could have said this.
        out = run_check({"kind": "snmp", "host": "-bad", "community": COMMUNITY})
        self.assertEqual(out["kind"], "snmp")
        self.assertEqual(out["detail"], "invalid host")

    def test_the_handler_table_routes_snmp_to_the_snmp_handler(self):
        self.assertIs(netcheck._HANDLERS["snmp"], _snmp)
        self.assertEqual(netcheck._TARGET_KEYS["snmp"], "host")


class SnmpAgainstAFakeAgentTests(unittest.TestCase):
    """The one path worth a real socket: no answer at all."""

    def setUp(self):
        self.agent = _FakeAgent(community=COMMUNITY.encode())
        self.addCleanup(self.agent.stop)

    def test_a_wrong_community_is_no_response_and_never_names_itself(self):
        out = run_check({"kind": "snmp", "host": "127.0.0.1", "port": self.agent.port,
                         "community": "not-it"}, timeout=0.3)
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "no SNMP response")
        self.assertNotIn("not-it", repr(out))


if __name__ == "__main__":
    unittest.main()
