"""The ssh check kind (S9, phase 08a): one read-only command on a device with a key.

No ssh is ever run here: shutil.which and subprocess.run are mocked in the
netcheck namespace, so a test box without an ssh client runs this file exactly
like one with it. The side effect of the fake run is where the interesting
assertions live — it sees the argv, and it can read the private key file and the
known_hosts file while they still exist, because the real check deletes the
temporary directory the moment the run returns.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.netcheck import DEFAULT_TIMEOUT, SSH_MAX_OUTPUT, SSH_TIMEOUT, run_check

PRIVATE_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACDJtGh7Y6h5wK4oQ2fXjZ0nDk1L3aqP4sM2rjZ0nDk1LwAAAEjQ6Mhu0OjI
bgAAAAtzc2gtZWQyNTUxOQAAACDJtGh7Y6h5wK4oQ2fXjZ0nDk1L3aqP4sM2rjZ0nDk1Lw
-----END OPENSSH PRIVATE KEY-----"""

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMm0aHtjqHnArihDZ9eNnScOTUvdrp"
LEARNED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyMaterialHere=="

BASE = {
    "kind": "ssh",
    "host": "10.0.0.7",
    "username": "vigil-backup",
    "command": "show running-config",
    "private_key": PRIVATE_KEY,
}


