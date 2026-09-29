"""Installed-software collection.

Pure collection: the payload is exactly what the server's software ingest
accepts — four top-level keys (the list digest, the collection timestamp, the
items and the per-source errors), each item carrying the nine fields the ingest
reads. No scheduling and no sending here, and no ``apt update``/``dnf makecache``:
the whole collection is offline, reading the package databases as they stand.

A failing source never propagates. It contributes no items and one entry in
``errors``; the remaining sources still run.

Windows collects from winget, Chocolatey and Scoop, then from the registry's
Uninstall keys — machine-wide and each logged-in user's, because the agent runs
as a service account and HKCU is therefore that account's hive rather than any
person's. A registry entry no package manager reported is emitted unmanaged.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys

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
          publisher: str = "", *, latest: str = "", scope: str = "machine",
          user: str = "", managed: bool = True) -> dict:
    """One inventory row.

    ``managed`` is False for the registry, which reports installs Vigil can see
    but no package manager can act on.
    """
    return dict(zip(ITEM_FIELDS, (source, ident, name, version, latest,
                                  scope, user, publisher, managed)))


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


# ── Windows: name key ─────────────────────────────────────────────────────────

#: Trailing architecture tag, alone or leading a parenthesised group
#: (``(x64 en-US)``, ``x64``). Stripped only from the end of the name.
_ARCH_TAG = re.compile(
    r"\s*\(?\s*(?:x64|x86|64-bit|32-bit)(?:\s[^)]*)?\)\s*$", re.IGNORECASE
)

#: Trailing version token — needs a dot, so a lone year in
#: "Microsoft Visual C++ 2015-2022 Redistributable" survives.
_VERSION_TAG = re.compile(r"\s+v?\d+(?:\.\d+)+(?:\s.*)?$")


def name_key(name: str) -> str:
    """Canonical grouping key for a display name.

    Verbatim the server's ``apps.software.models.name_key``: the agent uses it
    to decide which registry entries a package manager already claims, so the
    two must agree byte for byte.
    """
    key = (name or "").strip().lower()
    key = _ARCH_TAG.sub("", key)
    key = _VERSION_TAG.sub("", key)
    return re.sub(r"\s+", " ", key).strip()


# ── Windows: winget ───────────────────────────────────────────────────────────

#: The header words of ``winget list`` / ``winget upgrade``, in column order.
#: ``Available`` is absent when nothing is upgradable.
_WINGET_COLUMNS = ("Name", "Id", "Version", "Available", "Source")


def _is_winget_rule(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 20 and set(stripped) == {"-"}


def _parse_winget_table(text: str) -> list[dict]:
    r"""Rows of a ``winget list`` / ``winget upgrade`` table.

    Column boundaries are the header words' character offsets: a field's value
    runs to the start of the next column and the row ends there, so an empty
    value stays empty instead of pulling the following column's text in. That
    is what distinguishes a blank ``Source`` (winget did not install it) from a
    ``winget`` one, and keeps ``MSIX\\…`` ids intact.
    """
    lines = text.splitlines()
    rule_at = next((n for n, line in enumerate(lines) if _is_winget_rule(line)),
                   None)
    if rule_at is None:
        return []
    header = next((lines[n] for n in range(rule_at - 1, -1, -1)
                   if _WINGET_COLUMNS[0] in lines[n]), None)
    if header is None:
        return []
    present = [word for word in _WINGET_COLUMNS if word in header]
    offsets = [header.index(word) for word in present]
    ends = offsets[1:] + [None]

    rows: list[dict] = []
    for line in lines[rule_at + 1:]:
        if not line.strip():
            if rows:
                break
            continue
        if line[0].isdigit() and "upgrades available" in line:
            break
        if len(line) < offsets[1]:
            continue
        values = [line[start:end].strip() for start, end in zip(offsets, ends)]
        # Pair each value with the column it came from: a table without an
        # Available column must not shift Source into Available.
        row = dict.fromkeys(_WINGET_COLUMNS, "")
        row.update(zip(present, values))
        rows.append(row)
    return rows


def _winget_latest(rows: list[dict]) -> dict[str, str]:
    return {row["Id"]: row["Available"] for row in rows
            if row["Id"] and row["Available"]}


def _collect_winget(binary: str, errors: dict[str, str]) -> list[dict]:
    """winget's own packages, with the newest version winget offers."""
    code, out, err = _run([binary, "list", "--accept-source-agreements",
                           "--disable-interactivity"])
    if code != 0:
        raise RuntimeError(f"winget list exited {code}: {_head(err)}")
    listed = _parse_winget_table(out)

    # ``winget upgrade`` is the authoritative "what winget would move to"; the
    # list's own Available column can be empty before the sources agree.
    latest = _winget_latest(listed)
    code, out, err = _run([binary, "upgrade", "--accept-source-agreements",
                           "--disable-interactivity"])
    if code != 0:
        # An unreadable upgrade list leaves the list's own Available column in
        # place — it is the same offer whenever winget has one.
        logger.warning("software: winget upgrade exited %s: %s", code,
                       _head(err))
    else:
        latest.update(_winget_latest(_parse_winget_table(out)))

    return [_item("winget", row["Id"], row["Name"], row["Version"],
                  latest=latest.get(row["Id"], ""))
            for row in listed if row["Source"] == "winget"]


