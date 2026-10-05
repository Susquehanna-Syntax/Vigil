"""Version comparison, one function per package ecosystem's own rules.

Three schemes:

* ``deb`` — Debian policy, ``[epoch:]upstream[-revision]`` (revision after the
  *last* '-'), compared with a direct port of dpkg's ``verrevcmp()``: '~' sorts
  before everything (even the end), then digits/end, letters, other characters.
  Verified pair-for-pair against ``dpkg --compare-versions``.
* ``rpm`` — ``[epoch:]version[-release]`` compared like ``rpmVersionCompare``,
  each part with a direct port of rpm's ``rpmvercmp()`` ('~' sorts first, '^'
  after the end but before anything else; a side without a release compares
  on epoch and version only). Passes rpm's own ``rpmvercmp.at`` vectors.
* ``generic`` — dotted/numeric: split on ``.``, ``-``, ``_`` and ``+``;
  numeric parts compare numerically, the rest as strings, numeric parts beat
  text parts, and missing parts count as 0.

No subprocess calls: this module is pure string math.
"""

from __future__ import annotations

import re

_GENERIC_SPLIT = re.compile(r"[.\-+_]+")


def compare(a: str, b: str, scheme: str) -> int:
    """Compare version strings ``a`` and ``b`` with the named scheme.

    Returns -1 when ``a`` is older, 0 when equivalent, 1 when newer.
    ``scheme`` is "deb", "rpm" or "generic".
    """
    if scheme == "deb":
        return _compare_deb(a, b)
    if scheme == "rpm":
        return _compare_rpm(a, b)
    if scheme == "generic":
        return _compare_generic(a, b)
    raise ValueError(f"unknown version scheme: {scheme!r}")


# ── dpkg ─────────────────────────────────────────────────────────────────────


def _compare_deb(a: str, b: str) -> int:
    """Debian version order: epoch, then upstream, then revision (dpkg's rules)."""
    ea, ua, ra = _deb_parse(a)
    eb, ub, rb = _deb_parse(b)
    if ea != eb:
        return 1 if ea > eb else -1
    return _sign(_verrevcmp(ua, ub) or _verrevcmp(ra, rb))


def _deb_parse(version: str) -> tuple[int, str, str]:
    """``[epoch:]upstream[-revision]`` as dpkg parses it.

    The epoch is the part before the first ':'; the revision is everything
    after the *last* '-' (an upstream version may itself contain hyphens); a
    missing revision is the empty string, which dpkg orders like "0".
    """
    version = version.strip()
    epoch = 0
    if ":" in version:
        head, version = version.split(":", 1)
        epoch = int(head) if head.isdigit() else 0
    upstream, revision = version, ""
    if "-" in version:
        upstream, revision = version.rsplit("-", 1)
    return epoch, upstream, revision


def _deb_order(ch: str) -> int:
    """dpkg's character weight: '~' lowest, then end/digits, letters, then other chars."""
    if not ch or ch.isdigit():
        return 0
    if ch.isalpha():
        return ord(ch)
    if ch == "~":
        return -1
    return ord(ch) + 256


def _verrevcmp(a: str, b: str) -> int:
    """A direct port of dpkg's verrevcmp() (lib/dpkg/version.c)."""
    ia = ib = 0
    la, lb = len(a), len(b)
    while ia < la or ib < lb:
        first_diff = 0
        while (ia < la and not a[ia].isdigit()) or (ib < lb and not b[ib].isdigit()):
            ac = _deb_order(a[ia] if ia < la else "")
            bc = _deb_order(b[ib] if ib < lb else "")
            if ac != bc:
                return ac - bc
            ia += 1
            ib += 1
        while ia < la and a[ia] == "0":
            ia += 1
        while ib < lb and b[ib] == "0":
            ib += 1
        while ia < la and ib < lb and a[ia].isdigit() and b[ib].isdigit():
            if not first_diff:
                first_diff = ord(a[ia]) - ord(b[ib])
            ia += 1
            ib += 1
        if ia < la and a[ia].isdigit():
            return 1
        if ib < lb and b[ib].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