def _completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(args=["ssh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _options(argv: list[str]) -> dict[str, str]:
    """Every -o Option=value of the argv as a mapping."""
    return {name.split("=", 1)[0]: name.split("=", 1)[1]
            for flag, name in pairwise(argv) if flag == "-o"}


class _Runner:
    """A fake subprocess.run: records every call, inspects the temp files, answers.

    The inspection runs while the temporary directory still exists; the check
    deletes it in its finally, after this returns.
    """

    def __init__(self, result=None, error=None, learn=None):
        self.result = _completed() if result is None else result
        self.error = error
        self.learn = learn
        self.calls: list[tuple[list, dict]] = []
        self.key_contents: list[bytes | None] = []
        self.key_modes: list[int] = []
        self.known_hosts: list[str | None] = []
        self.dirs: list[str] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        key_path = argv[argv.index("-i") + 1]
        directory = os.path.dirname(key_path)
        self.dirs.append(directory)
        try:
            with open(key_path, "rb") as handle:
                self.key_contents.append(handle.read())
        except OSError:
            self.key_contents.append(None)
        try:
            self.key_modes.append(os.stat(key_path).st_mode & 0o777)
        except OSError:
            self.key_modes.append(-1)
        known_hosts = os.path.join(directory, "known_hosts")
        with open(known_hosts, "r", encoding="utf-8") as handle:
            contents = handle.read()
        if self.learn is not None:
            with open(known_hosts, "w", encoding="utf-8") as handle:
                handle.write(self.learn)
        self.known_hosts.append(contents)
        if self.error is not None:
            raise self.error
        return self.result


class _Fixture(unittest.TestCase):
    def run_ssh(self, changes=None, result=None, error=None, learn=None, which="/usr/bin/ssh"):
        """Run one ssh check with a mocked client. Returns (result, runner)."""
        runner = _Runner(result=result, error=error, learn=learn)
        params = dict(BASE, **(changes or {}))
        with mock.patch("vigil_agent.netcheck.shutil.which", return_value=which), \
                mock.patch("vigil_agent.netcheck.subprocess.run", side_effect=runner):
            out = run_check(params)
        self.assertEqual(len(runner.dirs), 1 if which is not None else 0)
        if which is not None:
            self.assertFalse(os.path.exists(runner.dirs[0]),
                             "the temporary directory must be gone afterwards")
        return out, runner

    def rejected(self, check):
        """run_check on a bad param: its detail is there and nothing was run."""
        with mock.patch("vigil_agent.netcheck.shutil.which") as which, \
                mock.patch("vigil_agent.netcheck.subprocess.run") as run:
            out = run_check(check)
        run.assert_not_called()
        which.assert_not_called()
        self.assertFalse(out["ok"])
        return out


class ArgvTests(_Fixture):
    def test_the_argv_is_exactly_the_documented_command(self):
        _out, runner = self.run_ssh()
        argv = runner.calls[0][0]
        key_path = argv[argv.index("-i") + 1]
        known_hosts = os.path.join(os.path.dirname(key_path), "known_hosts")
        self.assertEqual(argv, [
            "ssh",
            "-F", os.devnull,
            "-T",
            "-p", "22",
            "-i", key_path,
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", f"ConnectTimeout={int(DEFAULT_TIMEOUT)}",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", f"GlobalKnownHostsFile={os.devnull}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR",
            "--",
            "vigil-backup@10.0.0.7",
            "show running-config",
        ])

    def test_the_argv_carries_every_option_of_the_spec_and_no_others(self):
        _out, runner = self.run_ssh({"port": 2222})
        argv = runner.calls[0][0]
        known_hosts = argv[argv.index("-i") + 1].replace(f"{os.sep}key", f"{os.sep}known_hosts")
        self.assertEqual(_options(argv), {
            "BatchMode": "yes",
            "IdentitiesOnly": "yes",
            "IdentityAgent": "none",
            "PasswordAuthentication": "no",
            "KbdInteractiveAuthentication": "no",
            "ConnectTimeout": str(int(DEFAULT_TIMEOUT)),
            "UserKnownHostsFile": known_hosts,
            "GlobalKnownHostsFile": os.devnull,
            "StrictHostKeyChecking": "accept-new",
            "LogLevel": "ERROR",
        })
        self.assertEqual(argv[:6], ["ssh", "-F", os.devnull, "-T", "-p", "2222"])
        self.assertEqual(argv[-3:], ["--", "vigil-backup@10.0.0.7", "show running-config"])

    def test_a_key_check_is_given_a_list_and_never_a_shell(self):
        out, runner = self.run_ssh()
        argv, kwargs = runner.calls[0]
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["capture_output"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(out["ok"], out)

    def test_the_key_and_known_hosts_paths_are_the_two_files_in_one_directory(self):
        _out, runner = self.run_ssh()
        argv = runner.calls[0][0]
        key_path = argv[argv.index("-i") + 1]
        known_hosts = _options(argv)["UserKnownHostsFile"]
        self.assertTrue(key_path.endswith(f"{os.sep}key"))
        self.assertTrue(known_hosts.endswith(f"{os.sep}known_hosts"))
        self.assertEqual(os.path.dirname(key_path), os.path.dirname(known_hosts))

    def test_a_port_and_a_host_key_tighten_the_argv(self):
        _out, runner = self.run_ssh({"port": 2222, "host_key": HOST_KEY})
        argv = runner.calls[0][0]
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(_options(argv)["StrictHostKeyChecking"], "yes")

    def test_the_run_gets_the_ssh_timeout_and_a_clean_environment(self):
        out, runner = self.run_ssh()
        _argv, kwargs = runner.calls[0]
        self.assertEqual(kwargs["timeout"], SSH_TIMEOUT)
        env = kwargs["env"]
        self.assertIsInstance(env, dict)
        self.assertNotIn("VIGIL_AGENT_TOKEN", env)
        self.assertGreaterEqual(out["latency_ms"], 0)

    def test_no_private_key_text_reaches_the_argv(self):
        _out, runner = self.run_ssh()
        joined = " ".join(runner.calls[0][0])
        self.assertNotIn(PRIVATE_KEY, joined)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", joined)


class TempFileTests(_Fixture):
    def test_the_key_file_holds_the_key_and_is_owner_only(self):
        _out, runner = self.run_ssh()
        self.assertEqual(runner.key_contents[0], (PRIVATE_KEY + "\n").encode())
        if os.name != "nt":
            self.assertEqual(runner.key_modes[0], 0o600)

    def test_a_key_that_already_ends_with_a_newline_is_written_once(self):
        _out, runner = self.run_ssh({"private_key": PRIVATE_KEY + "\n"})
        self.assertEqual(runner.key_contents[0], (PRIVATE_KEY + "\n").encode())

    def test_known_hosts_names_the_host_plainly_on_port_22(self):
        _out, runner = self.run_ssh({"host_key": HOST_KEY})
        self.assertEqual(runner.known_hosts[0], f"10.0.0.7 {HOST_KEY}\n")

    def test_known_hosts_brackets_the_host_on_another_port(self):
        _out, runner = self.run_ssh({"port": 2222, "host_key": HOST_KEY})
        self.assertEqual(runner.known_hosts[0], f"[10.0.0.7]:2222 {HOST_KEY}\n")

    def test_known_hosts_is_empty_when_no_host_key_was_given(self):
        _out, runner = self.run_ssh()
        self.assertEqual(runner.known_hosts[0], "")

    def test_the_directory_is_gone_after_a_non_zero_exit(self):
        out, _runner = self.run_ssh(result=_completed(stderr=b"Permission denied\n",
                                                     returncode=255))
        self.assertFalse(out["ok"])

    def test_the_directory_is_gone_after_a_timeout(self):
        out, _runner = self.run_ssh(
            error=subprocess.TimeoutExpired(cmd=["ssh"], timeout=SSH_TIMEOUT))
        self.assertEqual(out["detail"], "ssh timed out")

    def test_the_directory_is_gone_when_the_run_raises_oserror(self):
        # run_check swallows it — the contract is that it never raises — but the
        # key file must still not survive.
        out, _runner = self.run_ssh(error=OSError("ssh: cannot fork"))
        self.assertFalse(out["ok"])
        self.assertTrue(out["detail"].startswith("could not run ssh"), out["detail"])


class OkResultTests(_Fixture):
    def test_output_sha256_size_and_line_count(self):
        text = "hostname switch-1\ninterface mgmt0\n"
        out, _runner = self.run_ssh(result=_completed(stdout=text.encode()))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["kind"], "ssh")
        self.assertEqual(out["target"], "10.0.0.7")
        self.assertEqual(out["detail"], "2 lines")
        self.assertEqual(out["output"], text)
        self.assertEqual(out["sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(out["size"], len(text.encode()))
        self.assertIsNone(out["host_key"])

    def test_crlf_is_normalised_before_the_digest(self):
        out, _runner = self.run_ssh(result=_completed(stdout=b"a\r\nb\r\n"))
        self.assertEqual(out["output"], "a\nb\n")
        self.assertEqual(out["size"], 4)
        self.assertEqual(out["sha256"], hashlib.sha256(b"a\nb\n").hexdigest())
        self.assertEqual(out["detail"], "2 lines")

    def test_undecodable_bytes_survive_as_replacement_characters(self):
        out, _runner = self.run_ssh(result=_completed(stdout=b"caf\xe9\n"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["output"], "caf\ufffd\n")
        self.assertEqual(out["size"], len("caf\ufffd\n".encode()))

    def test_an_empty_output_is_zero_lines_and_still_ok(self):
        out, _runner = self.run_ssh(result=_completed(stdout=b""))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["detail"], "0 lines")
        self.assertEqual(out["output"], "")
        self.assertEqual(out["size"], 0)

    def test_output_over_one_megabyte_is_a_failure(self):
        out, _runner = self.run_ssh(result=_completed(stdout=b"x" * (SSH_MAX_OUTPUT + 1)))
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "output over 1 MB")
        self.assertIsNone(out["output"])

    def test_exactly_one_megabyte_is_still_ok(self):
        out, _runner = self.run_ssh(result=_completed(stdout=b"x" * SSH_MAX_OUTPUT))
        self.assertTrue(out["ok"], out)


class HostKeyTests(_Fixture):
    def test_the_given_host_key_is_echoed_back(self):
        out, runner = self.run_ssh({"host_key": HOST_KEY})
        self.assertEqual(out["host_key"], HOST_KEY)
        self.assertIn(f"10.0.0.7 {HOST_KEY}", runner.known_hosts[0])

    def test_a_host_key_is_learned_from_known_hosts_on_success(self):
        out, _runner = self.run_ssh(learn=f"10.0.0.7 {LEARNED}\n")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["host_key"], LEARNED)

    def test_a_host_key_is_learned_even_when_the_login_is_refused(self):
        out, _runner = self.run_ssh(
            result=_completed(stderr=b"Permission denied (publickey).\n", returncode=255),
            learn=f"10.0.0.7 {LEARNED}\n")
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "Permission denied (publickey).")
        self.assertEqual(out["host_key"], LEARNED)

    def test_a_given_host_key_wins_over_what_ssh_wrote(self):
        out, _runner = self.run_ssh({"host_key": HOST_KEY}, learn=f"10.0.0.7 {LEARNED}\n")
        self.assertEqual(out["host_key"], HOST_KEY)

    def test_a_known_hosts_line_of_three_fields_is_read_as_type_and_base64(self):
        out, _runner = self.run_ssh(learn="junk ssh-ed25519 AAAA extra\n")
        self.assertEqual(out["host_key"], "ssh-ed25519 AAAA")

    def test_a_one_field_known_hosts_line_gives_no_host_key(self):
        out, _runner = self.run_ssh(learn="\n")
        self.assertIsNone(out["host_key"])


class FailureDetailTests(_Fixture):
    def test_a_changed_host_key_is_named_as_such(self):
        out, _runner = self.run_ssh(result=_completed(
            stderr=(b"@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
                    b"@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"),
            returncode=255))
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "host key changed")

    def test_host_key_verification_failed_is_also_a_changed_key(self):
        out, _runner = self.run_ssh(result=_completed(
            stderr=b"Host key verification failed.\n", returncode=255))
        self.assertEqual(out["detail"], "host key changed")

    def test_the_last_non_empty_stderr_line_is_the_detail(self):
        out, _runner = self.run_ssh(result=_completed(
            stderr=b"Connection refused\n\nssh: connect to device failed\n",
            returncode=255))
        self.assertEqual(out["detail"], "ssh: connect to device failed")

    def test_a_host_key_failure_in_lowercase_is_still_named_as_one(self):
        out, _runner = self.run_ssh(result=_completed(
            stderr=b"host key verification failed.\n", returncode=255))
        self.assertEqual(out["detail"], "host key changed")

    def test_a_long_stderr_line_is_cut_to_200_characters(self):
        out, _runner = self.run_ssh(result=_completed(stderr=b"E" * 400 + b"\n", returncode=1))
        self.assertEqual(out["detail"], "E" * 200)

    def test_empty_stderr_falls_back_to_the_exit_code(self):
        out, _runner = self.run_ssh(result=_completed(stderr=b"", returncode=255))
        self.assertEqual(out["detail"], "ssh exited 255")

    def test_a_timeout_is_reported_as_one(self):
        out, _runner = self.run_ssh(
            error=subprocess.TimeoutExpired(cmd=["ssh"], timeout=SSH_TIMEOUT))
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "ssh timed out")
        self.assertIsNone(out["output"])

    def test_no_ssh_client_on_this_poller(self):
        out, runner = self.run_ssh(which=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "no ssh client on this poller")
        self.assertEqual(runner.calls, [])


