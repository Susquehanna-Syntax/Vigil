"""Fetch a stack's compose folder from a Git repository (2026.14.1).

The server — never a host — talks to the Git server, so a repository
credential stays in Vigil. A fetch resolves the branch (or the pinned tag or
commit) to one commit, fetches only that commit into a bare repository (so no
checkout, no hooks, no filters run), takes ``git archive`` of the folder that
holds the compose file, and repacks it as a tar.gz of **regular files only**.
The deploy task carries that archive's sha256; the agent checks it before it
places anything (agent/vigil_agent/actions/stacks.py).

What reaches git is narrow on purpose:

- URLs: ``https://host/path`` or SSH (``user@host:path`` / ``ssh://user@host/path``).
  No ``file://``, ``ext::``, local paths, embedded passwords, or anything
  starting with ``-``. ``GIT_ALLOW_PROTOCOL`` holds git itself to https and ssh,
  and HTTP redirects are not followed (a redirect could point anywhere).
- A host that resolves to loopback, link-local (cloud metadata) or an
  unspecified address is refused. A LAN Git server is fine.
- A token goes through GIT_ASKPASS from the environment — never a command line,
  which every user on the machine can read in /proc. An SSH key is used only
  with pinned host keys (StrictHostKeyChecking=yes, no global known_hosts).
- No user or system git config is read; time and size are capped.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

#: The packed archive a host downloads.
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
#: What `git archive` may produce before repacking (uncompressed).
MAX_TREE_BYTES = 100 * 1024 * 1024
MAX_FILES = 5000
TIMEOUT_SECONDS = 120

_HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
_PATH = r"[A-Za-z0-9._~/-]{1,400}"
_HTTPS_RE = re.compile(rf"^https://(?P<host>{_HOST})(?::(?P<port>[0-9]{{1,5}}))?/(?P<path>{_PATH})$")
_SCP_RE = re.compile(rf"^(?P<user>[A-Za-z0-9._-]{{1,64}})@(?P<host>{_HOST}):(?P<path>{_PATH})$")
_SSH_RE = re.compile(rf"^ssh://(?P<user>[A-Za-z0-9._-]{{1,64}})@(?P<host>{_HOST})"
                     rf"(?::(?P<port>[0-9]{{1,5}}))?/(?P<path>{_PATH})$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_COMPOSE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")


class GitSourceError(ValueError):
    """Why a repository could not be used — safe to show the admin."""


@dataclass
class Source:
    url: str
    branch: str = "main"
    pin: str = ""
    path: str = "compose.yaml"
    #: (kind, username, secret, known_hosts, git_host) or None.
    credential: tuple[str, str, str, str, str] | None = None


@dataclass
class Fetched:
    commit: str
    compose_yaml: str
    compose_file: str
    archive: bytes
    sha256: str


# ── Input checks ─────────────────────────────────────────────────────────


def parse_url(url: str) -> tuple[str, str]:
    """(kind, host) for an allowed URL — kind is "https" or "ssh"."""
    url = (url or "").strip()
    for kind, regex in (("https", _HTTPS_RE), ("ssh", _SCP_RE), ("ssh", _SSH_RE)):
        m = regex.match(url)
        if m:
            if ".." in m.group("path").split("/") or "//" in m.group("path"):
                break
            return kind, m.group("host").lower()
    raise GitSourceError(
        "the repository URL must be https://host/owner/repo or git@host:owner/repo "
        "(no passwords in the URL — use a Git credential)")


def check_ref(name: str, what: str) -> str:
    name = (name or "").strip()
    if (not _REF_RE.match(name) or name.startswith(("-", "/")) or name.endswith(("/", ".lock"))
            or ".." in name or "//" in name or "@{" in name):
        raise GitSourceError(f"{what} {name!r} is not a valid Git branch or tag name")
    return name


def check_path(path: str) -> tuple[str, str]:
    """(folder, compose file name) for the compose file's path in the repo."""
    path = (path or "").strip()
    if path.startswith("/"):
        raise GitSourceError("the compose path must be relative to the repository, e.g. deploy/compose.yaml")
    parts = path.split("/") if path else []
    if not parts or any(p in ("", ".", "..") for p in parts) or len(path) > 300:
        raise GitSourceError("the compose path must be a relative path such as deploy/compose.yaml")
    if not _COMPOSE_NAME_RE.match(parts[-1]):
        raise GitSourceError("the compose path must end in a .yaml or .yml file name")
    if not all(re.match(r"^[A-Za-z0-9._ -]{1,100}$", p) for p in parts):
        raise GitSourceError("the compose path may use letters, digits, '.', '_', '-' and spaces")
    return "/".join(parts[:-1]), parts[-1]