# ── rpm ──────────────────────────────────────────────────────────────────────


def _compare_rpm(a: str, b: str) -> int:
    """RPM order: epoch, then version, then release (rpmVersionCompare).

    A side with no release (a bound such as "3.0.13") is compared on epoch and
    version only — the same way rpm treats a dependency range that names no
    release.
    """
    ea, va, ra = _rpm_parse(a)
    eb, vb, rb = _rpm_parse(b)
    if ea != eb:
        return 1 if ea > eb else -1
    rc = _rpmvercmp(va, vb)
    if rc or ra is None or rb is None:
        return rc
    return _rpmvercmp(ra, rb)


def _rpm_parse(evr: str) -> tuple[int, str, str | None]:
    """``[epoch:]version[-release]`` — the release is after the last '-'."""
    evr = evr.strip()
    epoch = 0
    if ":" in evr:
        head, evr = evr.split(":", 1)
        epoch = int(head) if head.isdigit() else 0
    if "-" in evr:
        version, release = evr.rsplit("-", 1)
        return epoch, version, release
    return epoch, evr, None


def _rpmvercmp(a: str, b: str) -> int:
    """A direct port of rpm's rpmvercmp() (rpmio/rpmvercmp.c)."""
    if a == b:
        return 0
    i = j = 0
    la, lb = len(a), len(b)

    def sep(s: str, k: int) -> bool:
        return k < len(s) and not s[k].isalnum() and s[k] not in "~^"

    while i < la or j < lb:
        while sep(a, i):
            i += 1
        while sep(b, j):
            j += 1
        ca = a[i] if i < la else ""
        cb = b[j] if j < lb else ""
        # '~' sorts before everything, even the end of the string.
        if ca == "~" or cb == "~":
            if ca != "~":
                return 1
            if cb != "~":
                return -1
            i += 1
            j += 1
            continue
        # '^' sorts after the end of the string but before anything else.
        if ca == "^" or cb == "^":
            if not ca:
                return -1
            if not cb:
                return 1
            if ca != "^":
                return 1
            if cb != "^":
                return -1
            i += 1
            j += 1
            continue
        if not (ca and cb):
            break
        si, sj = i, j
        if ca.isdigit():
            while i < la and a[i].isdigit():
                i += 1
            while j < lb and b[j].isdigit():
                j += 1
            isnum = True
        else:
            while i < la and a[i].isalpha():
                i += 1
            while j < lb and b[j].isalpha():
                j += 1
            isnum = False
        seg_a, seg_b = a[si:i], b[sj:j]
        if not seg_a:
            return -1
        if not seg_b:
            return 1 if isnum else -1
        if isnum:
            seg_a = seg_a.lstrip("0")
            seg_b = seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return 1 if seg_a > seg_b else -1
    ta = a[i:] if i < la else ""
    tb = b[j:] if j < lb else ""
    if not ta and not tb:
        return 0
    return -1 if not ta else 1


def _compare_generic(a: str, b: str) -> int:
    parts_a = _GENERIC_SPLIT.split(a)
    parts_b = _GENERIC_SPLIT.split(b)
    # A missing part counts as 0: pad the shorter list with zeros so 1.0 == 1.0.0.
    pad = max(len(parts_a), len(parts_b))
    while len(parts_a) < pad:
        parts_a.append("0")
    while len(parts_b) < pad:
        parts_b.append("0")
    for pa, pb in zip(parts_a, parts_b):
        a_num = pa.isdigit()
        b_num = pb.isdigit()
        if a_num != b_num:
            return 1 if a_num else -1
        if a_num:
            na, nb = int(pa), int(pb)
            if na != nb:
                return 1 if na > nb else -1
        elif pa != pb:
            return 1 if pa > pb else -1
    return 0


def _sign(value: int) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0
