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
    #: (kind, username, secret, known_hosts) or None.
    credential: tuple[str, str, str, str] | None = None


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


def _check_host_address(host: str, port: int) -> None:
    """Loopback, link-local (cloud metadata) and unspecified are refused —
    the server must not be talked into fetching from itself. Every address the
    name resolves to is checked."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GitSourceError(f"cannot resolve {host}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        mapped = getattr(addr, "ipv4_mapped", None)
        for a in (addr, mapped) if mapped else (addr,):
            if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
                raise GitSourceError(f"{host} resolves to {a}, which Vigil will not fetch from")


# ── Running git ──────────────────────────────────────────────────────────

_GIT_CONFIG = (
    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
    "-c", "protocol.ssh.allow=always", "-c", "http.followRedirects=false",
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
    "-c", "credential.helper=", "-c", "submodule.recurse=false",
)


def _env(tmp: str, source: Source, kind: str) -> dict:
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
            "-o", "ConnectTimeout=15"])
    return env


def _git(args: list[str], env: dict, cwd: str | None = None, *, limit: int = 1_000_000) -> bytes:
    try:
        proc = subprocess.Popen(["git", *_GIT_CONFIG, *args], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise GitSourceError("git is not installed on the Vigil server") from exc
    try:
        out, err = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise GitSourceError("the Git server did not answer in time") from None
    if len(out) > limit:
        raise GitSourceError("the repository folder is too large for a stack")
    if proc.returncode != 0:
        # The last stderr line says what went wrong; the token is never in it
        # (it never was on a command line or in the URL).
        lines = [ln for ln in err.decode(errors="replace").splitlines() if ln.strip()]
        raise GitSourceError(f"git failed: {lines[-1][:300] if lines else 'unknown error'}")
    return out


def _resolve(url: str, source: Source, env: dict) -> str:
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
    out = _git(["ls-remote", "--", url, *refs], env).decode(errors="replace")
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


def fetch(source: Source) -> Fetched:
    """Fetch, check and pack one commit's compose folder. Raises
    GitSourceError with a message fit for the admin."""
    url = (source.url or "").strip()
    kind, host = parse_url(url)
    folder, compose_file = check_path(source.path)
    port = 443 if kind == "https" else 22
    if (m := (_HTTPS_RE.match(url) or _SSH_RE.match(url))) and m.groupdict().get("port"):
        port = int(m.group("port"))
    _check_host_address(host, port)

    with tempfile.TemporaryDirectory(prefix="vigil-git-") as tmp:
        os.chmod(tmp, 0o700)
        env = _env(tmp, source, kind)
        commit = _resolve(url, source, env)
        repo = os.path.join(tmp, "repo.git")
        _git(["init", "-q", "--bare", repo], env)
        _git(["fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules",
              "--", url, commit], env, cwd=repo)
        got = _git(["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
                   env, cwd=repo).decode().strip().lower()
        if got != commit:
            raise GitSourceError(f"the Git server returned {got[:12]} for {commit[:12]}")
        raw = _git(["archive", "--format=tar", commit, "--", folder or "."], env, cwd=repo,
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
