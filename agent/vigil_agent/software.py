"""Installed-software collection on Linux.

Pure collection: the payload is exactly what the server's software ingest
accepts — four top-level keys (the list digest, the collection timestamp, the
items and the per-source errors), each item carrying the nine fields the ingest
reads. No scheduling and no sending here, and no ``apt update``/``dnf makecache``:
the whole collection is offline, reading the package databases as they stand.

A failing source never propagates. It contributes no items and one entry in
``errors``; the remaining sources still run.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import shutil
import subprocess

logger = logging.getLogger("vigil.software")

ITEM_FIELDS = ("source", "id", "name", "version", "latest", "scope", "user",
               "publisher", "managed")

#: Primary package database — the first whose binary exists.
_PRIMARY_ORDER = (("dpkg", "dpkg-query"), ("rpm", "rpm"), ("apk", "apk"),
                  ("pacman", "pacman"))

#: Overlay stores, collected in addition to the primary database.
_OVERLAYS = (("snap", "snap"), ("flatpak", "flatpak"))

#: snap rows whose Notes say they are runtime plumbing rather than apps.
_SNAP_BASE_MARKERS = ("base", "core", "snapd", "gadget")


def _run(argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """(returncode, stdout, stderr) — the one place a command runs. Tests patch this."""
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _which(binary: str) -> bool:
    return shutil.which(binary) is not None


def _failure(exc: BaseException, argv: list[str]) -> str:
    return f"{argv[0]} failed: {str(exc)[:200]}"


# ── item construction ─────────────────────────────────────────────────────────

def _item(source: str, ident: str, name: str, version: str,
          publisher: str = "", latest: str = "") -> dict:
    """One inventory row; the key set is exactly ``ITEM_FIELDS``."""
    return dict(zip(ITEM_FIELDS, (source, ident, name, version, latest,
                                  "machine", "", publisher, True)))


def _merge_updates(items: list[dict], updates: dict[str, str],
                   by: str = "name") -> None:
    """Stamp ``latest`` from the source's own update listing, matched on one field."""
    for item in items:
        latest = updates.get(item[by])
        if latest:
            item["latest"] = latest


# ── dpkg ──────────────────────────────────────────────────────────────────────

_DPKG_ARGV = ["dpkg-query", "-W",
              ("-f=${binary:Package}\t${Package}\t${Version}\t"
               "${db:Status-Abbrev}\t${Maintainer}\n")]
_APT_UPDATES_ARGV = ["apt", "list", "--upgradable"]


def _clean_publisher(value: str) -> str:
    return value.split(" <")[0].strip()


def _parse_dpkg_list(text: str) -> list[dict]:
    """``dpkg-query -W`` tab rows; keeps only installed ones (``ii``/``hi``)."""
    items: list[dict] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        status = fields[3].strip()
        if not status.startswith(("ii", "hi")):
            continue
        publisher = _clean_publisher(fields[4]) if len(fields) > 4 else ""
        items.append(_item("dpkg", fields[0].strip(), fields[1].strip(),
                           fields[2].strip(), publisher))
    return items


#: An upgradable row is ``name/suite version arch [upgradable from: old]``.
_APT_UPGRADE_ROW = re.compile(r"^(?P<name>[^/\s]+)/\S+\s+(?P<latest>\S+)")


def _parse_dpkg_updates(text: str) -> dict[str, str]:
    """``apt list --upgradable`` → {package name: latest version}.

    ``bluez/resolute-updates 5.85-4ubuntu0.2 amd64 [upgradable from: …]`` — the
    name is the text before the suite, the version the field after it.
    """
    updates: dict[str, str] = {}
    for line in text.splitlines():
        match = _APT_UPGRADE_ROW.match(line)
        if match is not None:
            updates[match.group("name")] = match.group("latest")
    return updates


def _collect_dpkg(errors: dict[str, str]) -> list[dict]:
    items = _run_and_parse(_DPKG_ARGV, _parse_dpkg_list, errors, "dpkg", _LIST_SHAPE)
    updates = _run_and_parse(_APT_UPDATES_ARGV, _parse_dpkg_updates, errors,
                             "dpkg-updates", shape=_MAP_SHAPE)
    _merge_updates(items, updates)
    return items


# ── rpm ───────────────────────────────────────────────────────────────────────

_RPM_ARGV = ["rpm", "-qa", "--qf",
             ("%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{VENDOR}\n")]


def _strip_epoch(version: str) -> str:
    return version.removeprefix("0:")


