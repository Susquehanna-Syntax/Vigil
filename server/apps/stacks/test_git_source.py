"""Stacks from Git (2026.14.1): the fetcher's input checks, a real fetch from
a local repository, and the API around it.

Production git may speak only https and ssh, so the fetch tests open the
``file`` protocol for one local repository through ``_local_git`` — nothing
else in the module changes.
"""

import gzip
import hashlib
import io
import os
import socket
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from . import gitsource
from .gitsource import GitSourceError, Source

_TOTP = "apps.accounts.totp.require_totp_confirmation"

COMPOSE = "services:\n  app:\n    build: ./app\n    ports: ['8080:80']\n"


def _run(repo, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repo(root: Path) -> tuple[str, str]:
    """A repo with deploy/compose.yaml, its build folder, an executable and a
    file outside the folder. Returns (path, head commit)."""
    repo = root / "repo"
    (repo / "deploy" / "app").mkdir(parents=True)
    (repo / "deploy" / "compose.yaml").write_text(COMPOSE)
    (repo / "deploy" / "app" / "Dockerfile").write_text("FROM nginx\n")
    (repo / "deploy" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (repo / "deploy" / "run.sh").chmod(0o755)
    (repo / "README.md").write_text("not part of the stack\n")
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "first")
    _run(repo, "tag", "-a", "v1", "-m", "release 1")
    return str(repo), _run(repo, "rev-parse", "HEAD")


@contextmanager
def _local_git():
    """Let the fetcher reach a local repository: allow git's file protocol and
    skip the URL/host checks (which are tested on their own below)."""
    real_env = gitsource._env

    def env(tmp, source, kind):
        e = real_env(tmp, source, kind)
        e["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
        return e
    with mock.patch.object(gitsource, "parse_url", return_value=("https", "git.test")), \
            mock.patch.object(gitsource, "_check_host_address"), \
            mock.patch.object(gitsource, "_env", side_effect=env), \
            mock.patch.object(gitsource, "_GIT_CONFIG", gitsource._GIT_CONFIG + ("-c", "protocol.file.allow=always")):
        yield


def _members(archive: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive)), mode="r:") as tar:
        return {m.name: (m.mode, tar.extractfile(m).read()) for m in tar if m.isreg()}


class InputCheckTests(SimpleTestCase):
    def test_only_https_and_ssh_urls(self):
        for good in ("https://github.com/acme/app.git", "https://git.lan:3000/acme/app",
                     "git@github.com:acme/app.git", "ssh://git@git.lan:2222/acme/app.git"):
            with self.subTest(good=good):
                gitsource.parse_url(good)
        for bad in ("file:///etc", "/srv/repo", "ext::sh -c id", "https://user:pw@github.com/a/b",
                    "-uhttps://x/y", "http://github.com/a/b", "git://github.com/a/b",
                    "https://github.com/a/../b", "https://github.com/a/b?x=1", "--upload-pack=id",
                    "git@github.com:-oProxyCommand=id", ""):
            with self.subTest(bad=bad), self.assertRaises(GitSourceError):
                gitsource.parse_url(bad)

    def test_ref_and_path_checks(self):
        for bad in ("-x", "a..b", "a@{1}", "x.lock", "/main", "main/", ""):
            with self.subTest(ref=bad), self.assertRaises(GitSourceError):
                gitsource.check_ref(bad, "branch")
        self.assertEqual(gitsource.check_path("deploy/compose.yaml"), ("deploy", "compose.yaml"))
        self.assertEqual(gitsource.check_path("docker-compose.yml"), ("", "docker-compose.yml"))
        for bad in ("../x.yaml", "/abs.yaml", "deploy/notes.txt", "", "a/./b.yaml"):
            with self.subTest(path=bad), self.assertRaises(GitSourceError):
                gitsource.check_path(bad)

    def test_the_server_will_not_fetch_from_itself_or_metadata(self):
        def resolving(ip):
            return mock.patch.object(socket, "getaddrinfo",
                                     return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))])
        for ip in ("127.0.0.1", "169.254.169.254", "0.0.0.0"):
            with self.subTest(ip=ip), resolving(ip), self.assertRaises(GitSourceError):
                gitsource._check_host_address("git.example", 443)
        with resolving("10.0.0.5"):
            gitsource._check_host_address("gitea.lan", 443)   # a LAN Git server is fine


class FetchTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.head = _make_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _fetch(self, **kw):
        with _local_git():
            return gitsource.fetch(Source(url=self.repo, path="deploy/compose.yaml", **kw))

    def test_a_branch_fetch_packs_only_the_compose_folder(self):
        got = self._fetch(branch="main")
        self.assertEqual(got.commit, self.head)
        self.assertEqual(got.compose_yaml, COMPOSE)
        self.assertEqual(got.compose_file, "compose.yaml")
        self.assertEqual(got.sha256, hashlib.sha256(got.archive).hexdigest())
        files = _members(got.archive)
        self.assertEqual(sorted(files), ["app/Dockerfile", "compose.yaml", "run.sh"])
        self.assertEqual(files["run.sh"][0], 0o755)
        self.assertEqual(files["compose.yaml"][0], 0o644)
        self.assertEqual(self._fetch(branch="main").sha256, got.sha256, "the archive is deterministic")

    def test_pins_hold_while_the_branch_moves(self):
        Path(self.repo, "deploy", "compose.yaml").write_text(COMPOSE + "  # changed\n")
        _run(self.repo, "commit", "-q", "-am", "second")
        new_head = _run(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self._fetch(branch="main").commit, new_head)
        self.assertEqual(self._fetch(pin="v1").commit, self.head)            # annotated tag → its commit
        self.assertEqual(self._fetch(pin=self.head).commit, self.head)        # an exact commit
        self.assertEqual(self._fetch(pin="v1").compose_yaml, COMPOSE)

    def test_a_symlink_in_the_folder_is_refused(self):
        os.symlink("/etc/passwd", Path(self.repo, "deploy", "passwd"))
        _run(self.repo, "add", ".")
        _run(self.repo, "commit", "-q", "-m", "link")
        with self.assertRaisesRegex(GitSourceError, "regular files only"):
            self._fetch(branch="main")

    def test_missing_branch_and_missing_compose_file(self):
        with self.assertRaisesRegex(GitSourceError, "not found"):
            self._fetch(branch="nope")
        with _local_git(), self.assertRaisesRegex(GitSourceError, "not in the repository"):
            gitsource.fetch(Source(url=self.repo, path="deploy/other.yaml"))

    def test_a_token_is_never_on_a_command_line(self):
        seen = []
        real = subprocess.Popen

        def spy(args, **kw):
            seen.append((args, kw.get("env") or {}))
            return real(args, **kw)
        with mock.patch.object(gitsource.subprocess, "Popen", side_effect=spy):
            self._fetch(branch="main", credential=("token", "bot", "s3cr3t-token", ""))
        self.assertTrue(seen)
        for args, env in seen:
            self.assertNotIn("s3cr3t-token", " ".join(args))
            self.assertEqual(env["VIGIL_GIT_TOKEN"], "s3cr3t-token")
            self.assertTrue(env["GIT_ASKPASS"].endswith("askpass.sh"))
            self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https:ssh:file")   # file only in this test

    def test_ssh_needs_a_key_and_pinned_host_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Source(url="git@github.com:a/b.git")
            with self.assertRaises(GitSourceError):
                gitsource._env(tmp, src, "ssh")
            src.credential = ("ssh_key", "", "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n", "")
            with self.assertRaisesRegex(GitSourceError, "known_hosts"):
                gitsource._env(tmp, src, "ssh")
            src.credential = ("ssh_key", "", "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n",
                              "github.com ssh-ed25519 AAAA")
            env = gitsource._env(tmp, src, "ssh")
            self.assertIn("StrictHostKeyChecking=yes", env["GIT_SSH_COMMAND"])
            self.assertIn("GlobalKnownHostsFile=/dev/null", env["GIT_SSH_COMMAND"])
            self.assertEqual(os.stat(Path(tmp, "id_key")).st_mode & 0o777, 0o600)


class GitStackApiTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo, self.head = _make_repo(Path(self.tmp.name))
        self.host = Host.objects.create(hostname="nas", agent_token="nastok", status=Host.Status.ONLINE,
                                        mode="managed", agent_features=["stack_source"])
        self.other = Host.objects.create(hostname="other", agent_token="othertok",
                                         status=Host.Status.ONLINE, mode="managed")
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.local = _local_git()
        self.local.__enter__()
        self.addCleanup(self.local.__exit__, None, None, None)

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self):
        return self.api.post("/api/v1/stacks/", {
            "host_id": str(self.host.id), "name": "web",
            "git": {"url": self.repo, "branch": "main", "path": "deploy/compose.yaml"},
            "env_text": "TOKEN=abc\n"}, format="json")

    def test_create_pull_and_no_edits_in_vigil(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["git"]["commit"], self.head)
        self.assertEqual(body["compose_yaml"], COMPOSE)
        sid = body["id"]
        edit = self.api.put(f"/api/v1/stacks/{sid}/", {"compose_yaml": COMPOSE}, format="json")
        self.assertEqual(edit.status_code, 400)
        self.assertIn("comes from Git", edit.json()["detail"])
        same = self.api.post(f"/api/v1/stacks/{sid}/pull/", format="json").json()
        self.assertFalse(same["changed"])
        Path(self.repo, "deploy", "compose.yaml").write_text(COMPOSE + "  # v2\n")
        _run(self.repo, "commit", "-q", "-am", "second")
        moved = self.api.post(f"/api/v1/stacks/{sid}/pull/", format="json").json()
        self.assertTrue(moved["changed"])
        self.assertEqual(moved["stack"]["revision"], 2)
        self.assertEqual(moved["stack"]["git"]["commit"], _run(self.repo, "rev-parse", "HEAD"))

    def test_a_refused_compose_file_makes_no_stack(self):
        Path(self.repo, "deploy", "compose.yaml").write_text(
            "services:\n  a:\n    image: x\n    privileged: true\n")
        _run(self.repo, "commit", "-q", "-am", "bad")
        resp = self._create()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("privileged", resp.json()["detail"])
        from .models import ManagedStack
        self.assertFalse(ManagedStack.objects.exists())

    def test_deploy_ships_a_ticket_and_the_archive_hash(self):
        from apps.tasks.models import Task

        from .models import SourceSnapshot
        sid = self._create().json()["id"]
        with mock.patch(_TOTP, return_value=None):
            resp = self.api.post(f"/api/v1/stacks/{sid}/deploy/", {"totp": "1"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        params = Task.objects.get(pk=resp.json()["task"]).params["steps"][0]["params"]
        snap = SourceSnapshot.objects.get()
        self.assertEqual(params["source_sha256"], snap.sha256)
        self.assertEqual(params["source_commit"], self.head)
        self.assertNotIn("TOKEN=abc", str(params))

        ticket = params["source_ticket"]
        url = f"/api/v1/agent/stack-source/{ticket}/"
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION="Bearer othertok").status_code, 404)
        got = self.client.get(url, HTTP_AUTHORIZATION="Bearer nastok")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(hashlib.sha256(got.content).hexdigest(), snap.sha256)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION="Bearer nastok").status_code, 404,
                         "a ticket is good once")

    def test_an_agent_without_stack_source_is_refused(self):
        sid = self._create().json()["id"]
        self.host.agent_features = []
        self.host.save(update_fields=["agent_features"])
        with mock.patch(_TOTP, return_value=None):
            resp = self.api.post(f"/api/v1/stacks/{sid}/deploy/", {"totp": "1"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("too old", resp.json()["detail"])

    def test_credentials_are_write_only_and_need_totp(self):
        url = "/api/v1/stacks/git-credentials/"
        body = {"name": "gh", "kind": "token", "secret": "ghp_secret", "username": "bot"}
        self.assertEqual(self.api.post(url, body, format="json").status_code, 401)
        with mock.patch(_TOTP, return_value=None):
            self.assertEqual(self.api.post(url, body, format="json").status_code, 201)
            ssh = {"name": "deploy", "kind": "ssh_key",
                   "secret": "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----"}
            self.assertEqual(self.api.post(url, ssh, format="json").status_code, 400, "no host key pinned")
        listed = self.api.get(url).json()["results"]
        self.assertEqual([c["name"] for c in listed], ["gh"])
        self.assertNotIn("ghp_secret", str(listed))

    def test_viewers_cannot_touch_git_stacks(self):
        viewer = get_user_model().objects.create_user("v", password="x")
        api = APIClient()
        api.force_authenticate(viewer)
        self.assertEqual(api.get("/api/v1/stacks/git-credentials/").status_code, 403)
        sid = self._create().json()["id"]
        self.assertEqual(api.post(f"/api/v1/stacks/{sid}/pull/").status_code, 403)
