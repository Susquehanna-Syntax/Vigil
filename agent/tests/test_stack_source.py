"""A Git stack's source folder on the host (2026.14.1): the archive must be
the one the signed task names, may hold regular files only, and is written as
root without following any link a container or user planted in the folder."""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import dataclasses
import gzip
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import client, executor  # noqa: F401 — executor first, it imports stacks
from vigil_agent.actions import stacks
from vigil_agent.config import AgentConfig

TICKET = "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
COMPOSE = "services:\n  app:\n    build: ./app\n"


def _archive(entries) -> bytes:
    """entries: (name, bytes|None for a dir, mode) or ("link", name, target)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for entry in entries:
            if entry[0] == "link":
                info = tarfile.TarInfo(entry[1])
                info.type, info.linkname = tarfile.SYMTYPE, entry[2]
                tar.addfile(info)
                continue
            name, data, mode = entry
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


GOOD = _archive([("compose.yaml", COMPOSE.encode(), 0o644), ("app", None, 0o755),
                 ("app/Dockerfile", b"FROM nginx\n", 0o644), ("run.sh", b"#!/bin/sh\n", 0o755)])


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class SourceFilesTests(unittest.TestCase):
    def test_the_archive_must_be_the_signed_one(self):
        files = stacks._source_files(GOOD, _sha(GOOD))
        self.assertEqual(sorted(files), ["app/Dockerfile", "compose.yaml", "run.sh"])
        self.assertEqual(files["run.sh"][1], 0o755)
        with self.assertRaisesRegex(ValueError, "does not match"):
            stacks._source_files(GOOD, "0" * 64)

    def test_only_regular_files_at_plain_paths(self):
        for bad in (_archive([("link", "etc", "/etc")]), _archive([("../x", b"x", 0o644)]),
                    _archive([("/abs", b"x", 0o644)]), _archive([("a//b", b"x", 0o644)])):
            with self.subTest(), self.assertRaises(ValueError):
                stacks._source_files(bad, _sha(bad))


class PlaceSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "web"
        self.work.mkdir()
        self.manifest = self.root / "data" / "web.source.json"
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _place(self, archive=GOOD):
        stacks._place_source(self.work, stacks._source_files(archive, _sha(archive)), self.manifest)

    def test_files_land_with_their_modes_and_a_private_manifest(self):
        (self.work / "data").mkdir()
        (self.work / "data" / "db.sqlite").write_text("keep me")
        (self.work / ".env").write_text("A=1\n")
        self._place()
        self.assertEqual((self.work / "app" / "Dockerfile").read_text(), "FROM nginx\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.work / "run.sh").st_mode), 0o755)
        self.assertEqual((self.work / "data" / "db.sqlite").read_text(), "keep me")
        self.assertEqual((self.work / ".env").read_text(), "A=1\n")
        self.assertEqual(json.loads(self.manifest.read_text()), ["app/Dockerfile", "compose.yaml", "run.sh"])
        self.assertEqual(stat.S_IMODE(os.stat(self.manifest).st_mode), 0o600)

    def test_a_planted_folder_link_is_refused_and_nothing_escapes(self):
        os.symlink(self.outside, self.work / "app")
        with self.assertRaisesRegex(ValueError, "not a plain folder"):
            self._place()
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_planted_file_link_is_replaced_not_written_through(self):
        target = self.outside / "cron"
        target.write_text("untouched")
        os.symlink(target, self.work / "run.sh")
        self._place()
        self.assertFalse((self.work / "run.sh").is_symlink())
        self.assertEqual(target.read_text(), "untouched")

    def test_files_a_newer_commit_dropped_are_removed_and_only_those(self):
        self._place()
        (self.work / "mine.txt").write_text("not from the repo")
        smaller = _archive([("compose.yaml", COMPOSE.encode(), 0o644)])
        self._place(smaller)
        self.assertFalse((self.work / "run.sh").exists())
        self.assertFalse((self.work / "app" / "Dockerfile").exists())
        self.assertTrue((self.work / "mine.txt").exists())

    def test_a_forged_manifest_entry_cannot_reach_outside(self):
        victim = self.outside / "victim"
        victim.write_text("x")
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps(["../outside/victim", "/etc/passwd"]))
        os.symlink(self.outside, self.work / "lnk")
        self.manifest.write_text(json.dumps(["../outside/victim", "lnk/victim"]))
        self._place()
        self.assertTrue(victim.exists())


class DeployWithSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = dataclasses.replace(AgentConfig(server_url="https://v", agent_token="t", mode="full_control"),
                                       data_dir=self.root / "agent-data")

    def tearDown(self):
        self.tmp.cleanup()

    def _deploy(self, archive, sha):
        params = {"project": "web", "compose": COMPOSE, "working_dir": str(self.root / "web"),
                  "source_ticket": TICKET, "source_sha256": sha, "source_commit": "a" * 40}
        with patch.object(client, "fetch_stack_source", return_value=archive) as fetch, \
                patch.object(stacks, "_safe_workdir"), \
                patch.object(stacks, "_check_resolved", return_value={"services": {}}), \
                patch.object(stacks, "_up", return_value="up"), \
                patch.object(executor.collector, "request_docker_recheck"):
            stacks._stack_deploy(params, self.cfg)
        fetch.assert_called_once_with(self.cfg, TICKET)

    def test_a_git_stack_gets_its_source_then_the_checked_compose(self):
        self._deploy(GOOD, _sha(GOOD))
        self.assertEqual((self.root / "web" / "app" / "Dockerfile").read_text(), "FROM nginx\n")
        self.assertEqual((self.root / "web" / "compose.yaml").read_text(), COMPOSE)
        self.assertTrue((self.root / "agent-data" / "stacks" / "web.source.json").exists())

    def test_a_tampered_archive_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            self._deploy(GOOD, "0" * 64)
        self.assertFalse((self.root / "web").exists())

    def test_half_a_source_param_is_refused(self):
        with self.assertRaises(ValueError):
            stacks._source_params({"source_ticket": TICKET})
        self.assertIsNone(stacks._source_params({}))


if __name__ == "__main__":
    unittest.main()