class ParamTests(_Fixture):
    def assert_rejected(self, detail, **changes):
        out = self.rejected(dict(BASE, **changes))
        self.assertEqual(out["detail"], detail)

    def test_an_invalid_host_is_rejected(self):
        for bad in ("10.0.0.7; rm -rf /", "swit ch", "10.0.0.7\nrm"):
            with self.subTest(host=str(bad)[:20]):
                self.assert_rejected("invalid host", host=bad)

    def test_a_missing_host_is_rejected(self):
        for bad in ("", 4, None):
            with self.subTest(host=str(bad)[:20]):
                self.assert_rejected("missing host", host=bad)

    def test_a_missing_username_is_rejected(self):
        check = {**BASE}
        del check["username"]
        self.assertEqual(self.rejected(check)["detail"], "invalid username")

    def test_an_invalid_username_is_rejected(self):
        for bad in ("root;id", "a" * 65, "", "some user", "ro\\ot"):
            with self.subTest(username=bad):
                self.assert_rejected("invalid username", username=bad)

    def test_a_missing_command_is_rejected(self):
        check = {**BASE}
        del check["command"]
        self.assertEqual(self.rejected(check)["detail"], "invalid command")

    def test_an_invalid_command_is_rejected(self):
        for bad in ("", "show running-config\nexit", "show running-config\rcat",
                    "show\x00running", "x" * 501, 7):
            with self.subTest(command=str(bad)[:24]):
                self.assert_rejected("invalid command", command=bad)

    def test_a_missing_key_is_rejected(self):
        check = {**BASE}
        del check["private_key"]
        self.assertEqual(self.rejected(check)["detail"], "invalid key")

    def test_an_invalid_key_is_rejected(self):
        for bad in ("ssh-rsa AAAAB3", "", "PEM KEY WITHOUT THE OPENSSH HEADER", 4, None):
            with self.subTest(key=str(bad)[:24]):
                self.assert_rejected("invalid key", private_key=bad)

    def test_a_key_over_sixteen_kilobytes_is_rejected(self):
        self.assert_rejected("invalid key", private_key=PRIVATE_KEY + "A" * 16_384)

    def test_a_key_of_exactly_sixteen_kilobytes_is_accepted(self):
        out, runner = self.run_ssh({"private_key": _padded_key()})
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(out["ok"], out)

    def test_an_invalid_port_is_rejected_without_running(self):
        self.assert_rejected("invalid port", port=70000)

    def test_a_given_host_key_of_the_wrong_shape_is_rejected(self):
        for bad in ("ssh-dss AAAAB3", "ssh-ed25519", "ssh-ed25519 AAA$%=", 5,
                    "ssh-ed25519 AAAA extra"):
            with self.subTest(host_key=str(bad)[:24]):
                self.assert_rejected("invalid host key", host_key=bad)

    def test_every_host_key_type_the_server_can_send_is_accepted(self):
        for key_type in ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                         "ecdsa-sha2-nistp521", "rsa-sha2-256", "rsa-sha2-512", "ssh-rsa"):
            with self.subTest(key_type=key_type):
                _out, runner = self.run_ssh({"host_key": f"{key_type} AAAAB3NzaC1j=="})
                self.assertEqual(len(runner.calls), 1)

    def test_no_host_key_at_all_is_valid(self):
        _out, runner = self.run_ssh()
        self.assertEqual(len(runner.calls), 1)

    def test_a_check_that_names_no_port_is_still_run_on_22(self):
        self.assertNotIn("port", BASE)  # the port is optional; ssh's default it is
        runner = _Runner()
        with mock.patch("vigil_agent.netcheck.shutil.which", return_value="/usr/bin/ssh"), \
                mock.patch("vigil_agent.netcheck.subprocess.run", side_effect=runner):
            out = run_check(dict(BASE))
        argv = runner.calls[0][0]
        self.assertEqual(argv[argv.index("-p") + 1], "22")
        self.assertTrue(out["ok"], out)

    def test_a_run_object_without_an_int_returncode_is_read_as_one_failure(self):
        # a mock stand-in for CompletedProcess; the real one always carries an int,
        # and anything else than that must not be mistaken for a good backup
        out, _runner = self.run_ssh(result=mock.Mock(stdout=b"ok\n", stderr=b"", returncode=None))
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "ssh exited 1")

    def test_an_empty_host_key_is_treated_as_absent(self):
        out, runner = self.run_ssh({"host_key": ""})
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(out["host_key"])


