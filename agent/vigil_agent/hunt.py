"""The hunt framework: one result shape and one set of guardrails for every hunt.

Contract shared by all hunts (phases 02–05): every hunt returns JSON text with
exactly the keys ``matches`` (list of match dicts), ``truncated`` (bool) and
``duration`` (float seconds), and the handler exposes the declared outputs
``matched`` (bool), ``count`` (int) and ``truncated`` (bool). Hunts are
read-only, bounded (max_results + timeout) and run at low CPU/I/O priority.
"""

from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time

import psutil

from . import versions

logger = logging.getLogger("vigil.hunt")

DEFAULT_MAX_RESULTS = 500
MAX_RESULTS_CEILING = 5000
DEFAULT_TIMEOUT = 120      # seconds
TIMEOUT_CEILING = 600

_JSON_SAFE = (str, int, float, bool, type(None))


class HuntTimeout(Exception):
    """Raised by HuntResult.check_deadline() once the hunt's deadline passes."""


class HuntResult:
    """Accumulates hunt matches, enforces the result cap and the deadline."""

    def __init__(self, max_results: int, timeout: float):
        self.max_results = max_results
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.matches: list[dict] = []
        self.truncated = False
        self.timed_out = False
        self.started = time.monotonic()
        # Closed once run_hunt has taken the answer. A probe still running past
        # its deadline must not keep appending while the result is serialised.
        self._closed = False
        self._lock = threading.Lock()

    def add(self, evidence_type: str, **fields) -> bool:
        """Record a match. Returns False (and truncates) once max_results is reached."""
        match = {"evidence_type": evidence_type}
        for key, value in fields.items():
            match[key] = value if isinstance(value, _JSON_SAFE) else str(value)
        with self._lock:
            if self._closed:
                return False
            if len(self.matches) >= self.max_results:
                self.truncated = True
                return False
            self.matches.append(match)
            return True

    def check_deadline(self) -> None:
        """Raise HuntTimeout past the deadline; probes call this inside their loops."""
        if self._closed or time.monotonic() > self.deadline:
            raise HuntTimeout()

    def close(self) -> None:
        """Stop accepting matches; later add() returns False, check_deadline() raises."""
        with self._lock:
            self._closed = True

    def to_dict(self) -> dict:
        with self._lock:
            out = {
                "matches": list(self.matches),
                "truncated": self.truncated,
                "duration": round(time.monotonic() - self.started, 3),
            }
            if self.timed_out:
                out["timed_out"] = True
            return out


def limits_from(params: dict) -> tuple[int, int]:
    """(max_results, timeout) from params, clamped to the ceilings; ValueError if < 1."""
    try:
        max_results = int(params.get("max_results", DEFAULT_MAX_RESULTS))
        timeout = int(params.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_results and timeout must be integers") from exc
    if max_results < 1 or timeout < 1:
        raise ValueError("max_results and timeout must be >= 1")
    return min(max_results, MAX_RESULTS_CEILING), min(timeout, TIMEOUT_CEILING)


def run_hunt(probe, params: dict):
    """Run a probe in a low-priority worker thread and shape its result."""
    from .executor import ActionOutput

    max_results, timeout = limits_from(params)
    result = HuntResult(max_results, timeout)
    error_box: dict = {}

    def worker():
        _lower_thread_priority()
        try:
            probe(result, params)
        except HuntTimeout:
            result.truncated = True
            result.timed_out = True
        except BaseException as exc:  # noqa: BLE001 — surfaced as RuntimeError below
            error_box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout + 5)

    if thread.is_alive():
        # The probe ignored its deadline; keep what it found and stop it adding more.
        result.truncated = True
        result.timed_out = True
    result.close()

    probe_error = error_box.get("error")
    if probe_error is not None and probe_error.__class__ is not HuntTimeout:
        raise RuntimeError(f"hunt failed: {probe_error}") from probe_error

    result_dict = result.to_dict()
    count = len(result_dict["matches"])
    return ActionOutput(
        json.dumps(result_dict, sort_keys=True),
        {"matched": count > 0, "count": count, "truncated": result_dict["truncated"]},
    )


