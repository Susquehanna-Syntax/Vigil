"""An SNMP v2c client: BER, message encoding, GET and WALK over UDP.

Standard library only — the agent ships as one binary and gets no new
dependency. Nothing here raises anything but SnmpError: a malformed packet,
an IndexError or a UnicodeDecodeError inside decoding all become SnmpError.

Not wired into checks yet (phase 05b adds the "snmp" check kind, the server
side and v3).
"""

from __future__ import annotations

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
_EMPTY_SEQUENCE_TAGS = (SEQUENCE, GetRequest, GetNextRequest, Response, GetBulkRequest)
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
    # A length is compared as bytes first: turning it into a number and shifting
    # by it must never be reachable with a value the buffer cannot possibly hold.
    length_bytes = buf[pos + 2 : pos + 2 + count]
    if length_bytes > len(buf).to_bytes(count, "big"):
        raise _malformed("a length runs past the end of the packet")
    length = int.from_bytes(length_bytes, "big")
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
    if tag in (SEQUENCE, GetRequest, GetNextRequest, Response, GetBulkRequest):
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
    second = non_repeaters if pdu_tag == GetBulkRequest else 0
    third = max_repetitions if pdu_tag == GetBulkRequest else 0
    pairs = b"".join(
        encode_sequence(encode_oid(oid), encode_null()) for oid in varbinds
    )
    pdu = encode_sequence(
        encode_integer(request_id),
        encode_integer(second),
        encode_integer(third),
        encode_sequence(pairs),
        tag=pdu_tag,
    )
    return encode_sequence(encode_integer(1), encode_octets(community), pdu)


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
    if pdu_tag != Response:
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
    return ResponseMessage(
        version=version,
        community=community,
        request_id=request_id,
        error_status=error_status,
        error_index=error_index,
        varbinds=rows,
    )


# --- the client -----------------------------------------------------------


class V2cClient:
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
        try:
            infos = socket.getaddrinfo(
                self.host, self.port, socket.AF_UNSPEC, socket.SOCK_DGRAM
            )
        except socket.gaierror as exc:
            raise SnmpError(f"cannot resolve {self.host}: {exc}") from None
        if not infos:
            raise SnmpError(f"no address for {self.host}")
        family, socktype, proto, _canonical, address = infos[0]
        for _attempt in range(self.retries + 1):
            request_id = secrets.randbelow(2**31 - 1) + 1
            request = build_request(
                pdu_tag,
                request_id,
                self.community,
                oids,
                max_repetitions=max_repetitions,
            )
            try:
                response = self._round_trip(
                    family, socktype, proto, address, request, request_id
                )
            except OSError as exc:
                raise SnmpError(
                    f"cannot send SNMP to {self.host}:{self.port}: {exc}"
                ) from None
            if response is not None:
                return response
        raise SnmpTimeout(f"no SNMP response from {self.host}:{self.port}")

    def _round_trip(
        self,
        family: int,
        socktype: int,
        proto: int,
        address: tuple,
        request: bytes,
        request_id: int,
    ) -> ResponseMessage | None:
        """One socket's life: send, then read until the deadline for our own reply."""
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
                try:
                    response = parse_response(data)
                except SnmpError:
                    continue
                if response.request_id != request_id:
                    continue
                if response.community != self.community:
                    continue
                _raise_error_status(response)
                return response
        finally:
            sock.close()


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