def _parse_rpm_list(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        name = fields[0].strip()
        publisher = fields[2].strip() if len(fields) > 2 else ""
        if publisher == "(none)":
            publisher = ""
        items.append(_item("rpm", name, name, _strip_epoch(fields[1].strip()),
                           publisher))
    return items


def _parse_check_update(text: str) -> dict[str, str]:
    """``dnf/yum -q check-update`` → {name: latest}, arch stripped, epoch kept.

    ``openssl.x86_64    1:3.2.2-10.fc41    updates`` — name is field 1 up to
    the last dot; parsing stops at the "Obsoleting Packages" section header.
    """
    updates: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("Obsoleting Packages"):
            break
        fields = line.split()
        if len(fields) < 2:
            continue
        updates[fields[0].rsplit(".", 1)[0]] = _strip_epoch(fields[1])
    return updates


def _collect_rpm(errors: dict[str, str]) -> list[dict]:
    items = _run_and_parse(_RPM_ARGV, _parse_rpm_list, errors, "rpm", _LIST_SHAPE)
    updates: dict[str, str] = {}
    for binary in ("dnf", "yum"):
        if _which(binary):
            # check-update exits 100 when it found updates, 0 when it found none.
            updates = _run_and_parse([binary, "-q", "check-update"],
                                     _parse_check_update, errors,
                                     "rpm-updates", shape=_MAP_SHAPE, ok_codes=(0, 100))
            break
    _merge_updates(items, updates)
    return items


# ── apk ───────────────────────────────────────────────────────────────────────

_APK_ARGV = ["apk", "info", "-v"]
_APK_UPDATES_ARGV = ["apk", "list", "-u"]

#: apk versions look like ``8.9.1-r1``; the name is everything before the
#: hyphen that starts one, so ``py3-foo-bar-1.2-r0`` splits as intended.
_APK_NAME_VERSION = re.compile(r"^(.+)-(\d[^-]*-r\d+)$")


def _split_apk(token: str) -> tuple[str, str] | None:
    match = _APK_NAME_VERSION.match(token)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _parse_apk_list(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        split = _split_apk(line.strip())
        if split is not None:
            items.append(_item("apk", split[0], split[0], split[1]))
    return items


def _parse_apk_updates(text: str) -> dict[str, str]:
    updates: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        split = _split_apk(fields[0])
        if split is not None:
            updates[split[0]] = split[1]
    return updates


def _collect_apk(errors: dict[str, str]) -> list[dict]:
    items = _run_and_parse(_APK_ARGV, _parse_apk_list, errors, "apk", _LIST_SHAPE)
    updates = _run_and_parse(_APK_UPDATES_ARGV, _parse_apk_updates, errors,
                             "apk-updates", shape=_MAP_SHAPE)
    _merge_updates(items, updates)
    return items


# ── pacman ────────────────────────────────────────────────────────────────────

_PACMAN_ARGV = ["pacman", "-Q"]
_PACMAN_UPDATES_ARGV = ["pacman", "-Qu"]


def _parse_pacman_list(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        items.append(_item("pacman", fields[0], fields[0], fields[1]))
    return items


def _parse_pacman_updates(text: str) -> dict[str, str]:
    """``openssl 3.3.2-1 -> 3.3.3-1`` → latest is the last field."""
    updates: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or "->" not in fields:
            continue
        updates[fields[0]] = fields[-1]
    return updates


def _collect_pacman(errors: dict[str, str]) -> list[dict]:
    items = _run_and_parse(_PACMAN_ARGV, _parse_pacman_list, errors, "pacman", _LIST_SHAPE)
    # -Qu exits 1 with empty output when nothing is outdated.
    updates = _run_and_parse(_PACMAN_UPDATES_ARGV, _parse_pacman_updates,
                             errors, "pacman-updates", shape=_MAP_SHAPE, ok_codes=(0, 1))
    _merge_updates(items, updates)
    return items


# ── snap ──────────────────────────────────────────────────────────────────────

_SNAP_ARGV = ["snap", "list"]
_SNAP_UPDATES_ARGV = ["snap", "refresh", "--list"]


def _snap_publisher(value: str) -> str:
    return value.rstrip("*\u2713").strip()


def _parse_snap_list(text: str) -> list[dict]:
    """Column rows (header dropped); rows with base-ish Notes are plumbing."""
    items: list[dict] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5:
            continue
        notes = fields[5] if len(fields) > 5 else ""
        if any(marker in notes for marker in _SNAP_BASE_MARKERS):
            continue
        items.append(_item("snap", fields[0], fields[0], fields[1],
                           _snap_publisher(fields[4])))
    return items


def _parse_snap_updates(text: str) -> dict[str, str]:
    """Header dropped; "All snaps up to date." (which snap prints to stderr) yields {}."""
    updates: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] == "Name":
            continue
        updates[fields[0]] = fields[1]
    return updates


def _collect_snap(errors: dict[str, str]) -> list[dict]:
    items = _run_and_parse(_SNAP_ARGV, _parse_snap_list, errors, "snap", _LIST_SHAPE)
    updates = _run_and_parse(_SNAP_UPDATES_ARGV, _parse_snap_updates, errors,
                             "snap-updates", shape=_MAP_SHAPE)
    _merge_updates(items, updates)
    return items


# ── flatpak ───────────────────────────────────────────────────────────────────

_FLATPAK_ARGV = ["flatpak", "list", "--app",
                 "--columns=application,name,version,installation"]
_FLATPAK_UPDATES_ARGV = ["flatpak", "remote-ls", "--updates", "--app",
                         "--columns=application,version"]

_MULTI_SPACE = re.compile(r"\s{2,}")


def _split_columns(line: str) -> list[str]:
    """flatpak separates columns with tabs; fall back to runs of 2+ spaces."""
    if "\t" in line:
        return [field.strip() for field in line.split("\t")]
    return [field.strip() for field in _MULTI_SPACE.split(line.strip())]


def _parse_flatpak_list(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_columns(line)
        if len(fields) < 3:
            continue
        items.append(_item("flatpak", fields[0], fields[1], fields[2]))
    return items


def _parse_flatpak_updates(text: str) -> dict[str, str]:
    updates: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_columns(line)
        if len(fields) < 2:
            continue
        updates[fields[0]] = fields[1]
    return updates


def _collect_flatpak(errors: dict[str, str]) -> list[dict]:
    items = _run_and_parse(_FLATPAK_ARGV, _parse_flatpak_list, errors,
                           "flatpak", _LIST_SHAPE)
    updates = _run_and_parse(_FLATPAK_UPDATES_ARGV, _parse_flatpak_updates,
                             errors, "flatpak-updates", shape=_MAP_SHAPE)
    # remote-ls names apps by application id; the item's id is the match, not
    # its display name.
    _merge_updates(items, updates, by="id")
    return items


# ── run/parse helper ──────────────────────────────────────────────────────────

#: What a parser returns when its command produced nothing usable.
_LIST_SHAPE = "list"
_MAP_SHAPE = "map"
_EMPTY = {_LIST_SHAPE: [], _MAP_SHAPE: {}}


def _run_and_parse(argv: list[str], parse, errors: dict[str, str], key: str,
                   shape: str, ok_codes: tuple[int, ...] = (0,)):
    """Run one command and parse it; on any failure record the error, return empty.

    ``shape`` says which empty value the caller merges — ``_LIST_SHAPE`` for a
    listing, ``_MAP_SHAPE`` for an update map — so a failed command never
    breaks the merge.
    """
    try:
        code, out, err = _run(argv)
    except (subprocess.SubprocessError, OSError) as exc:
        errors[key] = _failure(exc, argv)
        return _EMPTY[shape]
    if code not in ok_codes:
        errors[key] = f"{argv[0]} failed (exit {code}): {(err or '').strip()[:200]}"
        return _EMPTY[shape]
    return parse(out)


# ── payload ───────────────────────────────────────────────────────────────────

def digest(items: list[dict]) -> str:
    """sha256 over the items — independent of list order and dict key order."""
    ordered = sorted(items, key=lambda i: (i.get("source", ""), i.get("id", ""),
                                           i.get("scope", ""), i.get("user", "")))
    return hashlib.sha256(json.dumps(ordered, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _collected_at() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_COLLECTORS = {
    "dpkg": _collect_dpkg,
    "rpm": _collect_rpm,
    "apk": _collect_apk,
    "pacman": _collect_pacman,
    "snap": _collect_snap,
    "flatpak": _collect_flatpak,
}


def collect_linux() -> dict:
    """The server's software payload: primary database plus snap and flatpak."""
    items: list[dict] = []
    errors: dict[str, str] = {}
    for name, binary in _PRIMARY_ORDER:
        if _which(binary):
            items.extend(_COLLECTORS[name](errors))
            break
    for name, binary in _OVERLAYS:
        if _which(binary):
            items.extend(_COLLECTORS[name](errors))
    return {"digest": digest(items), "collected_at": _collected_at(),
            "items": items, "errors": errors}
