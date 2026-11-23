"""A self-update must not cost the host its agent (sprint M8): the new build
proves itself before the swap, the old one is kept until a check-in confirms
the new one, and check-ins that keep failing put the old one back."""

import hashlib
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import __version__, executor, update_guard
from vigil_agent.config import AgentConfig


def _config(data_dir, **kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t" * 32,
            "mode": "full_control", "data_dir": Path(data_dir)}
    base.update(kw)
    return AgentConfig(**base)


def _script(path: Path, body: str) -> Path:
    """An executable stand-in for a downloaded agent build."""
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


@unittest.skipIf(os.name == "nt", "the stand-in builds are shell scripts")
class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = _config(self.dir, config_path=self.dir / "agent.yml")

    def test_a_build_that_proves_itself_passes_and_names_its_version(self):
        build = _script(self.dir / "new", 'echo "starting"; '
                        'echo \'{"ok": true, "version": "2026.17.0"}\'')
        self.assertEqual(update_guard.run_preflight(build, self.config), "2026.17.0")

    def test_it_is_asked_with_the_running_agents_config(self):
        build = _script(self.dir / "new", f'echo "$@" > {self.dir}/argv; echo \'{{"ok": true}}\'')
        update_guard.run_preflight(build, self.config)
        self.assertEqual((self.dir / "argv").read_text().split(),
                         ["preflight", "-c", str(self.dir / "agent.yml")])

    def test_a_build_that_fails_its_check_is_refused_with_its_reason(self):
        build = _script(self.dir / "new", 'echo \'{"ok": false, "error": "ImportError: '
                        'libssl.so.3: cannot open shared object file"}\'; exit 1')
        with self.assertRaises(ValueError) as caught:
            update_guard.run_preflight(build, self.config)
        self.assertIn("libssl.so.3", str(caught.exception))
        self.assertIn("not installed", str(caught.exception))

    def test_a_build_that_crashes_without_a_verdict_is_refused(self):
        build = _script(self.dir / "new", 'echo "Segmentation fault" >&2; exit 139')
        with self.assertRaises(ValueError) as caught:
            update_guard.run_preflight(build, self.config)
        self.assertIn("Segmentation fault", str(caught.exception))

    def test_exit_zero_without_a_verdict_is_not_a_pass(self):
        build = _script(self.dir / "new", "exit 0")
        with self.assertRaises(ValueError):
            update_guard.run_preflight(build, self.config)

    def test_a_file_that_cannot_run_here_is_refused(self):
        build = self.dir / "new"
        build.write_bytes(b"\x7fELF" + b"\x00" * 64)  # the wrong architecture looks like this
        build.chmod(0o755)
        with self.assertRaises(ValueError) as caught:
            update_guard.run_preflight(build, self.config)
        self.assertIn("cannot run on this host", str(caught.exception))

    def test_a_build_that_hangs_is_refused(self):
        build = _script(self.dir / "new", "sleep 30")
        with mock.patch.object(update_guard, "PREFLIGHT_SECONDS", 0.3):
            with self.assertRaises(ValueError) as caught:
                update_guard.run_preflight(build, self.config)
        self.assertIn("did not finish", str(caught.exception))

    def test_a_build_older_than_the_self_check_is_let_through(self):
        build = _script(self.dir / "old", 'echo "usage: vigil-agent [-h]" >&2; '
                        'echo "vigil-agent: error: unrecognized arguments: preflight" >&2; exit 2')
        self.assertEqual(update_guard.run_preflight(build, self.config), "")

    def test_the_real_command_fails_cleanly_without_a_config(self):
        """`preflight` itself, from source: no config means a verdict, not a
        traceback, and a non-zero exit."""
        import subprocess
        done = subprocess.run(
            [sys.executable, "vigil_agent_main.py", "preflight", "-c", str(self.dir / "none.yml")],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        verdict = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["version"], __version__)
        self.assertIn("FileNotFoundError", verdict["error"])


class ProbationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.exe = self.dir / "vigil-agent"
        self.exe.write_bytes(b"old build")
        self.restarts = []
        self.now = 0.0

    NEW = "2026.99.0"
    swapped = False

    def probation(self, running=None):
        """The agent that starts next: the new version after a swap, unless a
        test says the old one came back."""
        running = running or (self.NEW if self.swapped else __version__)
        return update_guard.Probation(self.dir, restart=lambda: self.restarts.append(1),
                                      clock=lambda: self.now, version=running)

    def swap(self, to_version=NEW):
        previous = update_guard.keep_previous(self.exe)
        update_guard.begin(self.dir, to_version=to_version, exe=self.exe, previous=previous)
        fresh = self.dir / "download"
        fresh.write_bytes(b"new build")
        os.replace(fresh, self.exe)  # as the update does: a new file, not a rewrite
        self.swapped = True
        return previous

    def test_with_no_update_in_flight_nothing_happens(self):
        p = self.probation()
        self.assertIsNone(p.report())
        p.checkin_succeeded(reported=False)
        for _ in range(10):
            self.now += 600
            self.assertFalse(p.checkin_failed("boom"))
        self.assertEqual(self.restarts, [])

    def test_the_old_build_is_kept_whole_until_confirmed(self):
        previous = self.swap()
        self.assertEqual(previous.read_bytes(), b"old build")
        self.assertEqual(self.exe.read_bytes(), b"new build")
        self.assertEqual(update_guard.load(self.dir)["state"], "pending")

    def test_the_first_good_checkin_confirms_and_the_next_one_reports_it(self):
        previous = self.swap()
        p = self.probation()
        self.assertIsNone(p.report(), "nothing to say until the outcome is known")
        p.checkin_succeeded(reported=False)
        self.assertFalse(previous.exists(), "the old build is deleted once the new one works")
        report = p.report()
        self.assertEqual((report["state"], report["from_version"], report["to_version"],
                          report["running"]),
                         ("confirmed", __version__, self.NEW, self.NEW))
        p.checkin_succeeded(reported=True)
        self.assertIsNone(update_guard.load(self.dir), "told once, then forgotten")
        self.assertIsNone(p.report())

    def test_a_report_that_did_not_reach_the_server_is_kept(self):
        self.swap()
        p = self.probation()
        p.checkin_succeeded(reported=False)
        p.checkin_failed("network down")
        self.assertEqual(self.probation().report()["state"], "confirmed")

    def test_checkins_that_keep_failing_put_the_old_build_back(self):
        previous = self.swap()
        p = self.probation()
        self.now = 400
        self.assertFalse(p.checkin_failed("HTTP 500"))
        self.assertFalse(p.checkin_failed("HTTP 500"))
        self.assertEqual(self.restarts, [])
        self.assertTrue(p.checkin_failed("SSLError: bad handshake"))
        self.assertEqual(self.exe.read_bytes(), b"old build")
        self.assertFalse(previous.exists())
        self.assertEqual(self.restarts, [1])
        # The old agent, restarted, tells the server what happened and why.
        report = self.probation(running=__version__).report()
        self.assertEqual(report["running"], __version__)
        self.assertEqual(report["state"], "rolled_back")
        self.assertIn("SSLError", report["reason"])
        self.assertIn("3 check-ins failed", report["reason"])

    def test_failures_alone_are_not_enough_before_the_agent_has_been_up_a_while(self):
        self.swap()
        p = self.probation()
        self.now = 60  # a server restarting during the update
        for _ in range(8):
            self.assertFalse(p.checkin_failed("connection refused"))
        self.assertEqual(self.exe.read_bytes(), b"new build")

    def test_a_good_checkin_between_failures_starts_the_count_again(self):
        self.swap()
        p = self.probation()
        self.now = 400
        p.checkin_failed("x")
        p.checkin_failed("x")
        p.checkin_succeeded(reported=False)  # confirmed here
        for _ in range(5):
            self.assertFalse(p.checkin_failed("x"))
        self.assertEqual(self.exe.read_bytes(), b"new build")

    def test_after_a_rollback_the_old_agent_is_never_rolled_back_again(self):
        self.swap()
        p = self.probation()
        self.now = 400
        for _ in range(3):
            p.checkin_failed("x")
        old = self.probation(running=__version__)
        self.now = 4000
        for _ in range(10):
            self.assertFalse(old.checkin_failed("server still down"))
        self.assertEqual(self.restarts, [1])
        old.checkin_succeeded(reported=True)
        self.assertIsNone(update_guard.load(self.dir))

    def test_with_nothing_kept_it_keeps_trying_rather_than_guess(self):
        update_guard.begin(self.dir, to_version="2026.99.0", exe=self.exe, previous=None)
        p = self.probation()
        self.now = 4000
        for _ in range(10):
            self.assertFalse(p.checkin_failed("x"))
        self.assertEqual(self.restarts, [])

    def test_an_agent_that_came_back_as_its_old_self_says_the_update_did_not_apply(self):
        # The marker says this version was replaced by another, and here it is.
        update_guard.begin(self.dir, to_version="2026.99.0", exe=self.exe, previous=None)
        p = self.probation()
        p.checkin_succeeded(reported=False)
        report = p.report()
        self.assertEqual(report["state"], "not_applied")
        self.assertEqual(report["running"], __version__)

    def test_a_damaged_marker_is_no_marker(self):
        (self.dir / update_guard.MARKER_NAME).write_text("{not json")
        self.assertIsNone(self.probation().report())

    def test_long_text_from_a_failure_is_cut_to_one_readable_line(self):
        self.swap()
        p = self.probation()
        self.now = 400
        for _ in range(3):
            p.checkin_failed("Traceback\n" + "x" * 5000)
        reason = self.probation(running=__version__).report()["reason"]
        self.assertLessEqual(len(reason), update_guard.MAX_REASON)
        self.assertNotIn("\n", reason)


