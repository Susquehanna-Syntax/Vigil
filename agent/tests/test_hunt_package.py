"""hunt_package: installed packages by name and version range, per-system version rules."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, hunt
from vigil_agent.config import AgentConfig

DPKG = [("openssl", "3.0.2-0ubuntu1.15"), ("libssl3", "3.0.2-0ubuntu1.15"),
        ("curl", "7.81.0-1ubuntu1.18"), ("openssl-dev", "3.0.13-1")]
RPM = [("openssl", "1:3.0.7-27.el9"), ("curl", "0:7.76.1-29.el9")]


def _run(params, manager="dpkg", listing=DPKG):
    with patch.object(hunt, "_list_packages", return_value=listing), \
            patch.object(hunt.shutil, "which", side_effect=lambda b: "/usr/bin/x" if b == {
                "dpkg": "dpkg-query"}.get(manager, manager) else None):
        out = hunt.run_hunt(hunt.hunt_package, params)
    return json.loads(out), out


class HuntPackageTests(unittest.TestCase):
    def test_dpkg_listing_and_bounds(self):
        body, out = _run({"name": "openssl", "version_lt": "3.0.13"})
        self.assertEqual([(m["name"], m["version"]) for m in body["matches"]],
                         [("openssl", "3.0.2-0ubuntu1.15")])
        self.assertEqual(body["matches"][0]["manager"], "dpkg")
        self.assertEqual(out.data, {"matched": True, "count": 1, "truncated": False})

    def test_rpm_listing_uses_epoch(self):
        # 1:3.0.7 beats a bound with no epoch, however high its version.
        body, _ = _run({"name": "openssl", "version_gt": "9.9"}, manager="rpm", listing=RPM)
        self.assertEqual(len(body["matches"]), 1)

    def test_glob_names(self):
        body, _ = _run({"name": "openssl*"})
        self.assertEqual(sorted(m["name"] for m in body["matches"]), ["openssl", "openssl-dev"])

    def test_all_bounds_must_hold(self):
        body, _ = _run({"name": "openssl*", "version_gte": "3.0.2", "version_lt": "3.0.13"})
        self.assertEqual([m["name"] for m in body["matches"]], ["openssl"])

    def test_no_manager_is_an_error(self):
        with patch.object(hunt.shutil, "which", return_value=None), \
                self.assertRaises(RuntimeError) as ctx:
            hunt.run_hunt(hunt.hunt_package, {"name": "openssl"})
        self.assertTrue("no supported package manager" in str(ctx.exception))

    def test_name_is_required(self):
        with self.assertRaises(RuntimeError):
            _run({})

    def test_snap_listing_parses_padded_columns(self):
        rows = hunt._parse_listing("snap", ["Name    Version   Rev", "core22  20240111  1122",
                                            "lxd     5.21.1    28460"])
        self.assertEqual(rows, [("core22", "20240111"), ("lxd", "5.21.1")])

    def test_dpkg_listing_command(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="openssl\t3.0.2-0ubuntu1.15\n", stderr="")
        with patch.object(hunt.subprocess, "run", side_effect=fake):
            self.assertEqual(hunt._list_packages("dpkg"), [("openssl", "3.0.2-0ubuntu1.15")])
        self.assertEqual(seen["argv"][0], "dpkg-query")

    def test_through_execute_action(self):
        cfg = AgentConfig(server_url="https://vigil.example.com", agent_token="t", mode="full_control")
        with patch.object(hunt, "_list_packages", return_value=DPKG), \
                patch.object(hunt.shutil, "which", side_effect=lambda b: "/x" if b == "dpkg-query" else None):
            out = executor.execute_action("hunt_package", {"name": "curl", "version_lt": "8.0"}, cfg)
        self.assertEqual(out.data["count"], 1)


if __name__ == "__main__":
    unittest.main()