def default_scopes() -> list[str]:
    """Paths a default hunt scans: only the ones that exist on this host."""
    if sys.platform == "win32":
        scopes = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramData", r"C:\ProgramData"),
        ]
        users_dir = r"C:\Users"
        if os.path.isdir(users_dir):
            for user in sorted(os.listdir(users_dir)):
                base = os.path.join(users_dir, user)
                scopes.append(os.path.join(base, "AppData"))
                scopes.append(os.path.join(base, "Downloads"))
    else:
        scopes = ["/opt", "/usr/local", "/home", "/root", "/srv"]
    return [scope for scope in scopes if os.path.isdir(scope)]


def _lower_thread_priority() -> None:
    """Best effort: lower this thread's CPU/I/O priority. Never raises."""
    try:
        if sys.platform.startswith("linux"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            import psutil

            psutil.Process(threading.get_native_id()).ionice(psutil.IOPRIO_CLASS_IDLE)
        elif sys.platform == "win32":
            import ctypes

            THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
            kernel32 = ctypes.windll.kernel32
            current = kernel32.GetCurrentThread()
            kernel32.SetThreadPriority(current, THREAD_MODE_BACKGROUND_BEGIN)
        else:
            logger.debug("thread priority lowering not supported on %s", sys.platform)
    except Exception as exc:  # noqa: BLE001 — best effort by design (psutil.Error is not OSError)
        logger.debug("thread priority lowering skipped: %s", exc)


# ── hunt_file ────────────────────────────────────────────────────────────────

_POSIX_SKIP = ("/proc", "/sys", "/dev", "/run")
_HASH_CHUNK = 1024 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")
_MANIFEST_MAX = 64 * 1024
_ARCHIVE_EXTS = (".jar", ".war", ".ear")
_WINDOWS_VERSION_EXTS = (".exe", ".dll", ".sys")


def _full_scopes() -> list[str]:
    """Every root for scope: full — "/" on POSIX, each existing drive on Windows."""
    if sys.platform == "win32":
        import string
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
    return ["/"]


def _file_sha256(path: str, result: HuntResult) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
            result.check_deadline()
    return digest.hexdigest()


def hunt_file(result: HuntResult, params: dict) -> None:
    """Find files by name/glob and optional sha256, size and age. Read-only.

    Scope: ``paths`` (comma-separated roots) when given; otherwise ``scope:
    targeted`` (the default — install and home directories) or ``scope: full``
    (every filesystem root). Symlinks are never followed or reported, and
    /proc, /sys, /dev and /run are never entered. Hashing — the expensive part
    — only happens for files that already passed the name/size/age filters.
    """
    name = str(params.get("name") or "")
    want_sha = str(params.get("sha256") or "").lower()
    if not name and not want_sha:
        raise ValueError("hunt_file needs name or sha256")
    if want_sha and not _SHA256_HEX.match(want_sha):
        raise ValueError(f"sha256 must be 64 hex characters, got {want_sha!r}")
    include_hash = bool(params.get("hash")) or bool(want_sha)
    version_bounds = {key: str(params[key]) for key in
                      ("version_lt", "version_lte", "version_gt", "version_gte",
                       "version_eq")
                      if params.get(key) not in (None, "")}
    min_size = int(params["min_size"]) if params.get("min_size") not in (None, "") else None
    max_size = int(params["max_size"]) if params.get("max_size") not in (None, "") else None
    now = time.time()
    newer_than = (now - float(params["modified_within_days"]) * 86400
                  if params.get("modified_within_days") not in (None, "") else None)
    older_than = (now - float(params["older_than_days"]) * 86400
                  if params.get("older_than_days") not in (None, "") else None)
    scope = str(params.get("scope") or "targeted").lower()
    if scope not in ("targeted", "full"):
        raise ValueError("scope must be targeted or full")

    if params.get("paths"):
        roots = [p.strip() for p in str(params["paths"]).split(",") if p.strip()]
    else:
        roots = _full_scopes() if scope == "full" else default_scopes()
    windows = sys.platform == "win32"
    pattern = name.lower() if windows else name

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            result.check_deadline()
            if not windows:
                dirnames[:] = [d for d in dirnames
                               if os.path.join(dirpath, d) not in _POSIX_SKIP]
            for fname in filenames:
                if name and not fnmatch.fnmatchcase(fname.lower() if windows else fname, pattern):
                    continue
                path = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue
                if min_size is not None and st.st_size < min_size:
                    continue
                if max_size is not None and st.st_size > max_size:
                    continue
                if newer_than is not None and st.st_mtime < newer_than:
                    continue
                if older_than is not None and st.st_mtime > older_than:
                    continue
                sha = None
                if include_hash:
                    try:
                        sha = _file_sha256(path, result)
                    except OSError:
                        continue
                    if want_sha and sha != want_sha:
                        continue
                version = None
                if version_bounds:
                    version = _file_version(path)
                    if version is None:
                        continue
                    if not all(
                            _version_bound_holds(version, key, bound, "generic")
                            for key, bound in version_bounds.items()):
                        continue
                modified = datetime.datetime.fromtimestamp(
                    st.st_mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if not result.add("file", path=path, size=st.st_size,
                                  modified=modified, sha256=sha, version=version):
                    return


# ── hunt_package ──────────────────────────────────────────────────────────────

#: (manager, argv, version scheme) — one listing call per manager.
_PACKAGE_COMMANDS = {
    "dpkg": (["dpkg-query", "-W", "-f=${Package}\t${Version}\n"], "deb"),
    "rpm": (["rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n"], "rpm"),
    "pacman": (["pacman", "-Q"], "rpm"),
    "brew": (["brew", "list", "--versions"], "generic"),
    "snap": (["snap", "list"], "generic"),
}

#: Discovery order when no manager override is given.
_MANAGER_ORDER = ("dpkg", "rpm", "pacman", "brew", "snap")


def _parse_listing(manager: str, lines: list[str]) -> list[tuple[str, str]]:
    """Turn raw listing output into (name, version) pairs."""
    if manager == "dpkg" or manager == "rpm":
        return [(l.split("\t", 1)[0], l.split("\t", 1)[1]) for l in lines if "\t" in l]
    # Whitespace-separated (snap pads its columns with runs of spaces).
    rows = [line.split() for line in lines]
    if manager == "pacman":
        return [(r[0], r[1]) for r in rows if len(r) >= 2]
    if manager == "brew":
        # name + last token (a formula may list several installed versions).
        return [(r[0], r[-1]) for r in rows if len(r) >= 2]
    # snap: skip the header line.
    return [(r[0], r[1]) for r in rows[1:] if len(r) >= 2]


def _list_packages(manager: str) -> list[tuple[str, str]]:
    """One listing call for a named manager; returns (name, version) pairs.

    Module-level so tests can patch it (the package commands are refused in
    tests by agent/tests/__init__.py).
    """
    argv, _scheme = _PACKAGE_COMMANDS[manager]
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=60, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"package listing failed ({argv[0]}): "
                           f"{(proc.stderr or '').strip()[:200]}")
    return _parse_listing(manager, proc.stdout.splitlines())


def _pick_manager(manager: str | None) -> str:
    if manager is not None:
        if manager not in _PACKAGE_COMMANDS:
            raise ValueError(f"unsupported package manager: {manager!r}")
        return manager
    for candidate in _MANAGER_ORDER:
        binary = {"dpkg": "dpkg-query"}.get(candidate, candidate)
        if shutil.which(binary) is not None:
            return candidate
    raise ValueError("no supported package manager on this host")


def _file_version(path: str) -> str | None:
    """Best-effort version of a file, or None when it cannot be determined.

    Tries, in order: a JAR/WAR/EAR's MANIFEST.MF (Implementation-Version,
    Bundle-Version, Specification-Version), a Windows PE version resource
    (VS_FIXEDFILEINFO as a.b.c.d), then the first dotted-numeric token in the
    file name (so log4j-core-2.14.1.jar reads as 2.14.1). Never raises: a
    file whose version cannot be read simply returns None.
    """
    base = os.path.basename(path)
    try:
        lower = base.lower()
        if lower.endswith(_ARCHIVE_EXTS):
            version = _jar_manifest_version(path)
            if version:
                return version
        if sys.platform == "win32" and lower.endswith(_WINDOWS_VERSION_EXTS):
            version = _windows_version(path)
            if version:
                return version
        match = _VERSION_RE.search(base)
        return match.group(1) if match else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _jar_manifest_version(path: str) -> str | None:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            try:
                data = archive.read("META-INF/MANIFEST.MF")[:_MANIFEST_MAX]
            except KeyError:
                return None
    except (OSError, zipfile.BadZipFile):
        return None
    text = data.decode("utf-8", errors="replace")
    for key in ("Implementation-Version", "Bundle-Version",
                "Specification-Version"):
        for line in text.splitlines():
            if line.lower().startswith(key.lower() + ":"):
                value = line.split(":", 1)[1].strip()
                return value or None
    return None


def _windows_version(path: str) -> str | None:
    import ctypes

    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, None, size, buffer):
            return None
        value = ctypes.c_void_p()
        length = ctypes.c_uint()
        # A plain str goes to a W function as a wide string: "\\" is the
        # root block, i.e. VS_FIXEDFILEINFO.
        ok = ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(length))
        if not ok or not length.value:
            return None
        fixed = ctypes.cast(value, ctypes.POINTER(
            ctypes.c_uint32 * 4)).contents  # VS_FIXEDFILEINFO
        ms, ls = fixed[0], fixed[1]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _version_bound_holds(version: str, key: str, bound: str, scheme: str) -> bool:
    c = versions.compare(version, bound, scheme)
    if key == "version_lt":
        return c < 0
    if key == "version_lte":
        return c <= 0
    if key == "version_gt":
        return c > 0
    if key == "version_gte":
        return c >= 0
    return c == 0  # version_eq


