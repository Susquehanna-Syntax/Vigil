"""The interface walk an SNMP check can opt in to (S9, phase 05d).

Five walks are joined on ifIndex, so the fake client scripts all five by OID
root and answers with the (oid, tag, value) rows a real agent returns. What is
worth asserting is the join: which interfaces exist, what each column adds, and
what a device that cannot answer the table costs the check — one clause in the
detail, nothing else. The GET itself belongs to tests/test_netcheck_snmp.py;
this file keeps that fake client without any `walk()` method (the classes here
have both), so the two never script the same exchange twice and an edit to one
side cannot silently pass on the other's assertions.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import netcheck, snmp
from vigil_agent.netcheck import (
    IF_HC_IN_OID,
    IF_HC_OUT_OID,
    IF_HIGH_SPEED_OID,
    IF_NAME_OID,
    IF_OPER_OID,
    MAX_INTERFACES,
    SYSNAME_OID,
    SYSUPTIME_OID,
    _snmp_interfaces,
    run_check,
)

COMMUNITY = "s3cret-community"

# What an agent answers the two fixed OIDs with. TimeTicks 243300 is 2433 seconds.
BASE = {SYSNAME_OID: (snmp.OCTET_STRING, b"switch-1"), SYSUPTIME_OID: (snmp.TimeTicks, 243_300)}

WALKS: list[tuple[str, int]] = []


def _rows(root: str, indexes, tag, make):
    """The rows of one column, in OID order: root.ifIndex for each index."""
    return [(f"{root}.{index}", tag, make(index)) for index in indexes]


def _device(indexes=(2, 3)):
    """A switch whose interfaces are all there: two of them by default."""
    return {
        IF_NAME_OID: _rows(IF_NAME_OID, indexes, snmp.OCTET_STRING, lambda i: f"eth{i}".encode()),
        IF_OPER_OID: _rows(IF_OPER_OID, indexes, snmp.INTEGER, lambda i: 1),
        IF_HIGH_SPEED_OID: _rows(IF_HIGH_SPEED_OID, indexes, snmp.Gauge32, lambda i: 1000 * i),
        IF_HC_IN_OID: _rows(IF_HC_IN_OID, indexes, snmp.Counter64, lambda i: 111 * i),
        IF_HC_OUT_OID: _rows(IF_HC_OUT_OID, indexes, snmp.Counter64, lambda i: 222 * i),
    }


def _client_class(*, responses=None, walks=None, get_error=None, walk_error=None):
    class _Client:
        def __init__(self, host, **kwargs):
            self.host = host

        def get(self, oids):
            if get_error is not None:
                raise get_error
            return {oid: responses[oid] for oid in oids if oid in responses}

        def walk(self, root, *, max_rows=10000, max_repetitions=25):
            WALKS.append((root, max_rows))
            if walk_error is not None:
                raise walk_error
            return walks[root]

    return _Client


def _check(**extra):
    check = {"kind": "snmp", "host": "router.lan", "community": COMMUNITY}
    check.update(extra)
    return check


class InterfaceJoinTests(unittest.TestCase):
    """Five walks, one table: what the join of the columns looks like."""

    def setUp(self):
        WALKS.clear()

    def tearDown(self):
        mock.patch.stopall()

    def _patch(self, **kwargs):
        mock.patch.object(snmp, "V2cClient", _client_class(**kwargs)).start()

    def test_two_interfaces_are_joined_across_the_five_walks(self):
        self._patch(responses=BASE, walks=_device())
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["interfaces"], [
            {"index": 2, "name": "eth2", "up": True, "speed_mbps": 2000,
             "in_octets": 222, "out_octets": 444},
            {"index": 3, "name": "eth3", "up": True, "speed_mbps": 3000,
             "in_octets": 333, "out_octets": 666},
        ])
        self.assertEqual(out["detail"], "up 40m, switch-1")

    def test_the_five_walks_in_order_and_at_most_MAX_INTERFACES_rows(self):
        self._patch(responses=BASE, walks=_device())
        run_check(_check(interfaces=True), timeout=2.0)
        self.assertEqual(WALKS, [(root, MAX_INTERFACES) for root in
                                 (IF_NAME_OID, IF_OPER_OID, IF_HIGH_SPEED_OID,
                                  IF_HC_IN_OID, IF_HC_OUT_OID)])

    def test_more_than_MAX_INTERFACES_rows_are_cut_to_MAX_INTERFACES(self):
        self._patch(responses=BASE, walks=_device(range(1, 100)))
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertEqual(len(out["interfaces"]), MAX_INTERFACES)
        self.assertEqual(out["interfaces"][0]["index"], 1)
        self.assertEqual(out["interfaces"][-1]["index"], MAX_INTERFACES)

    def test_an_index_only_another_column_reports_is_dropped(self):
        walks = _device((2,))
        walks[IF_HC_IN_OID] = _rows(IF_HC_IN_OID, (2, 7), snmp.Counter64, lambda i: 100)
        walks[IF_OPER_OID] = []
        self._patch(responses=BASE, walks=walks)
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertEqual(out["interfaces"], [
            {"index": 2, "name": "eth2", "up": None, "speed_mbps": 2000,
             "in_octets": 100, "out_octets": 444},
        ])

    def test_an_oper_status_that_is_neither_up_nor_down_is_not_known_to_be_down(self):
        walks = _device((2,))
        walks[IF_OPER_OID] = _rows(IF_OPER_OID, (2,), snmp.INTEGER, lambda i: 7)
        self._patch(responses=BASE, walks=walks)
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertIsNone(out["interfaces"][0]["up"])

    def test_a_name_that_is_not_text_and_a_column_that_is_missing_leave_no_value(self):
        walks = _device((2,))
        walks[IF_NAME_OID] = _rows(IF_NAME_OID, (2,), snmp.OCTET_STRING, lambda i: b"\xff" * 130)
        walks[IF_HIGH_SPEED_OID] = []
        self._patch(responses=BASE, walks=walks)
        out = run_check(_check(interfaces=True), timeout=2.0)
        interface = out["interfaces"][0]
        self.assertEqual(len(interface["name"]), netcheck.MAX_SNMP_VALUE_CHARS)
        self.assertIsNone(interface["speed_mbps"])

    def test_a_check_without_interfaces_never_walks(self):
        self._patch(responses=BASE, walks=_device())
        out = run_check(_check(), timeout=2.0)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("interfaces", out)
        self.assertEqual(WALKS, [])

    def test_a_device_that_cannot_answer_the_walks_keeps_the_get_and_says_so(self):
        for error in (snmp.SnmpTimeout("no SNMP response"), snmp.SnmpError("bad varbind")):
            with self.subTest(error=error):
                WALKS.clear()
                self._patch(responses=BASE, walks=_device(), walk_error=error)
                out = run_check(_check(interfaces=True), timeout=2.0)
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["detail"], "up 40m, switch-1, interfaces unreadable")
                self.assertNotIn("interfaces", out)

    def test_a_long_name_leaves_the_walk_clause_whole_at_the_end_of_the_detail(self):
        # The clause is added to the whole detail and then cut: a device named
        # at full length must still say that its interfaces were unreadable.
        name = "n" * 100
        self._patch(responses={**BASE, SYSNAME_OID: (snmp.OCTET_STRING, name.encode())},
                    walks=_device(), walk_error=snmp.SnmpError("x"))
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertEqual(out["detail"], f"up 40m, {name}, interfaces unreadable"[:200])
        self.assertTrue(out["detail"].endswith(", interfaces unreadable"))

    def test_a_get_that_failed_never_walks(self):
        self._patch(responses={}, get_error=snmp.SnmpTimeout("no SNMP response"),
                    walks=_device())
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "no SNMP response")
        self.assertEqual(WALKS, [])

    def test_the_walks_never_carry_the_community(self):
        self._patch(responses=BASE, walks=_device())
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertNotIn(COMMUNITY, repr(out))
        self.assertNotIn(COMMUNITY, repr(WALKS))


class InterfacesHelperTests(unittest.TestCase):
    """_snmp_interfaces on its own client: it is what the join is."""

    def setUp(self):
        WALKS.clear()

    def tearDown(self):
        mock.patch.stopall()

    def _patch(self, **kwargs):
        mock.patch.object(snmp, "V2cClient", _client_class(**kwargs)).start()

    def test_rows_out_of_index_order_come_back_sorted_by_index(self):
        walks = _device((2, 3))
        for root in walks:
            walks[root].reverse()
        with mock.patch.object(snmp, "V2cClient", _client_class(walks=walks)):
            client = snmp.V2cClient("router.lan")
        self.assertEqual([i["index"] for i in _snmp_interfaces(client)], [2, 3])

    def test_a_device_with_no_if_mib_at_all_has_no_interfaces(self):
        self._patch(responses=BASE, walks={root: [] for root in
                                           (IF_NAME_OID, IF_OPER_OID, IF_HIGH_SPEED_OID,
                                            IF_HC_IN_OID, IF_HC_OUT_OID)})
        out = run_check(_check(interfaces=True), timeout=2.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["interfaces"], [])
        # The detail is the GET's: an empty table is not a device to complain about.
        self.assertEqual(out["detail"], "up 40m, switch-1")


if __name__ == "__main__":
    unittest.main()
