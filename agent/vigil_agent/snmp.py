"""SNMP v2c and v3 clients: BER, message encoding, GET and WALK over UDP.

v2c is standard library only. v3 (USM, RFC 3414) authenticates with HMAC-SHA-1
or HMAC-SHA-256 (RFC 7860) from the standard library and encrypts with
AES-128-CFB (RFC 3826) from the ``cryptography`` package, imported only when a
check asks for privacy. Nothing here raises anything but SnmpError: a malformed
packet, an IndexError or a UnicodeDecodeError inside decoding all become
SnmpError. No password or key ever appears in an exception message.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import socket
import time
from dataclasses import dataclass

INTEGER = 0x02  # signed two's complement, minimal length
OCTET_STRING = 0x04  # a byte string (community names, ifDescr, ...)
NULL = 0x05  # the empty value carried by a request varbind
OBJECT_IDENTIFIER = 0x06  # a dotted name, base-128 arcs
SEQUENCE = 0x30  # a message and its varbind list
IpAddress = 0x40  # exactly four octets, a dotted quad
Counter32 = 0x41  # unsigned, wraps at 2**32
Gauge32 = 0x42  # unsigned, a current value
TimeTicks = 0x43  # unsigned, hundredths of a second
Opaque = 0x44  # uninterpreted bytes
Counter64 = 0x46  # unsigned, wraps at 2**64
noSuchObject = 0x80  # an exception: this OID does not exist here
noSuchInstance = 0x81  # an exception: no instance of this object
endOfMibView = 0x82  # an exception: the walk ran off the end of the MIB
GetRequest = 0xA0
GetNextRequest = 0xA1
Response = 0xA2  # the PDU tag an agent answers with (the dataclass is ResponseMessage)
GetBulkRequest = 0xA5
Report = 0xA8  # a v3 agent's answer when it refuses a message (RFC 3414 3.2)

# SNMPv3 message flags and constants (RFC 3412 6).
MSG_FLAG_AUTH = 0x01
MSG_FLAG_PRIV = 0x02
MSG_FLAG_REPORTABLE = 0x04
USM_SECURITY_MODEL = 3
MAX_MESSAGE_SIZE = 65507

MAX_DECODE_DEPTH = 16
# Nesting stops being data past this point. A well-formed message nests SEQUENCE
# → PDU → varbind list → varbind → leaf: five levels. The limit is also what keeps
# a hostile length field from pushing the decoder's C stack past its limit, where
# CPython dies instead of raising. Lengths are refused before their bytes become a
# number, so a small packet cannot cause a multi-second big-integer shift either.
# An empty body is legal only for these: a NULL, an empty OCTET STRING or Opaque,
# and the three exception values. Every other zero-length element is malformed.
_EMPTY_OK_TAGS = (
    NULL,
    OCTET_STRING,
    Opaque,
    noSuchObject,
    noSuchInstance,
    endOfMibView,
)
# A PDU may legitimately carry an empty varbind list; nothing else nests empty.
_PDU_TAGS = (GetRequest, GetNextRequest, Response, GetBulkRequest, Report)
_EMPTY_SEQUENCE_TAGS = (SEQUENCE,) + _PDU_TAGS
MAX_LENGTH_BYTES = 4  # a longer length field cannot describe anything we accept
RECV_BUFFER = 65535  # the largest datagram an SNMP agent may send back
MAX_GET_OIDS = 32  # one GetRequest never carries more varbinds than this
_ARC_RE = re.compile(r"[0-9]+")  # not str.isdigit: that accepts "٣" and "²"

_ERROR_STATUS_NAMES = {
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    7: "wrongType",
    8: "wrongLength",
    9: "wrongEncoding",
    10: "wrongValue",
    11: "noCreation",
    12: "inconsistentValue",
    13: "resourceUnavailable",
    14: "commitFailed",
    15: "undoFailed",
    16: "authorizationError",
    17: "notWritable",
    18: "inconsistentName",
}


class SnmpError(Exception):
    """Any SNMP failure. str(e) is a short sentence for a check result's detail."""


class SnmpTimeout(SnmpError):
    """No matching response within the timeout, after every retry."""


def _malformed(what: str) -> SnmpError:
    return SnmpError(f"malformed SNMP response: {what}")


# --- BER encoding ---------------------------------------------------------


