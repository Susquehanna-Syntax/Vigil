"""Linux software collection: per-source parsers, failure isolation, digest."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# The package import needs the path above the tests package on it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import software

DPKG_LIST = (
    "libc6:amd64\tlibc6\t2.43-2ubuntu2.4\tii \tUbuntu Developers "
    "<ubuntu-devel-discuss@lists.ubuntu.com>\n"
    "openssl\topenssl\t3.5.5-1ubuntu3.5\tii \tUbuntu Developers "
    "<ubuntu-devel-discuss@lists.ubuntu.com>\n"
    "bluez\tbluez\t5.85-4ubuntu0.1\thi \tUbuntu Developers "
    "<ubuntu-devel-discuss@lists.ubuntu.com>\n"
    "oldpkg\toldpkg\t1.0-1\trc \tSomeone <someone@example.com>\n"
)

APT_UPDATES = (
    "Listing...\n"
    "bluez-cups/resolute-updates 5.85-4ubuntu0.2 amd64 [upgradable from: "
    "5.85-4ubuntu0.1]\n"
    "bluez/resolute-updates 5.85-4ubuntu0.2 amd64 [upgradable from: "
    "5.85-4ubuntu0.1]\n"
)

SNAP_LIST = (
    "Name                       Version                         Rev    "
    "Tracking         Publisher            Notes\n"
    "bare                       1.0                             5      "
    "latest/stable    canonical**          base\n"
    "core24                     20240425                        739    "
    "latest/stable    canonical**          core\n"
    "firefox                    156.0-1                         8929   "
    "latest/stable/…  mozilla**            -\n"
)

SNAP_REFRESH = (
    "Name              Version                         Rev   Size    "
    "Publisher    Notes\n"
    "firefox           156.0.1-1                       8969  274MB   "
    "mozilla**    -\n"
)


class _Runner:
    """Answers _run by matching the argv prefix, in table order.

    Table order matters where one command is a prefix of another asked for
    later ("flatpak list" vs "flatpak remote-ls"), so a test lists the
    specific command first.
    """

    def __init__(self, table):
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        for match, result in self.table:
            if list(argv[:len(match)]) == list(match):
                if isinstance(result, BaseException):
                    raise result
                return result
        return 0, "", ""


def _collect(table, present):
    runner = _Runner(table)
    with patch.object(software, "_run", side_effect=runner), \
            patch.object(software.shutil, "which",
                         side_effect=lambda b: "/usr/bin/" + b if b in present else None):
        payload = software.collect_linux()
    return payload, runner


class DpkgTests(unittest.TestCase):
    def test_dpkg_list_and_apt_updates(self):
        payload, _ = _collect(
            [(["dpkg-query"], (0, DPKG_LIST, "")),
             (["apt", "list"], (0, APT_UPDATES,
                                        "WARNING: apt does not have a stable CLI interface.\n"))],
            ["dpkg-query"])
        items = {i["id"]: i for i in payload["items"]}
        self.assertEqual(sorted(items), ["bluez", "libc6:amd64", "openssl"])
        self.assertEqual(payload["errors"], {})
        self.assertEqual(items["libc6:amd64"]["name"], "libc6")
        self.assertEqual(items["libc6:amd64"]["version"], "2.43-2ubuntu2.4")
        self.assertEqual(items["libc6:amd64"]["publisher"], "Ubuntu Developers")
        self.assertEqual(items["libc6:amd64"]["latest"], "")
        self.assertEqual(items["openssl"]["publisher"], "Ubuntu Developers")
        # The held ("hi") row is kept; the removed ("rc") row is dropped.
        self.assertEqual(items["bluez"]["version"], "5.85-4ubuntu0.1")
        self.assertEqual(items["bluez"]["latest"], "5.85-4ubuntu0.2")


class RpmTests(unittest.TestCase):
    def test_rpm_and_dnf_exit_100(self):
        listing = ("openssl\t1:3.2.2-9.fc41\tFedora Project\n"
                   "curl\t0:8.9.1-1.fc41\tFedora Project\n"
                   "zlib\t1.3.1-5.fc41\t(none)\n")
        updates = ("openssl.x86_64    1:3.2.2-10.fc41    updates\n"
                   "curl.x86_64    8.9.1-2.fc41    updates\n"
                   "\n"
                   "Obsoleting Packages\n"
                   "fake.x86_64    9.9-1.fc41    updates\n")
        payload, _ = _collect(
            [(["rpm"], (0, listing, "")), (["dnf"], (100, updates, ""))],
            ["rpm", "dnf"])
        items = {i["id"]: i for i in payload["items"]}
        self.assertEqual(payload["errors"], {})
        self.assertEqual(items["openssl"]["version"], "1:3.2.2-9.fc41")
        self.assertEqual(items["openssl"]["latest"], "1:3.2.2-10.fc41")
        self.assertEqual(items["openssl"]["publisher"], "Fedora Project")
        self.assertEqual(items["curl"]["version"], "8.9.1-1.fc41")
        self.assertEqual(items["curl"]["latest"], "8.9.1-2.fc41")
        self.assertEqual(items["zlib"]["publisher"], "")
        self.assertEqual(items["zlib"]["latest"], "")

    def test_rpm_updates_exit_1_is_a_failure(self):
        payload, _ = _collect(
            [(["rpm"], (0, "openssl\t1:3.2.2-9.fc41\tFedora Project\n", "")),
             (["dnf"], (1, "", "Traceback: boom\n"))],
            ["rpm", "dnf"])
        self.assertIn("rpm-updates", payload["errors"])
        self.assertEqual(payload["items"][0]["latest"], "")


class ApkTests(unittest.TestCase):
    def test_apk_name_version_split(self):
        listing = "curl-8.9.1-r1\npy3-foo-bar-1.2-r0\nnot-a-package\n\n"
        update_line = ("curl-8.9.1-r2 x86_64 {curl} (curl) "
                       "[upgradable from: curl-8.9.1-r1]\n")
        payload, _ = _collect(
            [(["apk", "info"], (0, listing, "")),
             (["apk", "list"], (0, update_line, ""))],
            ["apk"])
        items = {i["id"]: i for i in payload["items"]}
        self.assertEqual(sorted(items), ["curl", "py3-foo-bar"])
        self.assertEqual(items["curl"]["version"], "8.9.1-r1")
        self.assertEqual(items["curl"]["latest"], "8.9.1-r2")
        self.assertEqual(items["py3-foo-bar"]["version"], "1.2-r0")
        self.assertEqual(items["py3-foo-bar"]["latest"], "")


class PacmanTests(unittest.TestCase):
    def test_pacman_no_updates_exit_1(self):
        payload, _ = _collect(
            [(["pacman", "-Q"], (0, "openssl 3.3.2-1\nvim 9.1.000-1\n", "")),
             (["pacman", "-Qu"], (1, "", ""))],
            ["pacman"])
        self.assertEqual(payload["errors"], {})
        self.assertEqual([i["latest"] for i in payload["items"]], ["", ""])

    def test_pacman_updates_arrow(self):
        payload, _ = _collect(
            [(["pacman", "-Q"], (0, "openssl 3.3.2-1\n", "")),
             (["pacman", "-Qu"], (0, "openssl 3.3.2-1 -> 3.3.3-1\n", ""))],
            ["pacman"])
        self.assertEqual(payload["items"][0]["latest"], "3.3.3-1")


class SnapTests(unittest.TestCase):
    def test_snap_skips_bases_and_reads_refresh_list(self):
        payload, _ = _collect(
            [(["snap", "list"], (0, SNAP_LIST, "")),
             (["snap", "refresh"], (0, SNAP_REFRESH, ""))],
            ["snap"])
        items = {i["id"]: i for i in payload["items"]}
        self.assertEqual(list(items), ["firefox"])
        self.assertNotIn("bare", items)
        self.assertNotIn("core24", items)
        self.assertEqual(items["firefox"]["version"], "156.0-1")
        self.assertEqual(items["firefox"]["publisher"], "mozilla")
        self.assertEqual(items["firefox"]["latest"], "156.0.1-1")
        self.assertEqual(payload["errors"], {})

    def test_snap_all_up_to_date_is_success(self):
        payload, _ = _collect(
            [(["snap", "list"], (0, SNAP_LIST, "")),
             (["snap", "refresh"], (0, "", "All snaps up to date.\n"))],
            ["snap"])
        self.assertEqual(payload["errors"], {})
        self.assertEqual(payload["items"][0]["latest"], "")


class FlatpakTests(unittest.TestCase):
    def test_flatpak_tabs_and_space_fallback(self):
        payload, _ = _collect(
            [(["flatpak", "list"],
              (0, "org.mozilla.firefox\tFirefox\t131.0\tsystem\n", "")),
             (["flatpak", "remote-ls"],
              (0, "org.mozilla.firefox\t131.0.2\n", ""))],
            ["flatpak"])
        self.assertEqual(payload["items"], [{
            "source": "flatpak", "id": "org.mozilla.firefox", "name": "Firefox",
            "version": "131.0", "latest": "131.0.2", "scope": "machine",
            "user": "", "publisher": "", "managed": True}])
        spaced, _ = _collect(
            [(["flatpak", "list"], (0, "org.gimp.GIMP   GIMP   2.10.38   user\n", "")),
             (["flatpak", "remote-ls"], (0, "org.gimp.GIMP   2.10.40\n", ""))],
            ["flatpak"])
        self.assertEqual(spaced["items"][0]["name"], "GIMP")
        self.assertEqual(spaced["items"][0]["latest"], "2.10.40")


class FailureIsolationTests(unittest.TestCase):
    def test_failed_source_is_an_error_not_a_crash(self):
        timeout = subprocess.TimeoutExpired(cmd=["snap", "list"], timeout=60)
        payload, _ = _collect(
            [(["dpkg-query"], (0, DPKG_LIST, "")),
             (["apt", "list"], (0, APT_UPDATES, "")),
             (["snap", "list"], timeout)],
            ["dpkg-query", "snap"])
        self.assertEqual({i["source"] for i in payload["items"]}, {"dpkg"})
        self.assertIn("snap", payload["errors"])
        self.assertTrue(payload["errors"]["snap"].startswith("snap failed:"))


class PayloadTests(unittest.TestCase):
    def _items(self):
        return [software._item("dpkg", "openssl", "openssl", "3.5.5-1ubuntu3.5"),
                software._item("snap", "firefox", "firefox", "156.0-1", "mozilla")]

    def test_only_the_first_primary_database_runs(self):
        payload, runner = _collect(
            [(["dpkg-query"], (0, DPKG_LIST, "")),
             (["apt", "list"], (0, APT_UPDATES, "")),
             (["rpm"], (0, "openssl\t1:3.2.2-9.fc41\tFedora Project\n", ""))],
            ["dpkg-query", "rpm"])
        self.assertEqual({i["source"] for i in payload["items"]}, {"dpkg"})
        self.assertNotIn(["rpm"], runner.calls)
        self.assertIn("dpkg-query", [c[0] for c in runner.calls])

    def test_overlays_run_alongside_the_primary_database(self):
        payload, runner = _collect(
            [(["dpkg-query"], (0, DPKG_LIST, "")),
             (["apt", "list"], (0, APT_UPDATES, "")),
             (["snap", "list"], (0, SNAP_LIST, "")),
             (["snap", "refresh"], (0, SNAP_REFRESH, ""))],
            ["dpkg-query", "snap"])
        self.assertEqual({i["source"] for i in payload["items"]}, {"dpkg", "snap"})
        self.assertIn("snap", [c[0] for c in runner.calls])

    def test_digest_is_order_and_time_independent(self):
        items = self._items()
        self.assertEqual(software.digest(items), software.digest(list(reversed(items))))
        forward, _ = _collect([(["dpkg-query"], (0, DPKG_LIST, ""))], ["dpkg-query"])
        with patch.object(software, "_collected_at",
                          return_value="2020-01-01T00:00:00Z"):
            back, _ = _collect([(["dpkg-query"], (0, DPKG_LIST, ""))], ["dpkg-query"])
        self.assertEqual(forward["digest"], back["digest"])
        changed = [dict(items[0], version="9.9"), items[1]]
        self.assertNotEqual(software.digest(items), software.digest(changed))

    def test_payload_matches_phase01_shape(self):
        payload, _ = _collect(
            [(["dpkg-query"], (0, DPKG_LIST, "")),
             (["apt", "list"], (0, APT_UPDATES, ""))],
            ["dpkg-query"])
        self.assertEqual(sorted(payload),
                         ["collected_at", "digest", "errors", "items"])
        self.assertTrue(payload["items"])
        nine = sorted(software.ITEM_FIELDS)
        for item in payload["items"]:
            self.assertEqual(sorted(item), nine)
            self.assertEqual(item["scope"], "machine")
            self.assertEqual(item["user"], "")
            self.assertIs(item["managed"], True)
        self.assertEqual(len(payload["digest"]), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in payload["digest"]))


if __name__ == "__main__":
    unittest.main()