def _check_host_address(host: str, port: int) -> str:
    """Loopback, link-local (cloud metadata) and unspecified are refused —
    the server must not be talked into fetching from itself. Every address the
    name resolves to is checked, and the first is returned: git is then held
    to that address (``_pin_args``), so a DNS answer that changes between this
    check and git's own lookup (rebinding) cannot move it somewhere else."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GitSourceError(f"cannot resolve {host}") from exc
    if not infos:
        raise GitSourceError(f"cannot resolve {host}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        mapped = getattr(addr, "ipv4_mapped", None)
        for a in (addr, mapped) if mapped else (addr,):
            if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
                raise GitSourceError(f"{host} resolves to {a}, which Vigil will not fetch from")
    return infos[0][4][0].split("%", 1)[0]


def _pin_args(kind: str, host: str, port: int, ip: str) -> tuple[list[str], list[str]]:
    """(git -c args, ssh options) that make git connect to the checked *ip*
    while still verifying *host*'s TLS certificate or SSH host key."""
    if kind == "https":
        literal = f"[{ip}]" if ":" in ip else ip
        return ["-c", f"http.curloptResolve={host}:{port}:{literal}"], []
    return [], ["-o", f"HostName={ip}", "-o", f"HostKeyAlias={host}", "-o", f"Port={port}"]


# ── Running git ──────────────────────────────────────────────────────────

_GIT_CONFIG = (
    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
    "-c", "protocol.ssh.allow=always", "-c", "http.followRedirects=false",
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
    "-c", "credential.helper=", "-c", "submodule.recurse=false",
    # Keep what is fetched as one pack file, never unpacked into one loose
    # file per object: the per-file size cap (MAX_FETCH_BYTES) then bounds the
    # whole fetch, where a million small loose objects would each pass it.
    "-c", "fetch.unpackLimit=1", "-c", "transfer.unpackLimit=1",
)


def _env(tmp: str, source: Source, kind: str, ssh_pin: list[str] | None = None) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tmp,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:ssh",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "LC_ALL": "C",
    }
    cred = source.credential
    if kind == "https" and cred and cred[0] == "token":
        askpass = os.path.join(tmp, "askpass.sh")
        # Reads the values from its own environment: nothing secret is in a
        # file or on a command line.
        with open(askpass, "w") as fh:
            fh.write('#!/bin/sh\ncase "$1" in Username*) printf "%s" "$VIGIL_GIT_USER";; '
                     '*) printf "%s" "$VIGIL_GIT_TOKEN";; esac\n')
        os.chmod(askpass, 0o700)
        env.update(GIT_ASKPASS=askpass, VIGIL_GIT_USER=cred[1] or "x-access-token",
                   VIGIL_GIT_TOKEN=cred[2])
    if kind == "ssh":
        if not cred or cred[0] != "ssh_key":
            raise GitSourceError("an SSH repository needs an SSH deploy key credential")
        if not cred[3].strip():
            raise GitSourceError("the SSH credential has no known_hosts entry for the Git server")
        key, known = os.path.join(tmp, "id_key"), os.path.join(tmp, "known_hosts")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(cred[2].strip() + "\n")
        with open(known, "w") as fh:
            fh.write(cred[3].strip() + "\n")
        env["GIT_SSH_COMMAND"] = " ".join([
            "ssh", "-F", "/dev/null", "-i", shlex.quote(key), "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={shlex.quote(known)}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15", *(shlex.quote(a) for a in ssh_pin or ())])
    return env


#: The most any file git writes may grow to — the fetched pack, and the
#: captured output below. Past it the kernel stops git (SIGXFSZ), so a huge
#: repository cannot fill the server's disk or memory.
MAX_FETCH_BYTES = 512 * 1024 * 1024