@unittest.skipIf(os.name == "nt", "the one-file swap is the Linux and macOS path")
class UpdateAgentTests(unittest.TestCase):
    """update_agent end to end, with the download faked and everything on
    disk real."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.exe = self.dir / "vigil-agent"
        self.exe.write_bytes(b"old build")
        self.config = _config(self.dir / "data")
        (self.dir / "data").mkdir()

    def update(self, payload: bytes, preflight):
        resp = mock.MagicMock()
        resp.headers = {"X-Vigil-Version": "2026.99.0"}
        resp.iter_content.return_value = [payload]
        sha = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(executor.sys, "platform", "linux"), \
                mock.patch.object(executor.sys, "argv", [str(self.exe)]), \
                mock.patch.object(executor.os, "uname", return_value=mock.Mock(machine="x86_64")), \
                mock.patch("requests.get", return_value=resp), \
                mock.patch.object(executor.shutil, "which", return_value=None), \
                mock.patch.object(update_guard, "run_preflight", preflight), \
                mock.patch.object(update_guard, "restart_service"):
            return executor._update_agent({"binary_sha256": {"linux-amd64": sha}}, self.config)

    def test_a_build_that_fails_its_check_leaves_everything_as_it_was(self):
        def refuse(candidate, config):
            raise ValueError("the new agent failed its self-check on this host: boom")

        with self.assertRaises(ValueError):
            self.update(b"new build", refuse)
        self.assertEqual(self.exe.read_bytes(), b"old build")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["data", "vigil-agent"],
                         "no download, no .previous, nothing left behind")
        self.assertIsNone(update_guard.load(self.config.data_dir))

    def test_the_check_runs_on_the_downloaded_file_before_anything_is_replaced(self):
        seen = {}

        def check(candidate, config):
            seen["bytes"] = Path(candidate).read_bytes()
            seen["mode"] = stat.S_IMODE(Path(candidate).stat().st_mode)
            seen["exe_then"] = self.exe.read_bytes()
            return "2026.99.1"

        out = self.update(b"new build", check)
        self.assertEqual(seen, {"bytes": b"new build", "mode": 0o755, "exe_then": b"old build"})
        self.assertEqual(self.exe.read_bytes(), b"new build")
        self.assertEqual(update_guard.previous_path(self.exe).read_bytes(), b"old build")
        marker = update_guard.load(self.config.data_dir)
        self.assertEqual((marker["state"], marker["from_version"], marker["to_version"]),
                         ("pending", __version__, "2026.99.1"))
        self.assertIn("2026.99.1", str(out), "the version the build said it is, not the header")


class DocsTests(unittest.TestCase):
    def test_the_wiki_states_the_rollback_rule_the_code_applies(self):
        wiki = (Path(__file__).resolve().parents[2] / "wiki" / "vigil-wiki.html").read_text()
        start = wiki.index('<h3 id="agent-updates">')
        section = wiki[start:wiki.index("</section>", start)]
        self.assertIn(f"After {update_guard.ROLLBACK_AFTER_FAILURES} failed check-ins", section)
        self.assertIn(f"at least {update_guard.ROLLBACK_AFTER_SECONDS // 60} minutes", section)
        for state in (update_guard.CONFIRMED, update_guard.ROLLED_BACK, update_guard.NOT_APPLIED):
            self.assertIn(f"<code>{state}</code>", section)


if __name__ == "__main__":
    unittest.main()