def encode_length(n: int) -> bytes:
    if n < 0:
        raise SnmpError("cannot encode a negative length")
    if n < 128:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def encode_integer(v: int) -> bytes:
    length = max(1, (v.bit_length() + 8) // 8)
    try:
        body = v.to_bytes(length, "big", signed=True)
    except OverflowError:
        raise SnmpError(f"integer {v} does not fit a BER INTEGER") from None
    return bytes([INTEGER]) + encode_length(len(body)) + body


def encode_octets(b: bytes) -> bytes:
    return bytes([OCTET_STRING]) + encode_length(len(b)) + b


def encode_null() -> bytes:
    return bytes([NULL, 0x00])


def encode_sequence(*parts: bytes, tag: int = SEQUENCE) -> bytes:
    body = b"".join(parts)
    return bytes([tag]) + encode_length(len(body)) + body


def encode_oid(oid: str) -> bytes:
    """Dotted string to BER. The first two arcs share one byte, the rest are base-128."""
    arcs = _oid_arcs(oid)
    first, second = arcs[0], arcs[1]
    if first > 2:
        raise SnmpError(f"invalid SNMP OID {oid!r}: the first arc must be 0, 1 or 2")
    if first < 2 and second > 39:
        raise SnmpError(f"invalid SNMP OID {oid!r}: the second arc must be at most 39")
    body = bytearray(_base128(40 * first + second))
    for arc in arcs[2:]:
        body.extend(_base128(arc))
    return bytes([OBJECT_IDENTIFIER]) + encode_length(len(body)) + bytes(body)


def _base128(arc: int) -> bytes:
    out = bytearray([(arc & 0x7F)])
    arc >>= 7
    while arc:
        out.append((arc & 0x7F) | 0x80)
        arc >>= 7
    out.reverse()
    return bytes(out)


def _oid_arcs(oid: str) -> list[int]:
    parts = oid.split(".")
    if len(parts) < 2:
        raise SnmpError(f"invalid SNMP OID {oid!r}: at least two arcs are required")
    arcs = []
    for part in parts:
        if not _ARC_RE.fullmatch(part):
            raise SnmpError(f"invalid SNMP OID {oid!r}: {part!r} is not an arc number")
        arcs.append(int(part))
    return arcs


def oid_key(oid: str) -> tuple[int, ...]:
    """The arcs as integers, for ordering and subtree comparisons."""
    return tuple(_oid_arcs(oid))


# --- BER decoding ---------------------------------------------------------


def decode(buf: bytes, pos: int = 0) -> tuple[int, object, int]:
    try:
        tag, length, start = _decode_tlv_header(buf, pos, 0)
        if start + length > len(buf):
            raise _malformed("a length runs past the end of the packet")
        value = _decode_value(buf, tag, start, length, 0)
        return tag, value, start + length
    except IndexError:
        raise _malformed("the packet ended in the middle of a field") from None
    except ValueError:
        raise _malformed("a field did not decode as a number") from None
    except UnicodeDecodeError:
        raise _malformed("a string is not valid UTF-8") from None


def _decode_tlv_header(buf: bytes, pos: int, depth: int) -> tuple[int, int, int]:
    if depth >= MAX_DECODE_DEPTH:
        raise _malformed(f"nesting is deeper than {MAX_DECODE_DEPTH}")
    if pos >= len(buf):
        raise _malformed("the packet ended before a tag")
    tag = buf[pos]
    if pos + 1 >= len(buf):
        raise _malformed("the packet ended before a length")
    first = buf[pos + 1]
    if first == 0x80:
        raise _malformed("indefinite lengths are not used in SNMP")
    if first < 0x80:
        if first == 0 and tag not in _EMPTY_OK_TAGS + _EMPTY_SEQUENCE_TAGS:
            raise _malformed("an element has no type or length")
        return tag, first, pos + 2
    count = first & 0x7F
    if count > MAX_LENGTH_BYTES:
        raise _malformed(f"a length of {count} bytes is absurd")
    if pos + 2 + count > len(buf):
        raise _malformed("the packet ended inside a length")
    # At most MAX_LENGTH_BYTES octets, so the number is small and cheap to make;
    # it is checked against the buffer before anything uses it.
    length = int.from_bytes(buf[pos + 2 : pos + 2 + count], "big")
    if length > len(buf):
        raise _malformed("a length runs past the end of the packet")
    if length == 0 and tag not in _EMPTY_OK_TAGS + _EMPTY_SEQUENCE_TAGS:
        raise _malformed("an element has no type or length")
    start = pos + 2 + count
    if start + length > len(buf):
        raise _malformed("a length runs past the end of the packet")
    return tag, length, start


def _decode_tlv(buf: bytes, pos: int, depth: int) -> tuple[int, object, int]:
    tag, length, start = _decode_tlv_header(buf, pos, depth)
    if start + length > len(buf):
        raise _malformed("a length runs past the end of the packet")
    value = _decode_value(buf, tag, start, length, depth)
    return tag, value, start + length


def _decode_children(
    buf: bytes, start: int, end: int, depth: int
) -> list[tuple[int, object]]:
    children = []
    pos = start
    while pos < end:
        tag, value, next_pos = _decode_tlv(buf, pos, depth + 1)
        if next_pos <= pos:
            raise _malformed("a child element did not advance")
        children.append((tag, value))
        pos = next_pos
    return children


def _decode_value(buf: bytes, tag: int, start: int, length: int, depth: int) -> object:
    end = start + length
    body = buf[start:end]
    if tag == INTEGER:
        if length == 0:
            raise _malformed("an INTEGER has no bytes")
        return int.from_bytes(body, "big", signed=True)
    if tag in (OCTET_STRING, Opaque):
        return body
    if tag == NULL:
        if length != 0:
            raise _malformed("a NULL carries bytes")
        return None
    if tag == OBJECT_IDENTIFIER:
        return _decode_oid(body)
    if tag in _EMPTY_SEQUENCE_TAGS:
        return _decode_children(buf, start, end, depth)
    if tag == IpAddress:
        if length != 4:
            raise _malformed(f"an IpAddress is 4 bytes, not {length}")
        return ".".join(str(byte) for byte in body)
    if tag in (Counter32, Gauge32, TimeTicks, Counter64):
        if length == 0:
            raise _malformed("a counter has no bytes")
        return int.from_bytes(body, "big", signed=True) & ((1 << (8 * length)) - 1)
    if tag in (noSuchObject, noSuchInstance, endOfMibView):
        return None
    return body


def _decode_oid(body: bytes) -> str:
    if not body:
        raise _malformed("an OBJECT IDENTIFIER has no arcs")
    arcs: list[int] = []
    index = 0
    while index < len(body):
        value = 0
        while True:
            if index >= len(body):
                raise _malformed("the last OID arc is not finished")
            byte = body[index]
            index += 1
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        if not arcs:  # the first subidentifier holds two arcs: 40*a+b, a at most 2
            first = min(value // 40, 2)
            arcs.extend((first, value - 40 * first))
        else:
            arcs.append(value)
    return ".".join(str(arc) for arc in arcs)


# --- messages -------------------------------------------------------------


@dataclass
class ResponseMessage:
    """One decoded response message."""

    version: int
    community: bytes
    request_id: int
    error_status: int
    error_index: int
    varbinds: list[tuple[str, int, object]]


def build_request(
    pdu_tag: int,
    request_id: int,
    community: bytes,
    varbinds: list[str],
    *,
    non_repeaters: int = 0,
    max_repetitions: int = 0,
) -> bytes:
    """A v2c message. GetBulk carries non-repeaters and max-repetitions; the rest carry 0."""
    pdu = _build_pdu(
        pdu_tag,
        request_id,
        varbinds,
        non_repeaters=non_repeaters,
        max_repetitions=max_repetitions,
    )
    return encode_sequence(encode_integer(1), encode_octets(community), pdu)


def _build_pdu(
    pdu_tag: int,
    request_id: int,
    varbinds: list[str],
    *,
    non_repeaters: int = 0,
    max_repetitions: int = 0,
) -> bytes:
    second = non_repeaters if pdu_tag == GetBulkRequest else 0
    third = max_repetitions if pdu_tag == GetBulkRequest else 0
    pairs = b"".join(
        encode_sequence(encode_oid(oid), encode_null()) for oid in varbinds
    )
    return encode_sequence(
        encode_integer(request_id),
        encode_integer(second),
        encode_integer(third),
        encode_sequence(pairs),
        tag=pdu_tag,
    )


def parse_response(packet: bytes) -> ResponseMessage:
    tag, message, _ = decode(packet, 0)
    if tag != SEQUENCE or not isinstance(message, list) or len(message) != 3:
        raise _malformed("the packet is not a sequence of three elements")
    version_tag, version = message[0]
    if version_tag != INTEGER or not isinstance(version, int):
        raise _malformed("the first element is not the version")
    if version != 1:
        raise SnmpError(f"unsupported SNMP version {version}")
    community_tag, community = message[1]
    if community_tag != OCTET_STRING or not isinstance(community, bytes):
        raise _malformed("the second element is not the community")
    pdu_tag, pdu = message[2]
    request_id, error_status, error_index, rows = _parse_pdu(pdu_tag, pdu)
    return ResponseMessage(
        version=version,
        community=community,
        request_id=request_id,
        error_status=error_status,
        error_index=error_index,
        varbinds=rows,
    )


def _parse_pdu(
    pdu_tag: int, pdu: object, *, allowed: tuple[int, ...] = (Response,)
) -> tuple[int, int, int, list[tuple[str, int, object]]]:
    """A decoded PDU's request-id, error status, error index and varbinds."""
    if pdu_tag not in allowed:
        raise SnmpError(f"expected a response PDU, got tag 0x{pdu_tag:02x}")
    if not isinstance(pdu, list) or len(pdu) != 4:
        raise _malformed("the response PDU does not have four fields")
    id_tag, request_id = pdu[0]
    status_tag, error_status = pdu[1]
    index_tag, error_index = pdu[2]
    list_tag, varbinds = pdu[3]
    if id_tag != INTEGER or status_tag != INTEGER or index_tag != INTEGER:
        raise _malformed("the response PDU header is not three integers")
    if list_tag != SEQUENCE or not isinstance(varbinds, list):
        raise _malformed("the varbind list is not a sequence")
    rows: list[tuple[str, int, object]] = []
    for _name_tag, pair in varbinds:  # each varbind is a SEQUENCE of OID and value
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or pair[0][0] != OBJECT_IDENTIFIER
            or not isinstance(pair[0][1], str)
        ):
            raise _malformed("a varbind is not an OID and a value")
        rows.append((pair[0][1], pair[1][0], pair[1][1]))
    return request_id, error_status, error_index, rows


# --- the client -----------------------------------------------------------


class _Client:
    """GET and WALK over one ``_exchange`` per round; v2c and v3 differ only there."""

    host: str
    port: int
    timeout: float
    retries: int

    def get(self, oids: list[str]) -> dict[str, tuple[int, object]]:
        """OID -> (tag, value). noSuchObject and noSuchInstance are values, not errors."""
        if not oids:
            raise SnmpError("an SNMP GET needs at least one OID")
        if len(oids) > MAX_GET_OIDS:
            raise SnmpError(f"an SNMP GET carries at most {MAX_GET_OIDS} OIDs")
        response = self._exchange(GetRequest, list(oids))
        return {oid: (tag, value) for oid, tag, value in response.varbinds}

    def walk(
        self, root: str, *, max_rows: int = 10000, max_repetitions: int = 25
    ) -> list[tuple[str, int, object]]:
        """Every row under root, in OID order. Stops outside the subtree or at endOfMibView."""
        wanted = oid_key(root)
        rows: list[tuple[str, int, object]] = []
        current = root
        while len(rows) < max_rows:
            batch = self._bulk_round(current, max_repetitions)
            if not batch:
                raise SnmpError("SNMP agent returned no rows for a walk")
            for oid, tag, value in batch:
                # endOfMibView first: an agent may echo the requested OID with it.
                if tag == endOfMibView or not _inside(oid_key(oid), wanted):
                    return rows
                if oid_key(oid) <= oid_key(current):
                    raise SnmpError("SNMP agent returned OIDs out of order")
                rows.append((oid, tag, value))
                current = oid
                if len(rows) >= max_rows:
                    return rows
        return rows

    def _bulk_round(
        self, current: str, max_repetitions: int
    ) -> list[tuple[str, int, object]]:
        """One GetBulk round, in order. A broken agent that loops is the caller's check."""
        response = self._exchange(
            GetBulkRequest, [current], max_repetitions=max_repetitions
        )
        return [(oid, tag, value) for oid, tag, value in response.varbinds]

    def _exchange(
        self, pdu_tag: int, oids: list[str], *, max_repetitions: int = 0
    ) -> ResponseMessage:
        raise NotImplementedError

    def _address(self) -> tuple[int, int, int, tuple]:
        try:
            infos = socket.getaddrinfo(
                self.host, self.port, socket.AF_UNSPEC, socket.SOCK_DGRAM
            )
        except socket.gaierror as exc:
            raise SnmpError(f"cannot resolve {self.host}: {exc}") from None
        if not infos:
            raise SnmpError(f"no address for {self.host}")
        family, socktype, proto, _canonical, address = infos[0]
        return family, socktype, proto, address

    def _send_with_retries(self, target, build, accept):
        """Send ``build()`` (a new request each attempt) until ``accept`` takes a reply.

        ``build`` returns (request bytes, state); ``accept(data, state)`` returns the
        answer, or None for a datagram that is not ours. SnmpTimeout on silence."""
        for _attempt in range(self.retries + 1):
            request, state = build()
            try:
                answer = self._round_trip(target, request, lambda data: accept(data, state))
            except OSError as exc:
                raise SnmpError(
                    f"cannot send SNMP to {self.host}:{self.port}: {exc}"
                ) from None
            if answer is not None:
                return answer
        raise SnmpTimeout(f"no SNMP response from {self.host}:{self.port}")

    def _round_trip(self, target, request: bytes, accept):
        """One socket's life: send, then read until the deadline for our own reply."""
        family, socktype, proto, address = target
        try:
            sock = socket.socket(family, socktype, proto)
        except OSError as exc:
            raise SnmpError(
                f"cannot open a socket to {self.host}:{self.port}: {exc}"
            ) from None
        try:
            sock.settimeout(self.timeout)
            sent = sock.sendto(request, address)
            if sent != len(request):
                raise SnmpError(
                    f"only {sent} of {len(request)} SNMP request bytes reached {self.host}:{self.port}"
                )
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                try:
                    data, peer = sock.recvfrom(RECV_BUFFER)
                except TimeoutError:
                    return None
                if peer[0] != address[0]:
                    continue  # a reply from another host is not our answer
                if len(peer) > 1 and peer[1] != address[1]:
                    continue  # nor is one from another port
                answer = accept(data)
                if answer is not None:
                    return answer
        finally:
            sock.close()


class V2cClient(_Client):
    """One UDP exchange per call. Every failure is a SnmpError."""

    def __init__(
        self,
        host: str,
        *,
        community: str,
        port: int = 161,
        timeout: float = 2.0,
        retries: int = 1,
    ) -> None:
        if not host:
            raise SnmpError("no SNMP host given")
        self.host = host
        self.community = community.encode("utf-8")
        self.port = port
        self.timeout = timeout
        self.retries = max(0, int(retries))

    def _exchange(
        self, pdu_tag: int, oids: list[str], *, max_repetitions: int = 0
    ) -> ResponseMessage:
        target = self._address()

        def build():
            request_id = secrets.randbelow(2**31 - 1) + 1
            request = build_request(
                pdu_tag, request_id, self.community, oids, max_repetitions=max_repetitions
            )
            return request, request_id

        def accept(data: bytes, request_id: int) -> ResponseMessage | None:
            try:
                response = parse_response(data)
            except SnmpError:
                return None
            if response.request_id != request_id or response.community != self.community:
                return None
            _raise_error_status(response)
            return response

        return self._send_with_retries(target, build, accept)


def _raise_error_status(response: ResponseMessage) -> None:
    if response.error_status == 0:
        return
    name = _ERROR_STATUS_NAMES.get(response.error_status)
    if name is None:
        name = f"error status {response.error_status}"
    detail = f"SNMP agent reported {name}"
    index = response.error_index
    if 1 <= index <= len(response.varbinds):
        detail += f" for {response.varbinds[index - 1][0]}"
    raise SnmpError(detail)


def _inside(key: tuple[int, ...], root: tuple[int, ...]) -> bool:
    """Is this OID inside the subtree? Arcs are compared as integers, not strings."""
    return len(key) >= len(root) and key[: len(root)] == root


# --- SNMPv3 (USM) ---------------------------------------------------------

#: protocol name -> (digest, truncated MAC length): HMAC-SHA-96 (RFC 3414) and
#: usmHMAC192SHA256AuthProtocol (RFC 7860). MD5 is not offered.
AUTH_PROTOCOLS = {"sha": (hashlib.sha1, 12), "sha256": (hashlib.sha256, 24)}
_KEY_SOURCE_BYTES = 1_048_576  # RFC 3414 A.2: one megabyte of repeated password
_MIN_PASSWORD = 8
_MAX_USER_BYTES = 32

_REPORT_NOT_IN_TIME_WINDOW = "1.3.6.1.6.3.15.1.1.2.0"
_REPORT_UNKNOWN_ENGINE_ID = "1.3.6.1.6.3.15.1.1.4.0"
_REPORT_SENTENCES = {
    "1.3.6.1.6.3.15.1.1.1.0": "the device does not allow this SNMPv3 security level",
    "1.3.6.1.6.3.15.1.1.3.0": "SNMPv3 user is not known to the device",
    "1.3.6.1.6.3.15.1.1.5.0": "SNMPv3 auth password or protocol is wrong",
    "1.3.6.1.6.3.15.1.1.6.0": "SNMPv3 privacy password is wrong",
}


def password_to_key(password: bytes, digest) -> bytes:
    """RFC 3414 A.2: hash one megabyte of the password repeated, 64 bytes at a time."""
    if len(password) < _MIN_PASSWORD:
        raise SnmpError("an SNMPv3 password must be at least 8 characters")
    hasher = digest()
    # 64 bytes of the repeated password from each offset; the pattern repeats
    # every len(password) bytes, so a ring of length len*64 serves every chunk.
    ring = password * (64 // len(password) + 2)
    offset = 0
    for _ in range(_KEY_SOURCE_BYTES // 64):
        start = offset % len(password)
        hasher.update(ring[start : start + 64])
        offset += 64
    return hasher.digest()


def localize_key(key: bytes, engine_id: bytes, digest) -> bytes:
    """RFC 3414 2.6: the user's key for one engine, H(key || engine id || key)."""
    return digest(key + engine_id + key).digest()


def _aes_cfb(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
    """AES-128 in full-block CFB (RFC 3826 3.1.3)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:  # an agent installed from source without it
        raise SnmpError("SNMPv3 privacy needs the cryptography package on this agent") from None
    # RFC 3826 fixes AES-CFB for SNMPv3, and privacy only runs as authPriv: the whole
    # message carries an HMAC that _accept checks before anything is decrypted.
    # nosemgrep: crypto-mode-without-authentication -- see above
    cipher = Cipher(algorithms.AES(key), modes.CFB(iv))
    worker = cipher.encryptor() if encrypt else cipher.decryptor()
    return worker.update(data) + worker.finalize()


def _usm_parameters(
    engine_id: bytes, boots: int, engine_time: int, user: bytes, auth: bytes, priv: bytes
) -> bytes:
    return encode_sequence(
        encode_octets(engine_id),
        encode_integer(boots),
        encode_integer(engine_time),
        encode_octets(user),
        encode_octets(auth),
        encode_octets(priv),
    )


def _v3_message(msg_id: int, flags: int, security: bytes, data: bytes) -> bytes:
    header = encode_sequence(
        encode_integer(msg_id),
        encode_integer(MAX_MESSAGE_SIZE),
        encode_octets(bytes([flags])),
        encode_integer(USM_SECURITY_MODEL),
    )
    return encode_sequence(encode_integer(3), header, encode_octets(security), data)


def _child_spans(buf: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    """(tag, element start, body start, body end) of each element in buf[start:end]."""
    spans = []
    pos = start
    while pos < end:
        tag, length, body = _decode_tlv_header(buf, pos, 0)
        if body + length > end:
            raise _malformed("a length runs past the end of its sequence")
        spans.append((tag, pos, body, body + length))
        pos = body + length
    return spans


@dataclass
class _V3Message:
    msg_id: int
    flags: int
    engine_id: bytes
    boots: int
    engine_time: int
    user: bytes
    auth_params: bytes
    auth_at: int  # where auth_params' bytes start in the packet
    priv_params: bytes
    data_tag: int
    data: bytes  # the raw ScopedPDU, or the ciphertext when PRIV is set


def _parse_v3(packet: bytes) -> _V3Message:
    """The envelope of a v3 message, its scoped PDU still raw (or encrypted)."""
    try:
        tag, length, body = _decode_tlv_header(packet, 0, 0)
        if tag != SEQUENCE or body + length != len(packet):
            raise _malformed("the packet is not one sequence")
        parts = _child_spans(packet, body, body + length)
        if len(parts) != 4:
            raise _malformed("a v3 message has four elements")
        version_tag, version, _ = decode(packet[parts[0][1] : parts[0][3]])
        if version_tag != INTEGER or version != 3:
            raise SnmpError("not an SNMPv3 message")
        header_tag, header, _ = decode(packet[parts[1][1] : parts[1][3]])
        if (
            header_tag != SEQUENCE
            or len(header) != 4
            or [t for t, _v in header] != [INTEGER, INTEGER, OCTET_STRING, INTEGER]
            or len(header[2][1]) != 1
        ):
            raise _malformed("the v3 header is not msgID, size, flags, model")
        if header[3][1] != USM_SECURITY_MODEL:
            raise SnmpError("the SNMPv3 security model is not USM")
        sec_tag, _sec_at, sec_body, sec_end = parts[2]
        if sec_tag != OCTET_STRING:
            raise _malformed("the security parameters are not an octet string")
        usm_tag, usm_len, usm_body = _decode_tlv_header(packet, sec_body, 0)
        if usm_tag != SEQUENCE or usm_body + usm_len != sec_end:
            raise _malformed("the security parameters are not one sequence")
        fields = _child_spans(packet, usm_body, sec_end)
        expected = [OCTET_STRING, INTEGER, INTEGER, OCTET_STRING, OCTET_STRING, OCTET_STRING]
        if [f[0] for f in fields] != expected:
            raise _malformed("the USM parameters have the wrong shape")
        values = [decode(packet[f[1] : f[3]])[1] for f in fields]
        data_tag, data_at, data_body, data_end = parts[3]
        if data_tag == SEQUENCE:
            data = packet[data_at:data_end]
        elif data_tag == OCTET_STRING:
            data = packet[data_body:data_end]
        else:
            raise _malformed("the message data is neither a scoped PDU nor ciphertext")
    except IndexError:
        raise _malformed("the packet ended in the middle of a field") from None
    return _V3Message(
        msg_id=header[0][1],
        flags=header[2][1][0],
        engine_id=values[0],
        boots=values[1],
        engine_time=values[2],
        user=values[3],
        auth_params=values[4],
        auth_at=fields[4][2],
        priv_params=values[5],
        data_tag=data_tag,
        data=data,
    )


def _parse_scoped_pdu(raw: bytes) -> tuple[int, int, int, int, list[tuple[str, int, object]]]:
    """(PDU tag, request-id, error status, error index, varbinds) of a ScopedPDU."""
    tag, scoped, _ = decode(raw)
    if tag != SEQUENCE or not isinstance(scoped, list) or len(scoped) != 3:
        raise _malformed("a scoped PDU is engine id, context name and a PDU")
    pdu_tag, pdu = scoped[2]
    request_id, status, index, rows = _parse_pdu(pdu_tag, pdu, allowed=(Response, Report))
    return pdu_tag, request_id, status, index, rows


class _ReportReceived(Exception):
    """A Report PDU answered the request: the agent refused it."""

    def __init__(self, oid: str) -> None:
        super().__init__(oid)
        self.oid = oid


class V3Client(_Client):
    """SNMPv3 with USM: authNoPriv (no ``priv_password``) or authPriv (AES-128).

    The engine is discovered on the first call and kept for the client's life."""

    def __init__(
        self,
        host: str,
        *,
        user: str,
        auth_protocol: str,
        auth_password: str,
        priv_password: str | None = None,
        port: int = 161,
        timeout: float = 2.0,
        retries: int = 1,
        context: str = "",
    ) -> None:
        if not host:
            raise SnmpError("no SNMP host given")
        if auth_protocol not in AUTH_PROTOCOLS:
            raise SnmpError("SNMPv3 auth must be sha or sha256 (MD5 is not supported)")
        user_bytes = (user or "").encode("utf-8")
        if not user_bytes:
            raise SnmpError("no SNMPv3 user")
        if len(user_bytes) > _MAX_USER_BYTES:
            raise SnmpError("an SNMPv3 user is at most 32 bytes")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.user = user_bytes
        self.context = context.encode("utf-8")
        self._digest, self._mac_length = AUTH_PROTOCOLS[auth_protocol]
        # The password-derived keys are expensive (a megabyte hashed each) and
        # engine-independent: made once here, localized after discovery.
        self._auth_master = password_to_key(auth_password.encode("utf-8"), self._digest)
        self._priv_master = (
            None
            if priv_password is None
            else password_to_key(priv_password.encode("utf-8"), self._digest)
        )
        self._engine_id: bytes | None = None
        self._boots = 0
        self._time = 0
        self._time_read_at = 0.0
        self._auth_key = b""
        self._priv_key = b""
        self._salt = secrets.randbits(64)

    # -- the engine --------------------------------------------------------

    def _discover(self, target) -> None:
        """RFC 3414 4: an empty, unauthenticated request; the Report names the engine."""

        def build():
            msg_id = secrets.randbelow(2**31 - 1) + 1
            scoped = encode_sequence(
                encode_octets(b""),
                encode_octets(b""),
                _build_pdu(GetRequest, secrets.randbelow(2**31 - 1) + 1, []),
            )
            security = _usm_parameters(b"", 0, 0, b"", b"", b"")
            return _v3_message(msg_id, MSG_FLAG_REPORTABLE, security, scoped), msg_id

        def accept(data: bytes, msg_id: int):
            try:
                message = _parse_v3(data)
            except SnmpError:
                return None
            if message.msg_id != msg_id or not message.engine_id:
                return None
            return message

        message = self._send_with_retries(target, build, accept)
        self._learn_engine(message.engine_id, message.boots, message.engine_time)

    def _learn_engine(self, engine_id: bytes, boots: int, engine_time: int) -> None:
        if engine_id != self._engine_id:
            self._auth_key = localize_key(self._auth_master, engine_id, self._digest)
            if self._priv_master is not None:
                self._priv_key = localize_key(self._priv_master, engine_id, self._digest)[:16]
        self._engine_id = engine_id
        self._boots = boots
        self._time = engine_time
        self._time_read_at = time.monotonic()

    def _engine_time_now(self) -> int:
        return self._time + int(time.monotonic() - self._time_read_at)

    # -- one request ---------------------------------------------------------

    def _exchange(
        self, pdu_tag: int, oids: list[str], *, max_repetitions: int = 0
    ) -> ResponseMessage:
        target = self._address()
        if self._engine_id is None:
            self._discover(target)
        try:
            return self._request(target, pdu_tag, oids, max_repetitions)
        except _ReportReceived as report:
            if report.oid in (_REPORT_NOT_IN_TIME_WINDOW, _REPORT_UNKNOWN_ENGINE_ID):
                self._discover(target)  # the engine rebooted or its clock moved
            else:
                raise _report_error(report.oid) from None
        try:
            return self._request(target, pdu_tag, oids, max_repetitions)
        except _ReportReceived as report:
            if report.oid in (_REPORT_NOT_IN_TIME_WINDOW, _REPORT_UNKNOWN_ENGINE_ID):
                raise SnmpError("SNMPv3 time window could not be agreed") from None
            raise _report_error(report.oid) from None

    def _request(self, target, pdu_tag: int, oids: list[str], max_repetitions: int):
        def build():
            msg_id = secrets.randbelow(2**31 - 1) + 1
            request_id = secrets.randbelow(2**31 - 1) + 1
            return self._encode(pdu_tag, oids, max_repetitions, msg_id, request_id), (
                msg_id,
                request_id,
            )

        def accept(data: bytes, ids: tuple[int, int]):
            return self._accept(data, *ids)

        response = self._send_with_retries(target, build, accept)
        _raise_error_status(response)
        return response

    def _encode(
        self, pdu_tag: int, oids: list[str], max_repetitions: int, msg_id: int, request_id: int
    ) -> bytes:
        assert self._engine_id is not None
        boots, engine_time = self._boots, self._engine_time_now()
        scoped = encode_sequence(
            encode_octets(self._engine_id),
            encode_octets(self.context),
            _build_pdu(pdu_tag, request_id, oids, max_repetitions=max_repetitions),
        )
        flags = MSG_FLAG_AUTH | MSG_FLAG_REPORTABLE
        priv_params = b""
        if self._priv_master is not None:
            flags |= MSG_FLAG_PRIV
            self._salt = (self._salt + 1) % (1 << 64)
            priv_params = self._salt.to_bytes(8, "big")
            iv = boots.to_bytes(4, "big") + engine_time.to_bytes(4, "big") + priv_params
            scoped = encode_octets(_aes_cfb(self._priv_key, iv, scoped, encrypt=True))

        def message(mac: bytes) -> bytes:
            security = _usm_parameters(
                self._engine_id, boots, engine_time, self.user, mac, priv_params
            )
            return _v3_message(msg_id, flags, security, scoped)

        unsigned = message(bytes(self._mac_length))
        return message(self._mac(unsigned))

    def _mac(self, packet: bytes) -> bytes:
        return hmac.new(self._auth_key, packet, self._digest).digest()[: self._mac_length]

    def _accept(self, data: bytes, msg_id: int, request_id: int) -> ResponseMessage | None:
        """The answer to our request, None for a datagram that is not, or a raise."""
        try:
            message = _parse_v3(data)
        except SnmpError:
            return None
        if message.msg_id != msg_id:
            return None
        if message.flags & MSG_FLAG_AUTH:
            if message.engine_id != self._engine_id or message.user != self.user:
                return None
            if len(message.auth_params) != self._mac_length:
                raise SnmpError("SNMPv3 response failed authentication")
            at = message.auth_at
            zeroed = data[:at] + bytes(self._mac_length) + data[at + self._mac_length :]
            if not hmac.compare_digest(self._mac(zeroed), message.auth_params):
                raise SnmpError("SNMPv3 response failed authentication")
        raw = message.data
        if message.flags & MSG_FLAG_PRIV:
            if not message.flags & MSG_FLAG_AUTH or message.data_tag != OCTET_STRING:
                return None
            if self._priv_master is None or len(message.priv_params) != 8:
                raise SnmpError("SNMPv3 response could not be decrypted")
            iv = (
                message.boots.to_bytes(4, "big")
                + message.engine_time.to_bytes(4, "big")
                + message.priv_params
            )
            raw = _aes_cfb(self._priv_key, iv, raw, encrypt=False)
        elif message.data_tag != SEQUENCE:
            return None
        try:
            pdu_tag, got_id, status, index, rows = _parse_scoped_pdu(raw)
        except SnmpError:
            if message.flags & MSG_FLAG_PRIV:
                raise SnmpError("SNMPv3 response could not be decrypted") from None
            return None
        if pdu_tag == Report:
            # Reports about the user, digest or clock are sent unauthenticated
            # (RFC 3414 3.2): they can only fail this call, never answer it.
            oid = rows[0][0] if rows else ""
            raise _ReportReceived(oid)
        # A real answer must be as protected as the request was.
        wanted = MSG_FLAG_AUTH | (MSG_FLAG_PRIV if self._priv_master is not None else 0)
        if message.flags & wanted != wanted or got_id != request_id:
            return None
        return ResponseMessage(
            version=3,
            community=b"",
            request_id=got_id,
            error_status=status,
            error_index=index,
            varbinds=rows,
        )


def _report_error(oid: str) -> SnmpError:
    return SnmpError(_REPORT_SENTENCES.get(oid, f"SNMPv3 report {oid}"))