# ── Windows: Chocolatey ───────────────────────────────────────────────────────

def _collect_chocolatey(errors: dict[str, str]) -> list[dict]:
    """``choco list --limit-output``, with ``choco outdated`` as the newest."""
    code, out, err = _run(["choco", "list", "--limit-output"])
    if code != 0:
        raise RuntimeError(f"choco list exited {code}: {_head(err)}")
    installed = [line.split("|") for line in out.splitlines()]

    latest: dict[str, str] = {}
    code, out, err = _run(["choco", "outdated", "--limit-output"])
    if code != 0:
        raise RuntimeError(f"choco outdated exited {code}: {_head(err)}")
    for line in out.splitlines():
        fields = line.split("|")
        # name|current|available|pinned — a pinned package offers nothing newer.
        if (len(fields) == 4 and fields[0] and fields[2]
                and fields[2] != fields[1] and fields[3].lower() != "true"):
            latest[fields[0]] = fields[2]

    items = []
    for fields in installed:
        # One pipe, no empty fields: a malformed line is skipped rather than
        # inventing a package with no version.
        if len(fields) != 2 or not all(fields):
            continue
        items.append(_item("chocolatey", fields[0], fields[0], fields[1],
                           latest=latest.get(fields[0], "")))
    return items


# ── Windows: Scoop ────────────────────────────────────────────────────────────

def _collect_scoop(errors: dict[str, str]) -> list[dict]:
    """``scoop export``, which is JSON. Scoop is a ``.cmd`` shim, so cmd runs it."""
    code, out, err = _run(["cmd", "/c", "scoop", "export"])
    if code != 0:
        raise RuntimeError(f"scoop export exited {code}: {_head(err)}")
    try:
        apps = json.loads(out)["apps"]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"scoop export produced unusable JSON: {str(exc)[:120]}") from exc
    items = []
    for app in apps:
        if not isinstance(app, dict):
            continue
        name = str(app.get("Name") or "").strip()
        if not name:
            continue
        items.append(_item("scoop", name, name,
                           str(app.get("Version") or "").strip()))
    return items


# ── Windows: registry Uninstall keys ──────────────────────────────────────────

#: Uninstall container path tail, hung off HKLM (``SOFTWARE``, and the WOW6432
#: mirror) and off each user hive (``Software``).
_UNINSTALL_TAIL = r"Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_64 = rf"SOFTWARE\{_UNINSTALL_TAIL}"
_UNINSTALL_WOW = rf"SOFTWARE\WOW6432Node\{_UNINSTALL_TAIL}"
_UNINSTALL_USER = rf"Software\{_UNINSTALL_TAIL}"
_PROFILELIST = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"

#: The Uninstall values the item rules read.
_UNINSTALL_VALUES = ("DisplayName", "DisplayVersion", "Publisher",
                     "SystemComponent", "ParentKeyName", "ReleaseType",
                     "WindowsInstaller", "QuietUninstallString",
                     "UninstallString")

#: Rows that are a Windows Update rather than a product someone installed.
_UPDATE_RELEASE_TYPES = frozenset({"update", "hotfix", "security update"})