def hunt_package(result: HuntResult, params: dict) -> None:
    """List installed packages matching a name (exact or glob) and version bounds.

    Versions compare with the package system's own rules: dpkg for Debian,
    rpmvercmp for rpm and pacman, dotted-numeric elsewhere.
    """
    name = params.get("name")
    if name is None or not str(name).strip():
        raise ValueError("hunt_package needs name")

    manager = _pick_manager(params.get("manager"))
    packages = _list_packages(manager)
    _argv, scheme = _PACKAGE_COMMANDS[manager]

    bounds = [(key, str(params[key])) for key in
              ("version_lt", "version_lte", "version_gt", "version_gte", "version_eq")
              if params.get(key) not in (None, "")]

    for i, (pkg_name, version) in enumerate(packages):
        if i % 500 == 0:
            result.check_deadline()
        if not fnmatch.fnmatch(pkg_name, str(name)):
            continue
        if not all(_version_bound_holds(version, key, bound, scheme)
                   for key, bound in bounds):
            continue
        if not result.add("package", name=pkg_name, version=version,
                          manager=manager):
            return


# ── hunt_process / hunt_port / hunt_service ──────────────────────────────────

_CMDLINE_CUT = 500


def hunt_process(result: HuntResult, params: dict) -> None:
    """List running processes matching a name glob, cmdline substring or user."""
    name = str(params.get("name") or "")
    cmdline = str(params.get("cmdline") or "")
    user = str(params.get("user") or "")
    if not (name or cmdline or user):
        raise ValueError("hunt_process needs name, cmdline or user")
    cmdline_lower = cmdline.lower()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "username",
                                     "create_time"]):
        result.check_deadline()
        try:
            info = proc.info
            if name and not fnmatch.fnmatch(info.get("name") or "", name):
                continue
            joined = " ".join(info.get("cmdline") or [])
            if cmdline and cmdline_lower not in joined.lower():
                continue
            if user and info.get("username") != user:
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        started = ""
        if info.get("create_time"):
            started = datetime.datetime.fromtimestamp(
                info["create_time"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not result.add("process",
                          pid=info.get("pid"),
                          name=info.get("name"),
                          cmdline=joined[:_CMDLINE_CUT],
                          user=info.get("username"),
                          started=started):
            return


def hunt_port(result: HuntResult, params: dict) -> None:
    """List local socket bindings matching a port (or range), protocol and process."""
    process = str(params.get("process") or "")
    if not process and params.get("port") in (None, ""):
        raise ValueError("hunt_port needs port or process")

    want_protocol = str(params.get("protocol") or "").lower()
    if want_protocol not in ("", "tcp", "udp"):
        raise ValueError("protocol must be tcp or udp")

    port = params.get("port")
    if port not in (None, ""):
        text = str(port).strip()
        if "-" in text:
            low_s, _, high_s = text.partition("-")
            low, high = int(low_s), int(high_s)
        else:
            low = high = int(text)
        if not (0 <= low <= 65535 and 0 <= high <= 65535) or low > high:
            raise ValueError(f"port out of range: {port!r}")
    else:
        low, high = 0, 65535

    pids = {c.pid for c in psutil.net_connections(kind="inet") if c.pid is not None}
    names: dict = {}
    for pid in pids:
        try:
            names[pid] = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            names[pid] = None

    seen: set[tuple] = set()
    for conn in psutil.net_connections(kind="inet"):
        result.check_deadline()
        proto = "udp" if conn.type == socket.SOCK_DGRAM else "tcp"
        if want_protocol and proto != want_protocol:
            continue
        if proto == "tcp" and conn.status != psutil.CONN_LISTEN:
            continue
        if conn.laddr.port < low or conn.laddr.port > high:
            continue
        name = names.get(conn.pid)
        if process and (name is None or not fnmatch.fnmatch(name, process)):
            continue
        key = (conn.laddr.port, proto, conn.laddr.ip, conn.pid)
        if key in seen:
            continue
        seen.add(key)
        if not result.add("port", port=conn.laddr.port, protocol=proto,
                          address=conn.laddr.ip, pid=conn.pid, process=name):
            return


def _systemd_services() -> list[dict]:
    """systemctl state and unit-file state for every service on this host."""
    units = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-legend",
         "--plain"], capture_output=True, text=True, check=False)
    files = subprocess.run(
        ["systemctl", "list-unit-files", "--type=service", "--no-legend"],
        capture_output=True, text=True, check=False)
    if units.returncode != 0:
        raise RuntimeError(f"systemctl list-units failed: "
                           f"{(units.stderr or '').strip()[:200]}")
    if files.returncode != 0:
        raise RuntimeError(f"systemctl list-unit-files failed: "
                           f"{(files.stderr or '').strip()[:200]}")
    state: dict[str, dict] = {}
    for line in units.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            state[cols[0]] = {"state": "running" if cols[3] == "running" else "stopped"}
    for line in files.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 2:
            entry = state.setdefault(cols[0], {"state": "stopped"})
            entry["unit_state"] = cols[1]
    return [{"unit": unit, **state[unit]} for unit in state]