def _git(args: list[str], env: dict, cwd: str | None = None, *, limit: int = 1_000_000,
         pin: list[str] | None = None, deadline: float | None = None) -> bytes:
    """Run git with output going to files, never into memory unbounded:
    ``prlimit --fsize`` caps every file the process writes (its pack and its
    stdout here), then stdout is read back only if it is within *limit*."""
    import time
    if deadline is not None and deadline - time.monotonic() <= 0:
        raise GitSourceError("the Git server did not answer in time")
    cmd = ["prlimit", f"--fsize={MAX_FETCH_BYTES}", "--", "git", *_GIT_CONFIG, *(pin or ()), *args]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err)
        except FileNotFoundError as exc:
            raise GitSourceError("git (and prlimit) must be installed on the Vigil server") from exc
        budget = TIMEOUT_SECONDS if deadline is None else min(TIMEOUT_SECONDS, deadline - time.monotonic())
        try:
            proc.wait(timeout=max(budget, 0.1))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise GitSourceError("the Git server did not answer in time") from None
        if proc.returncode in (-25, 128 + 25, 153):      # SIGXFSZ, however it is reported
            raise GitSourceError("the repository is too large for a stack")
        size = out.seek(0, os.SEEK_END)
        if size > limit:
            raise GitSourceError("the repository folder is too large for a stack")
        if proc.returncode != 0:
            # The last stderr line says what went wrong; the token is never in it
            # (it never was on a command line or in the URL).
            err.seek(max(0, err.seek(0, os.SEEK_END) - 4096))
            text = err.read().decode(errors="replace")
            # The size cap usually stops git's unpacking child, and the parent
            # then reports only that unpacking failed.
            if re.search(r"(unpack-objects|index-pack) failed|invalid index-pack output|File size limit", text):
                raise GitSourceError("the repository is too large for a stack (or its data could not be unpacked)")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            raise GitSourceError(f"git failed: {lines[-1][:300] if lines else 'unknown error'}")
        out.seek(0)
        return out.read(limit + 1)


def _resolve(url: str, source: Source, env: dict, pin_args: list[str], deadline: float | None = None) -> str:
    """The commit a fetch should take: the pin if it is a commit, else what
    the pinned tag or tracked branch points at now."""
    pin = (source.pin or "").strip()
    if pin and _SHA_RE.match(pin.lower()):
        return pin.lower()
    if pin:
        name = check_ref(pin, "pin")
        refs = [f"refs/tags/{name}", f"refs/tags/{name}^{{}}"]
    else:
        name = check_ref(source.branch or "main", "branch")
        refs = [f"refs/heads/{name}"]
    out = _git(["ls-remote", "--", url, *refs], env, pin=pin_args, deadline=deadline).decode(errors="replace")
    found = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        found[ref.strip()] = sha.strip().lower()
    # An annotated tag's ^{} line is the commit; a lightweight tag has none.
    for ref in reversed(refs):
        if _SHA_RE.match(found.get(ref, "")):
            return found[ref]
    raise GitSourceError(f"{'tag' if pin else 'branch'} {name!r} was not found in the repository")


# ── Repacking ────────────────────────────────────────────────────────────


def repack(raw_tar: bytes, folder: str) -> tuple[bytes, dict[str, bytes]]:
    """``git archive`` output → a deterministic tar.gz of *folder*'s regular
    files, paths relative to it. Symlinks, hard links, devices and anything
    else are refused rather than skipped: a stack that silently lost a file
    would fail in a confusing way on the host. Returns (archive, files)."""
    prefix = f"{folder}/" if folder else ""
    files: dict[str, bytes] = {}
    executable: set[str] = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as tar:
        for member in tar:
            name = member.name
            if prefix and not (name + "/").startswith(prefix):
                continue
            rel = name[len(prefix):].strip("/")
            if not rel:
                continue
            parts = rel.split("/")
            if name.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\\" in rel:
                raise GitSourceError(f"the repository has an unusable path: {name!r}")
            if member.isdir():
                continue
            if not member.isreg():
                raise GitSourceError(
                    f"{rel} is a symlink or special file — a Git stack may contain regular files only")
            total += member.size
            if len(files) >= MAX_FILES or total > MAX_TREE_BYTES:
                raise GitSourceError("the repository folder is too large for a stack")
            files[rel] = tar.extractfile(member).read()
            if member.mode & 0o111:
                executable.add(rel)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel in sorted(files):
                info = tarfile.TarInfo(rel)
                info.size = len(files[rel])
                info.mode = 0o755 if rel in executable else 0o644
                info.mtime = 0
                tar.addfile(info, io.BytesIO(files[rel]))
    archive = out.getvalue()
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise GitSourceError("the repository folder is too large for a stack")
    return archive, files