def _head(text: str) -> str:
    """The first non-blank line of a command's stderr, for error strings."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _winget_binary() -> str | None:
    r"""winget on PATH, else inside the App Installer package; None if absent.

    The agent runs as a service account, whose PATH usually has no winget even
    when the App Installer package is installed — hence the resolver. It is
    imported here rather than at module scope: pkg_manager pulls in the tool
    modules, which would make ``import vigil_agent.software`` heavy and cyclic.

    winget is genuinely absent on Server 2019 / LTSC, so not finding it is a
    skip and not an error.
    """
    from . import pkg_manager

    try:
        return shutil.which("winget") or pkg_manager._resolve_winget()
    except OSError:
        return None


def _winreg():
    """The winreg module, behind a function so tests can patch it out."""
    import winreg

    return winreg


def _reg_open(winreg, hkey, path: str):
    """Open one key read-only in the 64-bit view, so 32-bit installs are not
    the only ones visible on an x64 host."""
    return winreg.OpenKey(hkey, path, 0,
                          winreg.KEY_READ | winreg.KEY_WOW64_64KEY)


def _reg_subkeys(winreg, handle) -> list[str]:
    names = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(handle, index))
        except OSError:
            return names
        index += 1


def _reg_data(data):
    """A registry value's data as text, or None.

    Numeric data stays an int so the ``SystemComponent`` / ``WindowsInstaller``
    rules can compare against 1.
    """
    if data is None:
        return None
    if isinstance(data, int):
        return data
    if isinstance(data, (list, tuple)):  # REG_MULTI_SZ
        return " ".join(str(part) for part in data)
    if isinstance(data, bytes):  # REG_BINARY
        return data.decode("utf-8", "replace")
    return str(data)


def _reg_values(winreg, handle, names: tuple[str, ...]) -> dict:
    """Several values of one opened key, as a dict (absent → None)."""
    out: dict = {}
    for name in names:
        try:
            data, _type = winreg.QueryValueEx(handle, name)
        except OSError:
            out[name] = None
            continue
        out[name] = _reg_data(data)
    return out


def _is_user_sid(sid: str) -> bool:
    """An interactive user's SID — not a service account, not a redirected hive."""
    return sid.startswith("S-1-5-21-") and not sid.endswith("_Classes")


def _read_uninstall_keys() -> list[dict]:
    """Every Uninstall subkey carrying a DisplayName, as plain dicts.

    The keys are ``hive`` ("HKLM" | "HKU"), ``sid`` ("" for HKLM), ``key`` (the
    subkey name) and the nine values the item rules read. User hives that cannot
    be opened — unloaded, access denied — are skipped silently. Failing to open
    HKLM's 64-bit Uninstall key raises: that one is the whole machine-wide list,
    so its absence is an error rather than "nothing installed".

    HKCU is not read at all: the agent runs as a service account, so HKCU is
    that account's hive and not any person's.
    """
    winreg = _winreg()
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for path in (_UNINSTALL_64, _UNINSTALL_WOW):
        try:
            root = _reg_open(winreg, winreg.HKEY_LOCAL_MACHINE, path)
        except OSError as exc:
            if path == _UNINSTALL_64:
                raise RuntimeError(
                    f"HKLM Uninstall key unreadable: {exc}") from exc
            continue
        with root:
            for sub in _reg_subkeys(winreg, root):
                _read_uninstall_subkey(winreg, root, sub, "HKLM", "", rows, seen)

    try:
        users = _reg_open(winreg, winreg.HKEY_USERS, "")
    except OSError:
        return rows
    with users:
        for sid in _reg_subkeys(winreg, users):
            if not _is_user_sid(sid):
                continue
            try:
                hive = _reg_open(winreg, winreg.HKEY_USERS,
                                 f"{sid}\\{_UNINSTALL_USER}")
            except OSError:
                continue
            with hive:
                for sub in _reg_subkeys(winreg, hive):
                    _read_uninstall_subkey(winreg, hive, sub, "HKU", sid,
                                           rows, seen)
    return rows


def _read_uninstall_subkey(winreg, parent, sub: str, hive: str, sid: str,
                           rows: list[dict],
                           seen: set[tuple[str, str, str]]) -> None:
    try:
        handle = _reg_open(winreg, parent, sub)
    except OSError:
        return
    row = {"hive": hive, "sid": sid, "key": sub}
    with handle:
        row.update(_reg_values(winreg, handle, _UNINSTALL_VALUES))
    if not (row["DisplayName"] or "").strip():
        return
    # The same install can appear under both the 64-bit and WOW6432Node paths;
    # one key under one hive is one item.
    ident = (hive, sid, row["key"].lower())
    if ident in seen:
        return
    seen.add(ident)
    rows.append(row)


