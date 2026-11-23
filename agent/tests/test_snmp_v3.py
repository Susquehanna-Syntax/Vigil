"""The SNMPv3 client: USM keys, engine discovery, authPriv and authNoPriv.

The fake v3 agent here answers bytes with bytes using its own HMAC, SHA and AES
code (`hmac`, `hashlib` and `cryptography` directly) and borrows only byte-offset
helpers from the module — never its key, MAC or AES functions — so a wrong one
of those shows up as a disagreement instead of agreeing with itself. Nothing
leaves the process: the socket is patched, as tests/test_snmp.py patches it.
"""

import builtins
import hashlib
import hmac as hmac_module
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from vigil_agent import snmp
from vigil_agent.netcheck import SYSNAME_OID, SYSUPTIME_OID, run_check
from vigil_agent.snmp import (
    GetBulkRequest,
    MSG_FLAG_AUTH,
    MSG_FLAG_PRIV,
    MSG_FLAG_REPORTABLE,
    INTEGER,
    OCTET_STRING,
    Report,
    Response,
    SEQUENCE,
    SnmpError,
    SnmpTimeout,
    TimeTicks,
    V3Client,
    decode,
    encode_length,
    encode_octets,
    encode_sequence,
)

from tests.test_snmp import MIB, _value_bytes

USER = "vigil-user"
AUTH_PASSWORD = "authPassphrase-1"
PRIV_PASSWORD = "privPassphrase-2"
SECRET_WORDS = (USER, AUTH_PASSWORD, PRIV_PASSWORD)
ENGINE_ID = bytes.fromhex("80001f8880654321789abcde")

SYS_NAME = SYSNAME_OID
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"

USM_UNSUPPORTED_SEC_LEVELS = "1.3.6.1.6.3.15.1.1.1.0"
USM_NOT_IN_TIME_WINDOWS = "1.3.6.1.6.3.15.1.1.2.0"
USM_UNKNOWN_USER_NAMES = "1.3.6.1.6.3.15.1.1.3.0"
USM_WRONG_DIGESTS = "1.3.6.1.6.3.15.1.1.5.0"
USM_DECRYPTION_ERRORS = "1.3.6.1.6.3.15.1.1.6.0"

# The id a reply answers when the fake could not read the request at all.
_GARBAGE_REQUEST_ID = 0x7FF00D