def _linux_start_mode(unit_state: str | None) -> str | None:
    if not unit_state:
        return None
    if unit_state.startswith("enabled"):
        return "enabled"
    if unit_state == "disabled":
        return "disabled"
    return None  # static, masked, indirect, … match only when start_mode is omitted


def hunt_service(result: HuntResult, params: dict) -> None:
    """List services matching a name glob and optional state / start mode."""
    name = params.get("name")
    if name is None or not str(name).strip():
        raise ValueError("hunt_service needs name")

    want_state = str(params.get("state") or "")
    if want_state not in ("", "running", "stopped"):
        raise ValueError("state must be running or stopped")
    want_mode = str(params.get("start_mode") or "")
    if want_mode not in ("", "enabled", "disabled"):
        raise ValueError("start_mode must be enabled or disabled")

    if sys.platform == "win32":
        services = [
            {"name": s.name,
             "state": "running" if s.status == psutil.STATUS_RUNNING else "stopped",
             "start_mode": {"automatic": "enabled", "disabled": "disabled"}.get(
                 s.start_type)}
            for s in psutil.win_service_iter()
        ]
    else:
        services = [
            {"name": entry["unit"].removesuffix(".service"),
             "state": entry["state"],
             "start_mode": _linux_start_mode(entry.get("unit_state"))}
            for entry in _systemd_services()
        ]

    for svc in services:
        result.check_deadline()
        if not fnmatch.fnmatch(svc["name"], str(name)):
            continue
        if want_state and svc["state"] != want_state:
            continue
        if want_mode and svc["start_mode"] != want_mode:
            continue
        if not result.add("service", name=svc["name"], state=svc["state"],
                          start_mode=svc["start_mode"]):
            return