def _profile_list() -> dict[str, str]:
    """``{sid: account name}`` for every SID ProfileList knows about.

    Read in one pass rather than per row: the hive of a logged-out user is
    unloaded, and the account is still named in ProfileList.
    """
    winreg = _winreg()
    names: dict[str, str] = {}
    try:
        root = _reg_open(winreg, winreg.HKEY_LOCAL_MACHINE, _PROFILELIST)
    except OSError:
        return names
    with root:
        for sid in _reg_subkeys(winreg, root):
            try:
                handle = _reg_open(winreg, root, sid)
            except OSError:
                continue
            with handle:
                path = _reg_values(winreg, handle,
                                   ("ProfileImagePath",))["ProfileImagePath"]
            if not path:
                continue
            # ``C:\Users\alice`` → ``alice``.
            leaf = re.split(r"[\\/]", str(path).rstrip("\\/"))[-1].strip()
            if leaf:
                names[sid] = leaf
    return names


def _registry_row_is_application(row: dict) -> bool:
    """A product someone installed — not a component of one, not a hotfix."""
    return not (
        row.get("SystemComponent") == 1
        or (row.get("ParentKeyName") or "").strip()
        or (row.get("ReleaseType") or "").strip().lower() in _UPDATE_RELEASE_TYPES
    )


def _collect_registry(rows: list[dict], claimed: set[str]) -> list[dict]:
    """Uninstall rows no package manager represents, as unmanaged items."""
    usernames = _profile_list() if any(r["hive"] == "HKU" for r in rows) else {}
    items = []
    for row in rows:
        name = (row.get("DisplayName") or "").strip()
        if not name or not _registry_row_is_application(row):
            continue
        if name_key(name) in claimed:
            continue
        user = usernames.get(row["sid"], row["sid"]) if row["hive"] == "HKU" else ""
        items.append(_item(
            "registry", row["key"], name,
            (row.get("DisplayVersion") or "").strip(),
            (row.get("Publisher") or "").strip(),
            scope="machine" if row["hive"] == "HKLM" else "user", user=user,
            managed=False))
    return items


# ── Windows: payload ──────────────────────────────────────────────────────────

_WINDOWS_SOURCES = (("chocolatey", _collect_chocolatey),
                    ("scoop", _collect_scoop))

#: The executable each source needs on PATH (winget resolves separately).
_SOURCE_BINARIES = {"chocolatey": "choco", "scoop": "scoop"}


def _run_source(name: str, collect, items: list[dict],
                errors: dict[str, str]) -> None:
    """One source's items, or one error entry. Never propagates."""
    try:
        items.extend(collect())
    except (OSError, ValueError, RuntimeError, KeyError,
            subprocess.SubprocessError) as exc:
        logger.warning("software: %s collection failed: %s", name, exc)
        errors[name] = f"{name} failed: {str(exc)[:200]}"[:250]


def collect_windows() -> dict:
    """The server's software payload for a Windows host.

    Package managers first, then the registry: a registry entry whose display
    name a manager already reported is dropped, because the manager's item is
    the one Vigil can upgrade or remove. Everything else the registry knows is
    reported unmanaged.
    """
    items: list[dict] = []
    errors: dict[str, str] = {}

    # winget first and apart from the others: it is often off PATH (the agent
    # runs as a service account) yet installed, so it needs the resolver, and
    # on Server 2019 / LTSC it is absent — which is a skip, not an error.
    winget = _winget_binary()
    if winget:
        _run_source("winget", lambda: _collect_winget(winget, errors), items,
                    errors)
    for name, collect in _WINDOWS_SOURCES:
        if _which(_SOURCE_BINARIES[name]):
            _run_source(name, lambda collect=collect: collect(errors), items,
                        errors)

    try:
        rows = _read_uninstall_keys()
    except (OSError, RuntimeError) as exc:
        logger.warning("software: registry collection failed: %s", exc)
        errors["registry"] = f"registry failed: {str(exc)[:200]}"[:250]
        rows = []
    claimed = {name_key(item["name"]) for item in items}
    try:
        items.extend(_collect_registry(rows, claimed))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        logger.warning("software: registry collection failed: %s", exc)
        errors["registry"] = f"registry failed: {str(exc)[:200]}"[:250]

    return {"digest": digest(items), "collected_at": _collected_at(),
            "items": items, "errors": errors}


def collect() -> dict:
    """The server's software payload for this host."""
    if sys.platform == "win32":
        return collect_windows()
    return collect_linux()
