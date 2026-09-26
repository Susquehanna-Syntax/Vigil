"""hunt_file: find files by name/glob, sha256, size and age (M5 phase 02).

The probe walks a targeted scope in a temp tree (paths=<tmp>) so the suite
stays hermetic; the full_disk root is exercised through the same code path.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, hunt
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control", "data_dir": Path("/nonexistent-vigil-data")}
    base.update(kw)
    return AgentConfig(**base)


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class HuntFileProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.params = {"paths": str(self.root)}

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **params):
        merged = dict(self.params)
        merged.update(params)
        out = hunt.run_hunt(hunt.hunt_file, merged)
        return json.loads(out), out

    def test_glob_matches_by_name(self):
        _write(self.root, "lib/log4j-core-2.14.1.jar", "a")
        _write(self.root, "lib/log4j-core-2.15.0.jar", "b")
        _write(self.root, "lib/log4j-api-2.14.1.jar", "c")

        body, _ = self._run(name="log4j-core-2.1*.jar")
        names = sorted(m["path"] for m in body["matches"])
        self.assertEqual(len(names), 2)
        self.assertTrue(all("log4j-core" in n for n in names))

    def test_sha256_filter(self):
        _write(self.root, "a.bin", "alpha")
        p2 = _write(self.root, "b.bin", "beta")
        digest = hashlib.sha256(b"beta").hexdigest()

        body, _ = self._run(sha256=digest)
        paths = [m["path"] for m in body["matches"]]
        self.assertEqual(paths, [str(p2)])

    def test_size_and_age_filters(self):
        small = _write(self.root, "small.txt", "x")
        big = _write(self.root, "big.txt", "y" * 2048)
        old = _write(self.root, "old.txt", "z" * 4096)
        stale = time.time() - 10 * 86400
        os.utime(old, (stale, stale))

        body, _ = self._run(name="*", min_size=1024)
        self.assertEqual(sorted(m["path"] for m in body["matches"]),
                         sorted([str(big), str(old)]))

        body, _ = self._run(name="*.txt", older_than_days=5)
        self.assertEqual([m["path"] for m in body["matches"]], [str(old)])

        body, _ = self._run(name="*.txt", modified_within_days=1)
        self.assertEqual(sorted(m["path"] for m in body["matches"]),
                         sorted([str(small), str(big)]))

    def test_symlinks_and_unreadable_are_skipped(self):
        target = _write(self.root, "real.log", "data")
        os.symlink(target, self.root / "link.log")
        # The symlink itself must never be reported as a match.
        body, _ = self._run(name="link.log")
        self.assertEqual(body["matches"], [])
        body, _ = self._run(name="*.log")
        self.assertEqual([m["path"] for m in body["matches"]], [str(target)])

    def test_max_results_truncates_walk(self):
        for i in range(5):
            _write(self.root, f"sub{i}/f{i}.jar", f"jar{i}")

        body, out = self._run(name="*.jar", max_results=2)
        self.assertEqual(len(body["matches"]), 2)
        self.assertEqual(body["truncated"], True)
        self.assertEqual(out.data["truncated"], True)
        self.assertEqual(out.data["count"], 2)
        self.assertEqual(out.data["matched"], True)

    def test_needs_name_or_hash(self):
        _write(self.root, "a.txt", "a")
        with self.assertRaises(RuntimeError) as ctx:
            self._run()
        self.assertIn("name", str(ctx.exception))
        self.assertIn("sha256", str(ctx.exception))

    def test_hash_only_when_asked(self):
        _write(self.root, "a.bin", "hello")

        body, _ = self._run(name="a.bin")
        self.assertEqual(body["matches"][0]["sha256"], None)

        body, _ = self._run(name="a.bin", hash=True)
        self.assertEqual(body["matches"][0]["sha256"],
                         hashlib.sha256(b"hello").hexdigest())

    def test_match_shape(self):
        p = _write(self.root, "shape.txt", "abc")
        body, _ = self._run(name="shape.txt", hash=True)
        match = body["matches"][0]
        self.assertEqual(match["evidence_type"], "file")
        self.assertEqual(match["path"], str(p))
        self.assertEqual(match["size"], 3)
        self.assertTrue(match["modified"].endswith("Z"))
        self.assertEqual(match["sha256"], hashlib.sha256(b"abc").hexdigest())

    def test_no_matches(self):
        _write(self.root, "a.txt", "a")
        body, out = self._run(name="missing-*.jar")
        self.assertEqual(body["matches"], [])
        self.assertEqual(body["truncated"], False)
        self.assertEqual(out.data, {"matched": False, "count": 0,
                                    "truncated": False})

    def test_bad_sha256_refused(self):
        with self.assertRaises(RuntimeError):
            self._run(sha256="nothex")

    def test_paths_comma_separated(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        first = self.root
        second = Path(other.name)
        _write(first, "one.jar", "1")
        _write(second, "sub/two.jar", "2")
        _write(second, "outside.jar", "x")

        body, _ = self._run(name="*.jar",
                            paths=f"{first},{second}")
        # one.jar from the first root, two.jar + outside.jar from the second.
        self.assertEqual(sorted(m["path"] for m in body["matches"]),
                         sorted([str(first / "one.jar"),
                                 str(second / "sub" / "two.jar"),
                                 str(second / "outside.jar")]))


class HuntFileActionTests(unittest.TestCase):
    def test_through_execute_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "log4j-core-2.14.1.jar"
            target.write_bytes(b"jar")
            out = executor.execute_action(
                "hunt_file",
                {"name": "log4j-core-2.1*.jar", "paths": tmp},
                _config())
            body = json.loads(out)
            self.assertIn("matches", body)
            self.assertIn("duration", body)
            self.assertTrue(out.data["matched"])
            self.assertEqual(out.data["count"], 1)
            self.assertEqual(out.data["truncated"], False)


if __name__ == "__main__":
    unittest.main()
