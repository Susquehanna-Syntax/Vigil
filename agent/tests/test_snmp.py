"""BER encode/decode, message parsing and the v2c client against a fake agent.

Everything here is local: the agent is a UDP socket on 127.0.0.1 port 0 in a
daemon thread, speaking this same module's encoding.
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import snmp
from vigil_agent.snmp import (
    INTEGER,
    NULL,
    OBJECT_IDENTIFIER,
    OCTET_STRING,
    SEQUENCE,
    Counter32,
    Counter64,
    GetBulkRequest,
    GetNextRequest,
    GetRequest,
    IpAddress,
    Opaque,
    SnmpError,
    SnmpTimeout,
    V2cClient,
    build_request,
    decode,
    encode_integer,
    encode_length,
    encode_null,
    encode_octets,
    encode_oid,
    encode_sequence,
    endOfMibView,
    noSuchInstance,
    noSuchObject,
    oid_key,
    parse_response,
)

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"


def _value_bytes(tag: int, value) -> bytes:
    """Encode one MIB value the way an agent would put it in a varbind."""
    if tag == OCTET_STRING:
        return encode_octets(value)
    if tag == IpAddress:
        body = bytes(int(part) for part in value.split("."))
        return bytes([IpAddress, len(body)]) + body
    if tag in (noSuchObject, noSuchInstance, endOfMibView):
        return bytes([tag, 0x00])
    width = max(1, (value.bit_length() + 8) // 8)
    return bytes([tag, width]) + value.to_bytes(width, "big")


MIB = {
    SYS_DESCR: (OCTET_STRING, b"FakeOS 1.0"),
    SYS_NAME: (OCTET_STRING, b"switch-1"),
    f"{IF_DESCR}.1": (OCTET_STRING, b"eth0"),
    f"{IF_DESCR}.2": (OCTET_STRING, b"eth1"),
    f"{IF_DESCR}.3": (OCTET_STRING, b"vlan10"),
    f"{IF_IN_OCTETS}.1": (Counter32, 4000),
    f"{IF_IN_OCTETS}.2": (Counter32, 5000),
    f"{IF_IN_OCTETS}.3": (Counter64, 2**33 + 7),
    "1.3.6.1.2.1.4.20.1.1.10.0.0.1": (IpAddress, "10.0.0.1"),
}
SORTED = sorted(MIB, key=oid_key)


class _FakeAgent:
    """A UDP SNMP agent over a dict MIB. mode picks the pathological behaviour."""

    def __init__(
        self, *, community: bytes = b"public", mode: str = "ok", error_status: int = 0
    ):
        self.community = community
        self.mode = mode
        self.error_status = error_status
        # replies come from the listening address: the client drops other senders
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.requests: list[tuple[int, list[str]]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        wake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            wake.sendto(b"", ("127.0.0.1", self.port))  # unblock the pending recvfrom
        except OSError:
            pass
        wake.close()
        self.socket.close()
        self._thread.join(timeout=2)

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, peer = self.socket.recvfrom(65535)
            except OSError:
                return
            if not data:
                continue  # the wake-up datagram from stop()
            self._handle(data, peer)

    def _handle(self, data: bytes, peer) -> None:
        _, message, _ = decode(data)
        community = message[1][1]
        if community != self.community:
            return  # drop it: the client must time out on its own
        pdu_tag, pdu = message[2]
        request_id = pdu[0][1]
        names = [pair[1][0][1] for pair in pdu[3][1]]
        if not names:
            return  # a request with no varbinds is unanswerable; drop it
        self.requests.append((pdu_tag, names))
        if pdu_tag == GetRequest:
            rows = [
                (name, *MIB[name]) if name in MIB else (name, noSuchInstance, None)
                for name in names
            ]
            self._send(peer, request_id, rows)
        elif pdu_tag == GetNextRequest:
            reply = (
                [request_id ^ 0x55, request_id]
                if self.mode == "two_replies"
                else [request_id]
            )
            for one_id in reply:
                self._raw(
                    peer, one_id, 0, 0, self._body([_next(name) for name in names])
                )
        elif pdu_tag == GetBulkRequest:
            count = max(1, pdu[2][1])
            if self.mode == "same_oid":
                rows = [(f"{IF_DESCR}.1", *MIB[f"{IF_DESCR}.1"])] * count
            elif self.mode == "same_oid_endofmib":
                # a padded batch that repeats the start OID: legal, not a loop
                rows = _successor_chain(names[0], 1) + [
                    (names[0], endOfMibView, None)
                ] * max(0, count - 1)
            else:
                rows = _successor_chain(names[0], count)
            self._send(peer, request_id, rows)

    def _send(self, peer, request_id: int, rows) -> None:
        if self.error_status:
            self._raw(peer, request_id, self.error_status, 1, self._body(rows[:1]))
            return
        self._raw(peer, request_id, 0, 0, self._body(rows))

    @staticmethod
    def _body(rows) -> bytes:
        return b"".join(
            encode_sequence(encode_oid(name), _value_bytes(tag, value))
            for name, tag, value in rows
        )

    def _raw(
        self,
        peer,
        request_id: int,
        error_status: int,
        error_index: int,
        varbind_body: bytes,
    ) -> None:
        message = encode_sequence(
            encode_integer(1),
            encode_octets(b"public"),
            encode_sequence(
                encode_integer(request_id),
                encode_integer(error_status),
                encode_integer(error_index),
                encode_sequence(varbind_body),
                tag=0xA2,
            ),
        )
        try:
            self.socket.sendto(message, peer)
        except OSError:
            pass


def _next(name: str):
    key = oid_key(name)
    for candidate in SORTED:
        if oid_key(candidate) > key:
            return (candidate, *MIB[candidate])
    return (name, 0x82, None)


def _successor_chain(start: str, count: int) -> list[tuple[str, int, object]]:
    """The `count` rows after start. EndOfMibView pads the batch like a real agent."""
    key = oid_key(start)
    following = [(c, *MIB[c]) for c in SORTED if oid_key(c) > key]
    if len(following) < count:
        following += [(start, 0x82, None)] * (count - len(following))
    return following[:count]


class EncodingVectors(unittest.TestCase):
    def test_length_short_and_long_forms(self):
        self.assertEqual(encode_length(0), bytes([0x00]))
        self.assertEqual(encode_length(127), bytes([0x7F]))
        self.assertEqual(encode_length(128), bytes([0x81, 0x80]))
        self.assertEqual(encode_length(300), bytes([0x82, 0x01, 0x2C]))
        self.assertEqual(encode_length(70000), bytes([0x83, 0x01, 0x11, 0x70]))

    def test_integer_vectors(self):
        self.assertEqual(encode_integer(0).hex(), "020100")
        self.assertEqual(encode_integer(127).hex(), "02017f")
        self.assertEqual(encode_integer(128).hex(), "02020080")
        self.assertEqual(encode_integer(-1).hex(), "0201ff")
        self.assertEqual(encode_integer(-129).hex(), "0202ff7f")
        self.assertEqual(encode_integer(256).hex(), "02020100")

    def test_octets_null_sequence(self):
        self.assertEqual(encode_octets(b"public").hex(), "04067075626c6963")
        self.assertEqual(encode_null().hex(), "0500")
        self.assertEqual(encode_sequence(bytes([0x02, 0x01, 0x00])).hex(), "3003020100")
        self.assertEqual(encode_sequence(tag=GetRequest).hex(), "a000")

    def test_oid_vectors(self):
        self.assertEqual(encode_oid("1.3.6.1.2.1.1.1.0").hex(), "06082b06010201010100")
        self.assertEqual(
            encode_oid("1.3.6.1.4.1.4294967295").hex(), "060a2b060104018fffffff7f"
        )

    def test_integer_decodes_back(self):
        for value in (0, 127, 128, -1, -129, 256, 2**31 - 1, -(2**31)):
            tag, decoded, rest = decode(encode_integer(value))
            self.assertEqual(
                (tag, decoded, rest), (INTEGER, value, len(encode_integer(value)))
            )

    def test_oid_decodes_back(self):
        for oid in ("1.3.6.1.2.1.1.1.0", "1.3.6.1.4.1.4294967295", "0.0", "2.5.4.3"):
            tag, decoded, _ = decode(encode_oid(oid))
            self.assertEqual((tag, decoded), (OBJECT_IDENTIFIER, oid))

    def test_scalar_types(self):
        tag, value, _ = decode(encode_octets(b"eth0"))
        self.assertEqual((tag, value), (OCTET_STRING, b"eth0"))
        self.assertEqual(decode(bytes([NULL, 0x00]))[1], None)
        self.assertEqual(decode(bytes([IpAddress, 4, 10, 0, 0, 1]))[1], "10.0.0.1")
        self.assertEqual(decode(bytes([Counter32, 5, 0x00, 0x80, 0, 0, 0]))[1], 2**31)
        self.assertEqual(decode(bytes([Opaque, 1, 0x09]))[:2], (Opaque, b"\x09"))
        counter64 = bytes([Counter64, 9, 0x00]) + (2**63).to_bytes(8, "big")
        self.assertEqual(decode(counter64)[1], 2**63)
        for exception_tag in (0x80, 0x81, 0x82):
            self.assertEqual(decode(bytes([exception_tag, 0x00]))[1], None)
        self.assertEqual(decode(bytes([0x9B, 0x01, 0x07]))[:2], (0x9B, b"\x07"))

    def test_sequence_decodes_to_children(self):
        packet = encode_sequence(encode_integer(1), encode_octets(b"public"))
        tag, value, _ = decode(packet)
        self.assertEqual(tag, SEQUENCE)
        self.assertEqual(value, [(INTEGER, 1), (OCTET_STRING, b"public")])

    def test_build_request_matches_the_reference_bytes(self):
        packet = build_request(GetRequest, 1, b"public", [SYS_NAME])
        self.assertEqual(
            packet.hex(),
            "302602010104067075626c6963a019020101020100020100"
            "300e300c06082b060102010105000500",
        )

    def test_build_request_getbulk_carries_the_two_counters(self):
        packet = build_request(
            GetBulkRequest,
            9,
            b"public",
            [IF_DESCR],
            non_repeaters=0,
            max_repetitions=25,
        )
        _, message, _ = decode(packet)
        pdu = message[2][1]
        self.assertEqual(message[2][0], GetBulkRequest)
        self.assertEqual([pdu[0][1], pdu[1][1], pdu[2][1]], [9, 0, 25])

    def test_getrequest_counters_are_always_zero(self):
        packet = build_request(
            GetNextRequest, 4, b"c", [SYS_NAME], non_repeaters=7, max_repetitions=8
        )
        _, message, _ = decode(packet)
        pdu = message[2][1]
        self.assertEqual([pdu[1][1], pdu[2][1]], [0, 0])


class MalformedInput(unittest.TestCase):
    def _assert_malformed(self, packet: bytes) -> None:
        with self.assertRaises(SnmpError):
            decode(packet)

    def test_length_past_the_buffer(self):
        self._assert_malformed(bytes([INTEGER, 0x05, 0x01, 0x02, 0x03]))

    def test_absurd_long_form_length(self):
        self._assert_malformed(bytes([INTEGER, 0x85, 1, 2, 3, 4, 5, 0x01]))
        self._assert_malformed(bytes([INTEGER, 0x85]) + bytes(8))

    def test_indefinite_length(self):
        self._assert_malformed(bytes([SEQUENCE, 0x80, 0x00, 0x00]))

    def test_deep_nesting(self):
        packet = b""
        for _ in range(snmp.MAX_DECODE_DEPTH + 4):
            packet = bytes([SEQUENCE, len(packet)]) + packet
        self._assert_malformed(packet)

    def test_empty_integer(self):
        self._assert_malformed(bytes([INTEGER, 0x00]))

    def test_oid_with_unterminated_arc(self):
        self._assert_malformed(bytes([OBJECT_IDENTIFIER, 0x02, 0x2B, 0x81]))

    def test_ip_address_of_three_bytes(self):
        self._assert_malformed(bytes([IpAddress, 0x03, 10, 0, 0]))

    def test_every_truncation_of_a_response_is_an_snmp_error(self):
        packet = _response_bytes(7, [(SYS_NAME, OCTET_STRING, b"switch-1")])
        for cut in range(len(packet)):
            try:
                parse_response(packet[:cut])
            except SnmpError:
                continue
            except Exception as exc:  # noqa: BLE001 — the point of the test
                self.fail(f"packet[:{cut}] raised {type(exc).__name__}: {exc}")
            self.fail(f"packet[:{cut}] parsed")

    def test_wrong_version_or_pdu_tag(self):
        wrong_version = encode_sequence(
            encode_integer(2),
            encode_octets(b"public"),
            encode_sequence(
                encode_integer(7),
                encode_integer(0),
                encode_integer(0),
                encode_sequence(),
                tag=0xA2,
            ),
        )
        with self.assertRaises(SnmpError):
            parse_response(wrong_version)
        get_pdu = encode_sequence(
            encode_integer(1),
            encode_integer(0),
            encode_integer(0),
            encode_sequence(),
            tag=GetRequest,
        )
        wrong_pdu = encode_sequence(
            encode_integer(1), encode_octets(b"public"), get_pdu
        )
        with self.assertRaises(SnmpError):
            parse_response(wrong_pdu)

    def test_bad_oid_strings(self):
        for oid in ("1", "", "3.1", "0.40", "1.x.2", "-1.2.3"):
            with self.assertRaises(SnmpError):
                encode_oid(oid)


def _response_bytes(
    request_id: int, rows, *, error_status: int = 0, error_index: int = 0
) -> bytes:
    body = b"".join(
        encode_sequence(encode_oid(name), _value_bytes(tag, value))
        for name, tag, value in rows
    )
    return encode_sequence(
        encode_integer(1),
        encode_octets(b"public"),
        encode_sequence(
            encode_integer(request_id),
            encode_integer(error_status),
            encode_integer(error_index),
            encode_sequence(body),
            tag=0xA2,
        ),
    )


class ParseResponse(unittest.TestCase):
    def test_fields(self):
        packet = _response_bytes(
            7,
            [
                (SYS_NAME, OCTET_STRING, b"switch-1"),
                (IF_IN_OCTETS + ".1", Counter32, 4000),
            ],
        )
        response = parse_response(packet)
        self.assertEqual(response.version, 1)
        self.assertEqual(response.community, b"public")
        self.assertEqual(response.request_id, 7)
        self.assertEqual(response.error_status, 0)
        self.assertEqual(response.error_index, 0)
        self.assertEqual(
            response.varbinds,
            [
                (SYS_NAME, OCTET_STRING, b"switch-1"),
                (IF_IN_OCTETS + ".1", Counter32, 4000),
            ],
        )


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.agent = _FakeAgent()
        self.addCleanup(self.agent.stop)
        self.client = V2cClient(
            "127.0.0.1", community="public", port=self.agent.port, timeout=1.0
        )

    def test_get_two_oids(self):
        found = self.client.get([SYS_DESCR, SYS_NAME])
        self.assertEqual(found[SYS_DESCR], (OCTET_STRING, b"FakeOS 1.0"))
        self.assertEqual(found[SYS_NAME], (OCTET_STRING, b"switch-1"))

    def test_get_missing_oid_is_no_such_instance(self):
        found = self.client.get(["1.3.6.1.2.1.99.0"])
        self.assertEqual(found["1.3.6.1.2.1.99.0"], (0x81, None))

    def test_get_carries_at_most_32_oids(self):
        with self.assertRaises(SnmpError):
            self.client.get([SYS_NAME] * 33)
        with self.assertRaises(SnmpError):
            self.client.get([])

    def test_walk_returns_exactly_the_column(self):
        rows = self.client.walk(IF_DESCR)
        self.assertEqual(
            [oid for oid, _tag, _v in rows],
            [f"{IF_DESCR}.1", f"{IF_DESCR}.2", f"{IF_DESCR}.3"],
        )
        self.assertEqual(
            [value for _o, _t, value in rows], [b"eth0", b"eth1", b"vlan10"]
        )

    def test_walk_respects_max_rows(self):
        rows = self.client.walk(IF_IN_OCTETS, max_rows=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [oid for oid, _t, _v in rows], [f"{IF_IN_OCTETS}.1", f"{IF_IN_OCTETS}.2"]
        )

    def test_walk_keeps_a_counter64_and_an_ip_address(self):
        rows = self.client.walk(IF_IN_OCTETS)
        self.assertEqual(rows[2], (f"{IF_IN_OCTETS}.3", Counter64, 2**33 + 7))
        one_row = self.client.walk("1.3.6.1.2.1.4.20.1.1")
        self.assertEqual(one_row[0][1:], (IpAddress, "10.0.0.1"))

    def test_walk_stays_inside_the_subtree_by_arcs(self):
        # .10 is not inside .1: the check compares arcs as integers, never strings
        rows = self.client.walk(f"{IF_DESCR[:-1]}1", max_repetitions=25)
        self.assertEqual(rows, [])

    def test_walk_stops_at_end_of_mib_view(self):
        rows = self.client.walk("1.3.6.1.2.1.4.20.1.1", max_repetitions=5)
        self.assertEqual(len(rows), 1)

    def test_walk_accepts_a_padded_batch_that_repeats_the_start_oid(self):
        padded = _FakeAgent(mode="same_oid_endofmib")
        self.addCleanup(padded.stop)
        client = V2cClient(
            "127.0.0.1", community="public", port=padded.port, timeout=1.0
        )
        rows = client.walk(IF_DESCR, max_repetitions=10)
        self.assertEqual([oid for oid, _t, _v in rows], [f"{IF_DESCR}.1"])

    def test_wrong_community_times_out_quickly(self):
        quiet = V2cClient(
            "127.0.0.1", community="wrong", port=self.agent.port, timeout=0.2, retries=0
        )
        started = time.monotonic()
        with self.assertRaises(SnmpTimeout) as caught:
            quiet.get([SYS_NAME])
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(
            str(caught.exception), f"no SNMP response from 127.0.0.1:{self.agent.port}"
        )

    def test_a_reply_with_the_wrong_request_id_is_ignored(self):
        agent = _FakeAgent(mode="two_replies")
        self.addCleanup(agent.stop)
        client = V2cClient(
            "127.0.0.1", community="public", port=agent.port, timeout=1.0
        )
        self.assertEqual(client.get([SYS_NAME])[SYS_NAME], (OCTET_STRING, b"switch-1"))

    def test_an_agent_that_repeats_one_oid_is_reported(self):
        looping = _FakeAgent(mode="same_oid")
        self.addCleanup(looping.stop)
        client = V2cClient(
            "127.0.0.1", community="public", port=looping.port, timeout=1.0
        )
        with self.assertRaises(SnmpError) as caught:
            client.walk(IF_DESCR)
        self.assertEqual(str(caught.exception), "SNMP agent returned OIDs out of order")

    def test_error_status_is_named(self):
        broken = _FakeAgent(error_status=2)
        self.addCleanup(broken.stop)
        client = V2cClient(
            "127.0.0.1", community="public", port=broken.port, timeout=1.0
        )
        with self.assertRaises(SnmpError) as caught:
            client.get([SYS_NAME])
        self.assertIn("noSuchName", str(caught.exception))
        self.assertIn(SYS_NAME, str(caught.exception))

    def test_unknown_error_status_number(self):
        broken = _FakeAgent(error_status=99)
        self.addCleanup(broken.stop)
        client = V2cClient(
            "127.0.0.1", community="public", port=broken.port, timeout=1.0
        )
        with self.assertRaises(SnmpError) as caught:
            client.get([SYS_NAME])
        self.assertIn("error status 99", str(caught.exception))

    def test_walk_uses_getbulk_and_stops_outside_the_subtree(self):
        rows = self.client.walk(IF_IN_OCTETS, max_repetitions=25)
        self.assertEqual(
            [oid for oid, _t, _v in rows], [f"{IF_IN_OCTETS}.{i}" for i in (1, 2, 3)]
        )
        bulk_only = [
            tag for tag, _names in self.agent.requests if tag == GetBulkRequest
        ]
        self.assertTrue(bulk_only)


class OidOrdering(unittest.TestCase):
    def test_arcs_compare_as_integers(self):
        self.assertLess(oid_key("1.3.6.1.2.1.2.2.1.2"), oid_key("1.3.6.1.2.1.2.2.1.10"))

    def test_subtree_membership_is_by_arcs(self):
        self.assertTrue(
            snmp._inside(
                oid_key("1.3.6.1.2.1.2.2.1.10.1"), oid_key("1.3.6.1.2.1.2.2.1.10")
            )
        )
        self.assertFalse(
            snmp._inside(
                oid_key("1.3.6.1.2.1.2.2.1.10"), oid_key("1.3.6.1.2.1.2.2.1.1")
            )
        )
        self.assertFalse(
            snmp._inside(
                oid_key("1.3.6.1.2.1.2.2.1.1"), oid_key("1.3.6.1.2.1.2.2.1.10")
            )
        )


if __name__ == "__main__":
    unittest.main()
