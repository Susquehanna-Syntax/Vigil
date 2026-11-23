"""install.sh on FreeBSD: the platform it picks and the rc.d service it writes.

FreeBSD has no systemd and no launchd, so the agent runs as an rc.d script
under ``daemon -r`` (the restart-on-exit flag the update rollback depends on),
and its config lives under /usr/local/etc like every other third-party package
there rather than /etc/vigil.

Like the Apple Silicon test, this lifts blocks out of the rendered template and
runs them with ``uname`` stubbed, so what is checked is the script a FreeBSD
host actually gets.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase

TEMPLATE = Path(settings.BASE_DIR) / "templates" / "agent_install.sh"
KEY = "q1Ivm7G1oQ0kq5mH0q2N4u3mXyZ9b8F6c5d4e3f2a1A="

#: The rc.d script install.sh writes, lifted out of the rendered template.
RC_SCRIPT = re.compile(r"<< 'EOF'\n(#!/bin/sh\n.*?)\nEOF", re.MULTILINE | re.DOTALL)


def _script() -> str:
    return render_to_string("agent_install.sh",
                            {"base_url": "https://vigil.example.com", "public_key": KEY})


def _check_sh(text: str) -> str:
    """sh -n on *text*; the stderr a POSIX shell would refuse it with."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "script.sh"
        path.write_text(text)
        result = subprocess.run(["sh", "-n", str(path)], capture_output=True,
                                text=True, timeout=30, check=False)
    return result.stderr


class FreeBSDPlatformTests(SimpleTestCase):
    """The detection block, run with uname stubbed to a FreeBSD host."""

    def _run(self, sysname: str, machine: str):
        script = _script()
        match = re.search(r"^OS=\$\(uname -s.*?^esac\n", script,
                          re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "no platform block in agent_install.sh")
        body = match.group(0) + 'echo "PLATFORM=$PLATFORM"\n'
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp)
            (stubs / "uname").write_text(
                f'#!/bin/sh\nif [ "$1" = "-s" ]; then echo {sysname}; else echo {machine}; fi\n')
            (stubs / "sysctl").write_text("#!/bin/sh\necho 0\n")
            for stub in stubs.iterdir():
                stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
            return subprocess.run(["sh", "-c", body], capture_output=True,
                                  text=True, env=env, timeout=30, check=False)

    def test_amd64_gets_the_freebsd_build(self):
        result = self._run("FreeBSD", "amd64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLATFORM=freebsd-amd64", result.stdout)

    def test_x86_64_is_the_same_build(self):
        # Some jails and shells report the kernel's name for the architecture.
        result = self._run("FreeBSD", "x86_64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLATFORM=freebsd-amd64", result.stdout)

    def test_another_architecture_is_refused_with_a_reason(self):
        result = self._run("FreeBSD", "arm64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported architecture", result.stderr)
        self.assertNotIn("PLATFORM=", result.stdout)