# ── hunt_registry ─────────────────────────────────────────────────────────────

#: HK root name -> HKEY constant attribute on the winreg module.
_HK_ROOTS = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "HKU": "HKEY_USERS",
}

#: REG_* constant value -> name, for the reported value type.
_REG_TYPE_NAMES = {
    0: "REG_NONE", 1: "REG_SZ", 2: "REG_EXPAND_SZ", 3: "REG_BINARY",
    4: "REG_DWORD", 5: "REG_DWORD_BIG_ENDIAN", 6: "REG_LINK", 7: "REG_MULTI_SZ",
    8: "REG_RESOURCE_LIST", 9: "REG_FULL_RESOURCE_DESCRIPTOR",
    10: "REG_RESOURCE_REQUIREMENTS_LIST", 11: "REG_QWORD",
}

_DATA_CUT = 500


def _reg_type_name(value_type: int) -> str:
    return _REG_TYPE_NAMES.get(value_type, f"REG_{value_type}")


def _reg_data_as_text(data) -> str:
    """A registry value's data as displayable text, whatever type it is."""
    if data is None:
        return ""
    if isinstance(data, bytes):  # REG_BINARY
        return data.hex()
    if isinstance(data, (list, tuple)):  # REG_MULTI_SZ
        return " ".join(str(d) for d in data)
    return str(data)