# ── The fetch ────────────────────────────────────────────────────────────

#: How long a fetch waits for a free slot before giving up.
LOCK_WAIT_SECONDS = 30
#: Fetches allowed at once on this server, across every worker. More than one,
#: so a single slow repository cannot hold up every Git stack; few, so the
#: size caps bound the disk a burst of requests can use.
FETCH_SLOTS = 2
#: The whole fetch — every git command in it — must finish within this.
FETCH_DEADLINE_SECONDS = 180


def _lock_dir() -> str:
    """A folder only this server's user can write: a lock file in the shared
    /tmp could be created first by anyone and held for ever."""
    path = os.path.join(tempfile.gettempdir(), f"vigil-git-locks-{os.getuid()}")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise GitSourceError(f"{path} is not a private folder of the Vigil server's user — refusing to use it")
    return path


@contextmanager
def _fetch_slot():
    """One of FETCH_SLOTS fetches at a time, across worker processes."""
    import fcntl
    import time

    folder = _lock_dir()
    fds = [os.open(os.path.join(folder, f"slot{i}.lock"),
                   os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
           for i in range(FETCH_SLOTS)]
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            for fd in fds:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    yield
                    return
                except BlockingIOError:
                    continue
            if time.monotonic() > deadline:
                raise GitSourceError("other Git fetches are running — try again in a moment")
            time.sleep(0.25)
    finally:
        for fd in fds:
            os.close(fd)        # releases whichever lock was held


def fetch(source: Source) -> Fetched:
    """Fetch, check and pack one commit's compose folder. Raises
    GitSourceError with a message fit for the admin."""
    url = (source.url or "").strip()
    kind, host = parse_url(url)
    folder, compose_file = check_path(source.path)
    port = 443 if kind == "https" else 22
    if (m := (_HTTPS_RE.match(url) or _SSH_RE.match(url))) and m.groupdict().get("port"):
        port = int(m.group("port"))
    if source.credential and source.credential[4].lower() != host:
        # A credential is for one Git server. Using it with any other would
        # hand a token to whoever runs that server.
        raise GitSourceError(f"that credential is for {source.credential[4]}, not {host}")
    ip = _check_host_address(host, port)
    git_pin, ssh_pin = _pin_args(kind, host, port, ip)

    import time
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    with _fetch_slot(), tempfile.TemporaryDirectory(prefix="vigil-git-") as tmp:
        os.chmod(tmp, 0o700)
        env = _env(tmp, source, kind, ssh_pin)
        commit = _resolve(url, source, env, git_pin, deadline)
        repo = os.path.join(tmp, "repo.git")
        _git(["init", "-q", "--bare", repo], env, deadline=deadline)
        _git(["fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules",
              "--", url, commit], env, cwd=repo, pin=git_pin, deadline=deadline)
        got = _git(["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
                   env, cwd=repo, deadline=deadline).decode().strip().lower()
        if got != commit:
            raise GitSourceError(f"the Git server returned {got[:12]} for {commit[:12]}")
        raw = _git(["archive", "--format=tar", commit, "--", folder or "."], env, cwd=repo, deadline=deadline,
                   limit=MAX_TREE_BYTES + MAX_FILES * 1024)

    archive, files = repack(raw, folder)
    if compose_file not in files:
        raise GitSourceError(f"{source.path} is not in the repository at {commit[:12]}")
    try:
        compose_yaml = files[compose_file].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitSourceError(f"{source.path} is not UTF-8 text") from exc
    return Fetched(commit=commit, compose_yaml=compose_yaml, compose_file=compose_file,
                   archive=archive, sha256=hashlib.sha256(archive).hexdigest())