class FreeBSDServiceTests(SimpleTestCase):
    """What the installer writes on a FreeBSD host."""

    def setUp(self):
        self.script = _script()
        # Comments explain what each platform does not have ("FreeBSD has no
        # systemctl"), so guards that look for another platform's service
        # manager read the prose documenting their own absence. Strip them.
        self.script_code = "\n".join(
            ln for ln in self.script.splitlines() if not ln.lstrip().startswith("#")
        )

    def _rc_block(self) -> str:
        """The freebsd service section, up to the next platform's elif."""
        start = self.script.index('elif [ "$OS" = "freebsd" ]; then')
        end = self.script.index('elif [ "$OS" = "darwin" ]; then', start)
        return self.script[start:end]

    def _rc_block_code(self) -> str:
        block = self._rc_block()
        # The rc.d script is itself a shell script whose comments belong to it.
        keep, in_rc = [], False
        for ln in block.splitlines():
            if ln.startswith("cat > /usr/local/etc/rc.d/vigil_agent"):
                in_rc = True
            elif ln == "EOF":
                in_rc = False
            if in_rc or not ln.lstrip().startswith("#"):
                keep.append(ln)
        return "\n".join(keep)

    def _rc_script(self) -> str:
        match = RC_SCRIPT.search(self.script)
        self.assertIsNotNone(match, "no rc.d script heredoc in agent_install.sh")
        return match.group(1)

    def test_the_freebsd_service_section_comes_before_macos(self):
        # Both are BSDs; the ordering also proves the elif chain is intact.
        self.assertLess(self.script.index('elif [ "$OS" = "freebsd" ]'),
                        self.script.index('elif [ "$OS" = "darwin" ]'))

    def test_the_rc_d_script_is_written_mode_0755(self):
        block = self._rc_block()
        self.assertIn("cat > /usr/local/etc/rc.d/vigil_agent", block)
        self.assertIn("chmod 0755 /usr/local/etc/rc.d/vigil_agent", block)

    def test_the_rc_d_script_content(self):
        """The exact script a FreeBSD host boots.

        ``daemon -r`` is the load-bearing flag: it restarts the agent when it
        exits, and the update rollback asks the service manager to restart and
        counts on something bringing the new binary back.
        """
        body = self._rc_script()
        daemon_args = ('command_args="-r -P ${pidfile} -o /var/log/vigil-agent.log '
                       '/usr/local/bin/vigil-agent -c /usr/local/etc/vigil/agent.yml"')
        for line in (
            "# PROVIDE: vigil_agent",
            "# REQUIRE: LOGIN NETWORKING",
            "# KEYWORD: shutdown",
            ". /etc/rc.subr",
            'name="vigil_agent"',
            'rcvar="vigil_agent_enable"',
            'command="/usr/sbin/daemon"',
            'pidfile="/var/run/${name}.pid"',
            'procname="/usr/local/bin/vigil-agent"',
            daemon_args,
            "load_rc_config $name",
            ': ${vigil_agent_enable:="NO"}',
            'run_rc_command "$1"',
        ):
            self.assertIn(line, body)

    def test_the_service_is_enabled_with_sysrc(self):
        self.assertIn("sysrc vigil_agent_enable=YES", self.script)

    def test_it_starts_the_same_way_the_other_platforms_decide(self):
        block = self._rc_block()
        self.assertIn('if [ -n "${VIGIL_TOKEN:-}" ]', block)
        self.assertIn("service vigil_agent start", block)
        self.assertIn("2. Approve the host in Vigil Settings > Enrollment Queue", block)

    def test_config_lives_under_usr_local_etc(self):
        # FreeBSD's /etc is for the base system; packages live in /usr/local.
        # Checked against the comment-stripped script: a path that only ever
        # appears in prose would satisfy the raw-text form of this assertion.
        self.assertIn('CONF_DIR=/usr/local/etc/vigil', self.script_code)
        self.assertIn('if [ "$OS" = "freebsd" ]; then\n  CONF_DIR=', self.script_code)

    def test_no_service_manager_from_another_platform_leaks_in(self):
        # Comments are stripped: they name systemd and launchd to explain what
        # FreeBSD does not have. The rc.d script's own comments stay, since the
        # PROVIDE line this file asserts on lives among them.
        block = self._rc_block_code()
        for other in ("systemctl", "launchctl", "brew"):
            self.assertNotIn(other, block)

    def test_the_rc_d_script_names_the_freebsd_paths_literally(self):
        body = self._rc_script()
        self.assertIn("/usr/local/etc/vigil/agent.yml", body)
        self.assertNotIn("CONF_DIR", body)

    def test_config_lives_under_usr_local_etc_in_code_not_only_comments(self):
        self.assertIn('  CONF_DIR=/usr/local/etc/vigil', self.script_code)
        self.assertIn('sysrc vigil_agent_enable=YES', self.script_code)


class RenderedInstallerIsValidPosixSh(SimpleTestCase):
    """The rendered script must parse under a plain POSIX shell.

    On FreeBSD the installer's service section and the rc.d script it writes
    both run under /bin/sh — bash is not installed there by default, so a
    bashism is a syntax error at the first line that uses it, on every install.
    sh -n reports it without executing anything. The Django tags are gone by
    now: only the rendered text is checked, never the raw template.
    """

    def test_the_rendered_script_parses_under_sh_dash_n(self):
        self.assertEqual(_check_sh(_script()), "")

    def test_the_rc_d_script_parses_under_sh_dash_n(self):
        match = RC_SCRIPT.search(_script())
        self.assertIsNotNone(match, "no rc.d script heredoc in agent_install.sh")
        self.assertEqual(_check_sh(match.group(1) + "\n"), "")