def _hunt_registry_key(winreg, result: HuntResult, params: dict,
                       root_attr: str, key_path: str, key_display: str,
                       value_glob: str, want_data: str, view: int) -> None:
    """Enumerate one key's values (and, without a value filter, the key itself)."""
    hkey = getattr(winreg, root_attr)
    access = winreg.KEY_READ | view
    try:
        handle = winreg.OpenKey(hkey, key_path, 0, access)
    except OSError:
        return
    try:
        if not value_glob and not want_data:
            result.add("registry", key=key_display, value=None, data=None,
                       type="REG_KEY")
            return
        glob = value_glob or "*"
        index = 0
        while True:
            try:
                # (name, data, type) — the data comes with the enumeration.
                name, data, vtype = winreg.EnumValue(handle, index)
            except OSError:
                break  # no more values
            index += 1
            if not fnmatch.fnmatch(name, glob):
                continue
            text = _reg_data_as_text(data)
            if want_data and want_data.lower() not in text.lower():
                continue
            if not result.add("registry", key=key_display, value=name,
                              data=text[:_DATA_CUT],
                              type=_reg_type_name(vtype)):
                return
    finally:
        winreg.CloseKey(handle)


def _hunt_registry_expand(winreg, result: HuntResult, params: dict,
                          root_attr: str, key_path: str, key_display: str,
                          value_glob: str, want_data: str, view: int) -> None:
    """key with a trailing wildcard: visit each direct subkey (not the key itself)."""
    try:
        parent = winreg.OpenKey(getattr(winreg, root_attr), key_path, 0,
                                winreg.KEY_READ | view)
    except OSError:
        return
    try:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(parent, index)
            except OSError:
                break
            index += 1
            result.check_deadline()
            if result.truncated:
                break
            sub_path = f"{key_path}\\{sub}"
            _hunt_registry_key(winreg, result, params, root_attr, sub_path,
                               f"{key_display}\\{sub}", value_glob, want_data,
                               view)
    finally:
        winreg.CloseKey(parent)


def hunt_registry(result: HuntResult, params: dict) -> None:
    """Find Windows registry keys and values.

    ``key`` is the path to open, e.g. ``HKLM\\SOFTWARE\\Microsoft\\Windows\\
    CurrentVersion\\Uninstall``; a final ``\\*`` means the key itself plus
    each direct subkey. ``value`` is a glob on value names (no filter: the
    matching keys are reported with value=None). ``data`` is a
    case-insensitive substring of the value's data as text. ``view`` is 64
    (default) or 32. Missing keys and access errors are skipped, not errors.
    """
    if sys.platform != "win32":
        raise ValueError("hunt_registry runs on Windows only")

    key = params.get("key")
    if key is None or not str(key).strip():
        raise ValueError("hunt_registry needs key")
    key = str(key).strip()

    value_glob = str(params.get("value") or "")
    want_data = str(params.get("data") or "")
    view = str(params.get("view") or "64").strip()
    if view not in ("64", "32"):
        raise ValueError("view must be 64 or 32")

    import winreg

    root, sep, rest = key.partition("\\")
    root_attr = _HK_ROOTS.get(root.upper())
    if not root_attr or not sep or not rest:
        raise ValueError(
            f"key must start with an HKLM\\, HKCU\\ or HKU\\ root, got {key!r}")
    view_flag = winreg.KEY_WOW64_64KEY if view == "64" else winreg.KEY_WOW64_32KEY

    result.check_deadline()
    if rest.endswith("\\*"):
        base = rest[:-2] or "\\"
        _hunt_registry_expand(winreg, result, params, root_attr, base,
                              key[:-2], value_glob, want_data, view_flag)
    else:
        _hunt_registry_key(winreg, result, params, root_attr, rest, key,
                           value_glob, want_data, view_flag)