class SecretTests(_Fixture):
    def test_the_private_key_appears_nowhere_in_a_result(self):
        for result in (self.run_ssh()[0],
                       self.run_ssh(result=_completed(stderr=b"nope\n", returncode=255))[0],
                       self.run_ssh(
                           error=subprocess.TimeoutExpired(cmd=["ssh"], timeout=SSH_TIMEOUT))[0],
                       self.run_ssh(which=None)[0]):
            with self.subTest(detail=result["detail"]):
                dumped = json.dumps(result)
                self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", dumped)
                self.assertNotIn("b3BlbnNzaC1rZXktdjEA", dumped)

    def test_the_key_file_path_appears_nowhere_in_a_result(self):
        out, runner = self.run_ssh()
        self.assertNotIn(runner.dirs[0], json.dumps(out))
        self.assertNotIn("vigil-ssh-", json.dumps(out))

    def test_a_poller_without_an_ssh_client_leaks_nothing_either(self):
        out, _runner = self.run_ssh(which=None)
        dumped = json.dumps(out)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", dumped)
        self.assertIsNone(out["output"])
        self.assertIsNone(out["sha256"])


class TempDirTests(_Fixture):
    def test_the_temporary_directory_carries_the_vigil_prefix_and_mode(self):
        seen = {}

        class _Probe(_Runner):
            def __call__(self, argv, **kwargs):
                directory = os.path.dirname(argv[argv.index("-i") + 1])
                seen["name"] = os.path.basename(directory)
                seen["mode"] = os.stat(directory).st_mode & 0o777
                return super().__call__(argv, **kwargs)

        probe = _Probe()
        with mock.patch("vigil_agent.netcheck.shutil.which", return_value="/usr/bin/ssh"), \
                mock.patch("vigil_agent.netcheck.subprocess.run", side_effect=probe):
            run_check(dict(BASE))
        self.assertTrue(seen["name"].startswith("vigil-ssh-"), seen["name"])
        if os.name != "nt":
            self.assertEqual(seen["mode"], 0o700)

    def test_nothing_is_left_behind_in_the_temp_root(self):
        leftovers = _leftovers()
        self.run_ssh()
        self.assertEqual(_leftovers(), leftovers)


def _padded_key() -> str:
    header = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    return header + "A" * (16 * 1024 - len(header) - 1) + "\n"


def _leftovers() -> list[str]:
    return [p for p in os.listdir(tempfile.gettempdir()) if p.startswith("vigil-ssh-")]


if __name__ == "__main__":
    unittest.main()