def _password_to_key(password: bytes, digest) -> bytes:
    """The RFC 3414 A.2 loop again, written here so the fake stands on its own."""
    hasher = digest()
    repeated = (password * (1_048_576 // len(password) + 1))[:1_048_576]
    for offset in range(0, 1_048_576, 64):
        hasher.update(repeated[offset:offset + 64])
    return hasher.digest()


def _localize(key: bytes, engine_id: bytes, digest) -> bytes:
    return digest(key + engine_id + key).digest()


def _priv_key(password: str, engine_id: bytes, digest) -> bytes:
    """RFC 3826 3.1.2.1: the AES-128 privacy key comes from the auth digest."""
    return _localize(_password_to_key(password.encode("utf-8"), digest), engine_id,
                     digest)[:16]


def _aes(key: bytes, data: bytes, iv: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.CFB(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_decrypt(key: bytes, data: bytes, iv: bytes) -> bytes:
    """CFB decrypts with its own direction: an encryptor gets only the first block right."""
    decryptor = Cipher(algorithms.AES(key), modes.CFB(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


class TestUsmKeys(unittest.TestCase):
    """RFC 3414 A.3.2 (SHA-1) and RFC 7860's SHA-256 worked example."""

    ENGINE = bytes.fromhex("000000000000000000000002")

    def test_sha1_localized_key_matches_the_rfc_vector(self):
        key = snmp.password_to_key(b"maplesyrup", hashlib.sha1)
        localized = snmp.localize_key(key, self.ENGINE, hashlib.sha1)
        self.assertEqual(localized.hex(), "6695febc9288e36282235fc7151f128497b38f3f")

    def test_sha256_localized_key_matches_the_rfc_7860_vector(self):
        key = snmp.password_to_key(b"maplesyrup", hashlib.sha256)
        localized = snmp.localize_key(key, self.ENGINE, hashlib.sha256)
        self.assertEqual(
            localized.hex(),
            "8982e0e549e866db361a6b625d84cccc11162d453ee8ce3a6445c2d6776f0f8b",
        )

    def test_a_seven_character_password_is_refused(self):
        for digest in (hashlib.sha1, hashlib.sha256):
            with self.assertRaises(SnmpError) as caught:
                snmp.password_to_key(b"sevench", digest)
            self.assertEqual(
                str(caught.exception), "an SNMPv3 password must be at least 8 characters"
            )

    def test_the_digest_and_truncated_mac_length_of_each_protocol(self):
        self.assertEqual(snmp.AUTH_PROTOCOLS["sha"], (hashlib.sha1, 12))
        self.assertEqual(snmp.AUTH_PROTOCOLS["sha256"], (hashlib.sha256, 24))


# --- the fake's own BER -----------------------------------------------------


def _header_len(buf: bytes, at: int) -> int:
    """How many octets the length field starting at `at` occupies."""
    first = buf[at]
    return 1 if first < 0x80 else 1 + (first & 0x7F)


def _body_len(buf: bytes, start: int) -> int:
    first = buf[start + 1]
    if first < 0x80:
        return first
    count = first & 0x7F
    return int.from_bytes(buf[start + 2:start + 2 + count], "big")


def _element_span(buf: bytes, start: int) -> tuple[int, int]:
    """(where an element's payload begins, how long it is), read independently."""
    at = start + 1 + _header_len(buf, start + 1)
    return at, _body_len(buf, start)


def _offset_of(packet: bytes, index: int) -> int:
    """Where the n-th element of the outer SEQUENCE begins."""
    offset = 1 + _header_len(packet, 1)
    for _ in range(index):
        offset += 1 + _header_len(packet, offset + 1) + _body_len(packet, offset)
    return offset


def _octet_body(buf: bytes) -> bytes:
    """The value inside an encoded OCTET STRING."""
    at = 1 + _header_len(buf, 1)
    return bytes(buf[at:at + _body_len(buf, 0)])


def _field_tag(buf: bytes, index: int) -> int:
    """Where the n-th element of an encoded SEQUENCE starts, header included."""
    offset = 1 + _header_len(buf, 1)
    for _ in range(index):
        offset += 1 + _header_len(buf, offset + 1) + _body_len(buf, offset)
    return offset


def _zero_at(buf: bytes, start: int, length: int) -> bytes:
    return buf[:start] + bytes(length) + buf[start + length:]


def _int_bytes(value: int) -> bytes:
    length = max(1, (value.bit_length() + 8) // 8)
    return bytes([0x02]) + encode_length(length) + value.to_bytes(length, "big",
                                                                   signed=True)


def _oid_bytes(oid: str) -> bytes:
    arcs = [int(one) for one in oid.split(".")]
    body = bytearray([arcs[0] * 40 + arcs[1]])
    for arc in arcs[2:]:
        chunk = bytearray([arc & 0x7F])
        arc >>= 7
        while arc:
            chunk.insert(0, (arc & 0x7F) | 0x80)
            arc >>= 7
        body.extend(chunk)
    return bytes([0x06]) + encode_length(len(body)) + bytes(body)


def _varbind(oid: str, tag: int, value) -> bytes:
    return encode_sequence(_oid_bytes(oid), _value_bytes(tag, value))


def _security_parameters(packet: bytes):
    """(engine id, boots, time, user, auth params, priv params) of a message."""
    start = _offset_of(packet, 2)
    at = start + 1 + _header_len(packet, start + 1)
    inner = bytes(packet[at:at + _body_len(packet, start)])
    _tag, fields, _used = decode(inner, 0)
    return tuple(one[1] for one in fields)


class FakeV3Agent:
    """A v3 device in the test: bytes in, bytes out, its own HMAC and AES."""

    def __init__(self, *, user: str = USER, auth_password: str = AUTH_PASSWORD,
                 priv_password: str | None = PRIV_PASSWORD,
                 engine_id: bytes = ENGINE_ID, boots: int = 1, when: int = 700_000,
                 digest=hashlib.sha1, reports=None, bad_mac: bool = False,
                 wrong_msg_id: bool = False, silent: bool = False,
                 unauthenticated: bool = False,
                 answer_request_id=None):
        self.user_name = user.encode("utf-8")
        self.engine_id = engine_id
        self.boots = boots
        self.when = when
        self.digest = digest
        self.mac_length = snmp.AUTH_PROTOCOLS[
            "sha" if digest is hashlib.sha1 else "sha256"][1]
        self.auth_key = _localize(
            _password_to_key(auth_password.encode("utf-8"), digest), engine_id, digest)
        self.priv_key = (
            _priv_key(priv_password, engine_id, digest) if priv_password is not None else None
        )
        self.reports = list(reports or [])
        self.bad_mac = bad_mac
        self.wrong_msg_id = wrong_msg_id
        self.silent = silent
        self.unauthenticated = unauthenticated
        self.answer_request_id = answer_request_id or (lambda sent: sent)
        self.packets_in: list[bytes] = []
        self.replies_out: list[bytes] = []
        self.users_seen: list[bytes] = []

    def answer(self, packet: bytes) -> bytes | None:
        self.packets_in.append(bytes(packet))
        if self.silent and len(self.packets_in) > 1:
            return None
        _tag, elements, _used = decode(packet)
        msg_id = elements[1][1][0][1]
        flags = elements[1][1][2][1][0]
        _engine, boots, when, user, _auth, priv = _security_parameters(packet)
        self.users_seen.append(user)

        if not user:
            # Engine discovery. A probe that does carry a user would not be one;
            # the check lives here rather than at the top so the fake's shape
            # stays obvious in one place.
            assert flags & MSG_FLAG_REPORTABLE and not (flags & (MSG_FLAG_AUTH | MSG_FLAG_PRIV))
            return self._reply(msg_id, MSG_FLAG_REPORTABLE, self._report_pdu(msg_id, ()))
        if self.reports:
            return self._reply(msg_id, MSG_FLAG_AUTH | MSG_FLAG_REPORTABLE,
                               self._report_pdu(msg_id, (self.reports.pop(0),)))
        if not flags & MSG_FLAG_AUTH or not self._mac_ok(packet):
            return self._refused(msg_id, USM_WRONG_DIGESTS)
        if user != self.user_name:
            return self._refused(msg_id, USM_UNKNOWN_USER_NAMES)

        scoped = bytes(packet[_offset_of(packet, 3):])
        if flags & MSG_FLAG_PRIV:
            if self.priv_key is None or len(priv) != 8:
                return self._refused(msg_id, USM_UNSUPPORTED_SEC_LEVELS)
            plain = _aes_decrypt(self.priv_key, _octet_body(scoped),
                         boots.to_bytes(4, "big") + when.to_bytes(4, "big") + priv)
        else:
            plain = scoped
        if not self._readable(plain):
            # Our own code encrypts right, so this is reached only for a request
            # built with a privacy key we do not hold. Say so in a Report.
            return self._refused(msg_id, USM_DECRYPTION_ERRORS)

        fields = decode(plain, 0)[1]
        pdu_tag, pdu = fields[2]
        request_id = self.answer_request_id(pdu[0][1])
        rows = self._rows(pdu_tag, pdu)
        # The answer carries privacy only when the request did, and its salt is
        # then the very one the client just sent.
        reply_flags = MSG_FLAG_AUTH | (MSG_FLAG_PRIV if flags & MSG_FLAG_PRIV else 0)
        if self.unauthenticated:  # a forger without the key: no auth, in the clear
            reply_flags = 0
        priv_params = priv if reply_flags & MSG_FLAG_PRIV else b""
        body = encode_sequence(_int_bytes(request_id), _int_bytes(0), _int_bytes(0),
                               encode_sequence(rows), tag=Response)
        scoped_reply = encode_sequence(encode_octets(self.engine_id),
                                       encode_octets(b""), body)
        return self._reply(msg_id + (1 if self.wrong_msg_id else 0), reply_flags,
                           scoped_reply, priv_params, at=(boots, when))

    @staticmethod
    def _readable(scoped: bytes) -> bool:
        """A ScopedPDU as RFC 3412 draws it. Wrong-key privacy rarely decodes."""
        try:
            tag, fields, used = decode(scoped, 0)
        except SnmpError:
            return False
        if tag != SEQUENCE or not isinstance(fields, list) or len(fields) != 3:
            return False
        if used != len(scoped):
            return False
        if fields[0][0] != OCTET_STRING or fields[1][0] != OCTET_STRING:
            return False
        pdu = fields[2][1]
        if not isinstance(pdu, list) or len(pdu) != 4:
            return False
        return all(one[0] == INTEGER for one in pdu[:3])

    def _refused(self, msg_id: int, oid: str) -> bytes:
        # RFC 3414 3.2: a Report goes back in the clear, even for an authPriv user.
        return self._reply(msg_id, MSG_FLAG_AUTH | MSG_FLAG_REPORTABLE,
                           self._report_pdu(msg_id, (oid,)))

    def _rows(self, pdu_tag: int, pdu) -> bytes:
        names = [pair[1][0][1] for pair in pdu[3][1]]
        if not names:
            return b""
        if pdu_tag == GetBulkRequest:
            key = snmp.oid_key(names[0])
            after = [one for one in sorted(MIB, key=snmp.oid_key)
                     if snmp.oid_key(one) > key]
            names = after[: max(1, pdu[2][1])]
        return b"".join(
            _varbind(name, *MIB[name]) if name in MIB
            else encode_sequence(_oid_bytes(name), bytes([0x81, 0x00]))
            for name in names
        )

    @staticmethod
    def _counter64(value: int) -> bytes:
        """usmStats counters are Counter64 on the wire (tag 0x46)."""
        return bytes([0x46, 0x01, value])

    def _report_pdu(self, msg_id: int, oids) -> bytes:
        varbinds = b"".join(encode_sequence(_oid_bytes(one), self._counter64(1))
                            for one in oids)
        pdu = encode_sequence(_int_bytes(msg_id), _int_bytes(0), _int_bytes(0),
                              encode_sequence(varbinds), tag=Report)
        return encode_sequence(encode_octets(self.engine_id), encode_octets(b""), pdu)

    @staticmethod
    def _auth_at_inside(sec_payload: bytes) -> int:
        """Where the MAC's value sits inside the security parameters' payload."""
        return _element_span(sec_payload, _field_tag(sec_payload, 4))[0]

    def _reply(self, msg_id: int, flags: int, scoped_pdu: bytes,
               priv_params: bytes = b"", at: tuple[int, int] | None = None) -> bytes:
        if flags & MSG_FLAG_PRIV:
            # RFC 3826 2.3: the answer's IV is the request's boots and time with
            # the very salt that request carried.
            boots, when = at if at is not None else (self.boots, self.when)
            iv = boots.to_bytes(4, "big") + when.to_bytes(4, "big") + priv_params
            msg_data = encode_octets(_aes(self.priv_key, scoped_pdu, iv))
        else:
            msg_data = scoped_pdu
        sec = encode_sequence(
            encode_octets(self.engine_id), _int_bytes(self.boots), _int_bytes(self.when),
            encode_octets(self.user_name), encode_octets(bytes(self.mac_length)),
            encode_octets(priv_params),
        )
        message = encode_sequence(
            _int_bytes(3),
            encode_sequence(_int_bytes(msg_id), _int_bytes(snmp.MAX_MESSAGE_SIZE),
                            encode_octets(bytes([flags])),
                            _int_bytes(snmp.USM_SECURITY_MODEL)),
            encode_octets(sec), msg_data,
        )
        payload_at, _length = _element_span(message, _offset_of(message, 2))
        start = payload_at + self._auth_at_inside(bytes(message[payload_at:]))
        mac = self._hmac(_zero_at(message, start, self.mac_length))
        if self.bad_mac and len(self.packets_in) > 1:
            mac = bytes([mac[0] ^ 0xFF]) + mac[1:]
        signed = message[:start] + mac + message[start + len(mac):]
        self.replies_out.append(signed)
        return signed

    def _hmac(self, packet: bytes) -> bytes:
        return hmac_module.new(self.auth_key, packet, self.digest).digest()[:self.mac_length]

    def _mac_ok(self, packet: bytes) -> bool:
        payload_at, _length = _element_span(packet, _offset_of(packet, 2))
        start = payload_at + self._auth_at_inside(bytes(packet[payload_at:]))
        received = bytes(packet[start:start + self.mac_length])
        return hmac_module.compare_digest(
            self._hmac(_zero_at(packet, start, self.mac_length)), received)


# --- the client's socket, routed into the fake ------------------------------


class _RoutedSockets:
    def __init__(self, agent: FakeV3Agent):
        self.agent = agent
        self.reply: bytes | None = None

    def install(self, test: unittest.TestCase) -> None:
        outer = self

        class _Socket:
            def __init__(self, *_args):
                pass

            def settimeout(self, _seconds):
                pass

            def sendto(self, packet, _address):
                outer.reply = outer.agent.answer(bytes(packet))
                return len(packet)

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def recvfrom(self, _size):
                if outer.reply is None:
                    raise TimeoutError
                reply, outer.reply = outer.reply, None
                return reply, ("127.0.0.1", 161)

            def close(self):
                pass

        def fake_getaddrinfo(_host, port, *_args, **_kwargs):
            return [(2, 2, 17, "", ("127.0.0.1", port))]

        for name, value in (("getaddrinfo", fake_getaddrinfo), ("socket", _Socket)):
            patcher = mock.patch.object(snmp.socket, name, value)
            patcher.start()
            test.addCleanup(patcher.stop)


class _AgentTests(unittest.TestCase):
    """Base class: self.client(agent, **kwargs) speaks to one fake device."""

    def client(self, agent: FakeV3Agent, **kwargs) -> V3Client:
        _RoutedSockets(agent).install(self)
        kwargs.setdefault("auth_protocol", "sha")
        kwargs.setdefault("priv_password", PRIV_PASSWORD)
        return V3Client(
            "127.0.0.1", user=kwargs.pop("user", USER),
            auth_password=kwargs.pop("auth_password", AUTH_PASSWORD),
            timeout=0.3, retries=0, **kwargs)


class AuthPrivExchanges(_AgentTests):
    """Discovery, then GET and WALK over AES-128-CFB with both digests."""

    def test_discovery_then_a_get_over_sha(self):
        agent = FakeV3Agent()
        found = self.client(agent).get([SYS_NAME])
        self.assertEqual(found[SYS_NAME], (OCTET_STRING, b"switch-1"))
        self.assertEqual(len(agent.packets_in), 2)  # the probe, then the GET
        self.assertEqual(agent.users_seen[0], b"")  # a probe carries no user

    def test_discovery_then_a_get_over_sha256(self):
        agent = FakeV3Agent(digest=hashlib.sha256)
        client = self.client(agent, auth_protocol="sha256")
        self.assertEqual(client.get([SYS_NAME])[SYS_NAME], (OCTET_STRING, b"switch-1"))

    def test_the_engine_is_discovered_once_per_client(self):
        agent = FakeV3Agent()
        client = self.client(agent)
        client.get([SYS_NAME])
        client.get(["1.3.6.1.2.1.1.1.0"])
        self.assertEqual(len(agent.packets_in), 3)

    def test_authnopriv_gets_the_varbinds_too(self):
        agent = FakeV3Agent(priv_password=None)
        client = self.client(agent, priv_password=None)
        self.assertEqual(client.get([SYS_NAME])[SYS_NAME], (OCTET_STRING, b"switch-1"))

    def test_authnopriv_sends_no_priv_flag_and_no_salt(self):
        agent = FakeV3Agent(priv_password=None)
        self.client(agent, priv_password=None).get([SYS_NAME])
        request = agent.packets_in[1]
        _tag, elements, _used = decode(request)
        self.assertEqual(elements[1][1][2][1][0], MSG_FLAG_AUTH | MSG_FLAG_REPORTABLE)
        self.assertEqual(_security_parameters(request)[5], b"")

    def test_a_walk_over_getbulk_returns_every_row_in_order(self):
        agent = FakeV3Agent()
        rows = self.client(agent).walk(IF_DESCR, max_repetitions=25)
        self.assertEqual([oid for oid, _t, _v in rows],
                         [f"{IF_DESCR}.1", f"{IF_DESCR}.2", f"{IF_DESCR}.3"])

    def test_the_request_flags_its_mac_and_its_plaintext(self):
        agent = FakeV3Agent()
        self.client(agent).get([SYS_NAME])
        request = agent.packets_in[1]
        _tag, elements, _used = decode(request)
        self.assertEqual(elements[1][1][2][1][0],
                         MSG_FLAG_AUTH | MSG_FLAG_PRIV | MSG_FLAG_REPORTABLE)
        self.assertTrue(agent._mac_ok(request))
        engine, boots, when, user, _auth, priv = _security_parameters(request)
        self.assertEqual((engine, user, len(priv)), (ENGINE_ID, USER.encode(), 8))
        plain = _aes_decrypt(_priv_key(PRIV_PASSWORD, ENGINE_ID, hashlib.sha1),
                     bytes(elements[3][1]),
                     boots.to_bytes(4, "big") + when.to_bytes(4, "big") + priv)
        _pdu_tag, fields, _used2 = decode(plain, 0)
        self.assertEqual(fields[2][1][3][1][0][1][0][1], SYS_NAME)

    def test_each_message_gets_a_fresh_salt_and_msg_id(self):
        agent = FakeV3Agent()
        client = self.client(agent)
        client.get([SYS_NAME])
        client.get([SYS_NAME])
        first = _security_parameters(agent.packets_in[1])[5]
        second = _security_parameters(agent.packets_in[2])[5]
        self.assertNotEqual(first, second)
        ids = [decode(one)[1][1][1][0][1] for one in agent.packets_in[1:]]
        self.assertEqual(len(set(ids)), 2)


class ReportHandling(_AgentTests):
    """Each usmStats counter is an error with its own sentence."""

    def test_each_counter_maps_to_its_sentence(self):
        cases = [
            (USM_UNKNOWN_USER_NAMES, "SNMPv3 user is not known to the device"),
            (USM_WRONG_DIGESTS, "SNMPv3 auth password or protocol is wrong"),
            (USM_DECRYPTION_ERRORS, "SNMPv3 privacy password is wrong"),
            (USM_UNSUPPORTED_SEC_LEVELS, "the device does not allow this SNMPv3 security level"),
            ("1.3.6.1.6.3.15.1.1.9.0", "SNMPv3 report 1.3.6.1.6.3.15.1.1.9.0"),
        ]
        for oid, sentence in cases:
            with self.subTest(oid=oid):
                agent = FakeV3Agent(reports=[oid])
                client = self.client(agent)
                with self.assertRaises(SnmpError) as caught:
                    client.get([SYS_NAME])
                self.assertEqual(str(caught.exception), sentence)
                for word in SECRET_WORDS:
                    self.assertNotIn(word, str(caught.exception))

    def test_a_stale_time_window_is_rediscovered_once_and_succeeds(self):
        agent = FakeV3Agent(reports=[USM_NOT_IN_TIME_WINDOWS])
        client = self.client(agent)
        self.assertEqual(client.get([SYS_NAME])[SYS_NAME], (OCTET_STRING, b"switch-1"))
        self.assertEqual(len(agent.packets_in), 4)  # probe, GET, probe, GET

    def test_a_second_stale_time_window_gives_up(self):
        agent = FakeV3Agent(reports=[USM_NOT_IN_TIME_WINDOWS] * 6)
        client = self.client(agent)
        with self.assertRaises(SnmpError) as caught:
            client.get([SYS_NAME])
        self.assertEqual(str(caught.exception), "SNMPv3 time window could not be agreed")


class RequestIdMismatch(_AgentTests):
    """RFC 3414: an unmatched request id is not an answer, so the call times out."""

    def test_a_reply_answering_another_request_id_times_out(self):
        agent = FakeV3Agent(answer_request_id=lambda sent: sent ^ 0x5A5A)
        client = self.client(agent)
        with self.assertRaises(SnmpTimeout) as caught:
            client.get([SYS_NAME])
        self.assertEqual(str(caught.exception), "no SNMP response from 127.0.0.1:161")

    def test_a_reply_answering_another_msg_id_times_out(self):
        agent = FakeV3Agent(wrong_msg_id=True)
        client = self.client(agent)
        with self.assertRaises(SnmpTimeout) as caught:
            client.get([SYS_NAME])
        self.assertEqual(str(caught.exception), "no SNMP response from 127.0.0.1:161")


class BadResponses(_AgentTests):
    def test_an_unauthenticated_answer_is_never_taken(self):
        # Someone on the path who saw the msgID but holds no key can only stay
        # silent, as far as the client is concerned.
        for priv in (PRIV_PASSWORD, None):
            with self.subTest(priv=priv is not None):
                agent = FakeV3Agent(priv_password=priv, unauthenticated=True)
                client = self.client(agent, priv_password=priv)
                with self.assertRaises(SnmpTimeout):
                    client.get([SYS_NAME])

    def test_a_corrupted_mac_fails_authentication(self):
        agent = FakeV3Agent(bad_mac=True)
        client = self.client(agent)
        with self.assertRaises(SnmpError) as caught:
            client.get([SYS_NAME])
        self.assertEqual(str(caught.exception), "SNMPv3 response failed authentication")

    def test_silence_is_a_timeout_that_never_names_a_secret(self):
        agent = FakeV3Agent(silent=True)
        client = self.client(agent)
        with self.assertRaises(SnmpTimeout) as caught:
            client.get([SYS_NAME])
        for word in SECRET_WORDS:
            self.assertNotIn(word, str(caught.exception))


class RefusedParameters(_AgentTests):
    """Bad parameters are refused before a single packet is built."""

    def test_md5_and_an_unknown_protocol_are_refused(self):
        agent = FakeV3Agent()
        for protocol in ("md5", "sha512", None, 7):
            with self.subTest(protocol=protocol):
                with self.assertRaises(SnmpError) as caught:
                    self.client(agent, auth_protocol=protocol)
                self.assertEqual(
                    str(caught.exception),
                    "SNMPv3 auth must be sha or sha256 (MD5 is not supported)")
        self.assertEqual(agent.packets_in, [])

    def test_an_empty_or_oversized_user_is_refused(self):
        agent = FakeV3Agent()
        with self.assertRaises(SnmpError) as caught:
            self.client(agent, user="")
        self.assertEqual(str(caught.exception), "no SNMPv3 user")
        with self.assertRaises(SnmpError) as caught:
            self.client(agent, user="u" * 33)
        self.assertIn("at most 32", str(caught.exception))
        self.assertEqual(agent.packets_in, [])

    def test_a_short_password_is_refused_without_naming_itself(self):
        agent = FakeV3Agent()
        with self.assertRaises(SnmpError) as caught:
            self.client(agent, auth_password="sevench")
        self.assertEqual(
            str(caught.exception), "an SNMPv3 password must be at least 8 characters")
        with self.assertRaises(SnmpError) as caught:
            self.client(agent, priv_password="shorter")
        self.assertIn("at least 8", str(caught.exception))
        for word in ("sevench", "shorter"):
            self.assertNotIn(word, str(caught.exception))
        self.assertEqual(agent.packets_in, [])


class MissingCryptography(_AgentTests):
    """An agent installed from source without the package still does authNoPriv."""

    def setUp(self):
        real_import = builtins.__import__

        # snmp.py imports the cipher lazily inside one function; blocking that
        # one import is what an agent installed from source without it looks like.
        def refusing(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("cryptography"):
                raise ImportError(name)
            return real_import(name, globals, locals, fromlist, level)

        patcher = mock.patch.object(builtins, "__import__", refusing)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_authpriv_says_what_is_missing(self):
        client = self.client(FakeV3Agent())
        with self.assertRaises(SnmpError) as caught:
            client.get([SYS_NAME])
        self.assertEqual(
            str(caught.exception),
            "SNMPv3 privacy needs the cryptography package on this agent")

    def test_authnopriv_still_works(self):
        agent = FakeV3Agent(priv_password=None)
        client = self.client(agent, priv_password=None)
        self.assertEqual(client.get([SYS_NAME])[SYS_NAME], (OCTET_STRING, b"switch-1"))


class SnmpCheckVersionSelection(unittest.TestCase):
    """The snmp check picks its client from the params; everything else is shared."""

    def tearDown(self):
        mock.patch.stopall()

    def _record(self, name: str, values=None) -> dict:
        seen: dict = {}
        answers = values or {SYSNAME_OID: (OCTET_STRING, b"switch-1"),
                             SYSUPTIME_OID: (TimeTicks, 60_000)}

        class _Client:
            def __init__(self, host, **kwargs):
                seen["host"] = host
                seen.update(kwargs)

            def get(self, oids):
                return {oid: answers[oid] for oid in oids if oid in answers}

        patcher = mock.patch.object(snmp, name, _Client)
        patcher.start()
        return seen

    def test_version_3_builds_a_v3_client_from_the_params(self):
        seen = self._record("V3Client")
        out = run_check({"kind": "snmp", "host": "router.lan", "version": 3, "user": USER,
                         "auth": "sha256", "auth_password": AUTH_PASSWORD,
                         "priv_password": PRIV_PASSWORD}, timeout=1.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen, {"host": "router.lan", "user": USER,
                                "auth_protocol": "sha256", "auth_password": AUTH_PASSWORD,
                                "priv_password": PRIV_PASSWORD, "port": 161,
                                "timeout": 1.0, "retries": 1})

    def test_no_version_still_builds_a_v2c_client(self):
        seen = self._record("V2cClient")
        out = run_check({"kind": "snmp", "host": "router.lan", "community": "public"},
                        timeout=1.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen["community"], "public")

    def test_a_v3_check_reports_the_same_fields_as_a_v2c_one(self):
        self._record("V3Client", {SYSNAME_OID: (OCTET_STRING, b"fw-1"),
                                  SYSUPTIME_OID: (TimeTicks, 60_000)})
        out = run_check({"kind": "snmp", "host": "router.lan", "version": 3, "user": USER,
                         "auth": "sha", "auth_password": AUTH_PASSWORD}, timeout=1.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["sys_name"], "fw-1")
        self.assertEqual(out["uptime_s"], 600)  # TimeTicks are hundredths

    def test_missing_or_bad_v3_fields_name_themselves_plainly(self):
        base = {"kind": "snmp", "host": "router.lan", "version": 3}
        cases = [
            ({**base, "auth": "sha", "auth_password": AUTH_PASSWORD}, "no SNMPv3 user"),
            ({**base, "user": USER, "auth": "sha"}, "no SNMPv3 auth password"),
            ({**base, "user": USER, "auth_password": AUTH_PASSWORD},
             "SNMPv3 auth must be sha or sha256"),
            ({**base, "user": USER, "auth": "md5", "auth_password": AUTH_PASSWORD},
             "SNMPv3 auth must be sha or sha256"),
        ]
        for check, detail in cases:
            with self.subTest(detail=detail):
                out = run_check(check, timeout=0.5)
                self.assertFalse(out["ok"], out)
                self.assertEqual(out["detail"], detail)

    def test_a_v3_check_never_leaks_a_password_into_its_result(self):
        out = run_check({"kind": "snmp", "host": "router.lan", "version": 3, "user": USER,
                         "auth": "sha"}, timeout=0.5)
        self.assertFalse(out["ok"])
        for word in SECRET_WORDS:
            self.assertNotIn(word, str(out))


if __name__ == "__main__":
    unittest.main()