# ── hunt_content ──────────────────────────────────────────────────────────────

_DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024
_MAX_FILE_SIZE_CEILING = 100 * 1024 * 1024
_BINARY_PEEK = 8192
_LINE_SCAN_LIMIT = 4096
_LINES_REPORTED = 50
_TEXT_REPORTED = 20
_TEXT_CUT = 200


def _is_binary(fh) -> bool:
    """True when the first 8 KiB contains a NUL byte."""
    head = fh.read(_BINARY_PEEK)
    return b"\x00" in head


def _hunt_content_file(path: str, pattern, name_glob: str, max_file_size: int,
                       want_return: str, result: HuntResult) -> bool:
    """Scan one file; False once the result is full and the walk should stop."""
    try:
        size = os.lstat(path).st_size
    except OSError:
        return True
    if size > max_file_size:
        return True
    try:
        with open(path, "rb") as fh:
            if _is_binary(fh):
                return True
            fh.seek(0)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return True

    match_count = 0
    matched_lines: list[int] = []  # each line once, however many hits it has
    matched_texts: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if lineno % 1000 == 0:
            result.check_deadline()
        hits = 0
        for m in pattern.finditer(line[:_LINE_SCAN_LIMIT]):
            hits += 1
            if want_return == "text" and len(matched_texts) < _TEXT_REPORTED:
                matched_texts.append(m.group(0)[:_TEXT_CUT])
        if hits:
            match_count += hits
            matched_lines.append(lineno)
    if not match_count:
        return True
    return result.add(
        "content",
        path=path,
        match_count=match_count,
        lines=",".join(str(n) for n in matched_lines[:_LINES_REPORTED])
        if want_return in ("lines", "text") else None,
        text=" | ".join(matched_texts[:_TEXT_REPORTED])
        if want_return == "text" else None,
    )


def hunt_content(result: HuntResult, params: dict) -> None:
    """Find regex matches inside files.

    By default a match reports the file, the number of matches and on which
    lines they sit; ``return: text`` also carries the matched substrings,
    which is why the server counts that variant as high risk. Binaries (a NUL
    byte in the first 8 KiB) are skipped, files are read as UTF-8 with
    replacement, at most the first 4,096 characters of each line are scanned,
    and the deadline is checked every 1,000 lines.
    """
    pattern_raw = params.get("pattern")
    if pattern_raw is None or not str(pattern_raw).strip():
        raise ValueError("hunt_content needs pattern")
    try:
        pattern = re.compile(str(pattern_raw))
    except re.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc

    name = str(params.get("name") or "*")
    want_return = str(params.get("return") or "lines").lower()
    if want_return not in ("match", "lines", "text"):
        raise ValueError("return must be match, lines or text")

    max_file_size = int(params.get("max_file_size")
                        or _DEFAULT_MAX_FILE_SIZE)
    if max_file_size < 1:
        raise ValueError("max_file_size must be >= 1")
    max_file_size = min(max_file_size, _MAX_FILE_SIZE_CEILING)

    scope = str(params.get("scope") or "targeted").lower()
    if scope not in ("targeted", "full"):
        raise ValueError("scope must be targeted or full")

    if params.get("paths"):
        roots = [p.strip() for p in str(params["paths"]).split(",") if p.strip()]
    else:
        roots = _full_scopes() if scope == "full" else default_scopes()

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            result.check_deadline()
            if sys.platform != "win32":
                dirnames[:] = [d for d in dirnames
                               if os.path.join(dirpath, d) not in _POSIX_SKIP]
            for fname in filenames:
                if not fnmatch.fnmatchcase(fname, name):
                    continue
                path = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue
                if not _hunt_content_file(path, pattern, name, max_file_size,
                                          want_return, result):
                    return
