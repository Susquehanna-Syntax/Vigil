"""The configure-before-you-install UI, checked without a browser.

QA-35 shipped an Add Host command that threw its token away, and the suite
stayed green: the string rendered, so nothing looked at what bash would do with
it. These are the same lesson applied to QA-64's wizard — the install command is
*executed* under bash with a fake download, and the assertion is that the
one-time config token reaches the installer's environment. Plus the structural
checks that catch the other failure mode: a renamed function or a markup block
that stopped existing, both of which fail silently in a browser.
"""
import re
import shutil
import subprocess
from pathlib import Path
from unittest.case import skipUnless

from django.conf import settings
from django.test import SimpleTestCase

STATIC = Path(settings.BASE_DIR) / "static" / "js"
TEMPLATES = Path(settings.BASE_DIR) / "templates"

#: The command constants and builders, lifted verbatim from vigil-enroll.js and
#: run under node in the order the page runs them: the constants are defined
#: while the tokens are still null, and filled in only when a command is made.
#: A constant that interpolates at load time (a backtick string) therefore
#: fails here with VIGIL_TOKEN=null instead of passing.
_CONSTANT = r"^const _ENROLL_{}_CMD = .*$"
_BUILDER = r"^function _enroll{}Cmd\(\) \{{\n.*?^\}}$"


def _js(name: str) -> str:
    return (STATIC / name).read_text()


def _command(kind: str, config_token: str | None) -> str:
    """Run the wizard's builder for *kind* ("Linux" or "Windows") in node."""
    js = _js("vigil-enroll.js")
    constant = re.search(_CONSTANT.format({"Linux": "LINUX", "Windows": "WIN"}[kind]),
                         js, re.M)
    builder = re.search(_BUILDER.format(kind), js, re.M | re.S)
    assert constant and builder, f"no {kind} command constant or builder"
    program = (
        'var origin = "https://dashboard.invalid";\n'
        "let _enrollToken = null, _enrollConfigToken = null, _enrollServer = null;\n"
        f"{constant.group(0)}\n{builder.group(0)}\n"
        '_enrollToken = "tok123";\n'
        '_enrollServer = "https://relay.test";\n'
        f"_enrollConfigToken = {config_token and repr(config_token) or 'null'};\n"
        f"process.stdout.write(_enroll{kind}Cmd());\n"
    )
    out = subprocess.run(["node", "-e", program], capture_output=True,
                         text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout


@skipUnless(shutil.which("node"), "node is unavailable")
class LinuxCommandDeliversTheConfigToken(SimpleTestCase):
    """The command must hand VIGIL_CONFIG_TOKEN to the installer, not to curl.

    ``curl … | sudo env VIGIL_TOKEN=x bash`` names in ``sudo env`` exactly what
    the downloaded script sees; a token spliced anywhere else — after ``bash``,
    or in front of ``curl`` — is dropped on the floor. So this runs the command
    with curl replaced by something that prints both variables back.
    """

    def test_bash_receives_both_the_enroll_token_and_the_config_token(self):
        command = _command("Linux", "cfg456")
        # The download comes from the address the agent will check in with, so
        # a host that can only see its relay can still fetch the installer.
        self.assertIn("curl -fsSL https://relay.test/agent/install.sh", command)
        run = command.replace(
            "curl -fsSL https://relay.test/agent/install.sh",
            'printf %s \'printf "%s %s" "$VIGIL_TOKEN" "$VIGIL_CONFIG_TOKEN"\'',
        ).replace("sudo ", "")
        result = subprocess.run(["bash", "-c", run], capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(
            result.stdout, "tok123 cfg456",
            f"the tokens did not reach the installer (stderr: {result.stderr})")

    def test_without_a_config_token_the_command_is_the_plain_one(self):
        """Nothing is spliced before a command exists."""
        command = _command("Linux", None)
        self.assertNotIn("VIGIL_CONFIG_TOKEN", command)
        self.assertIn("VIGIL_TOKEN=tok123 bash", command)


@skipUnless(shutil.which("node"), "node is unavailable")
class WindowsCommandSetsTheConfigToken(SimpleTestCase):
    """PowerShell needs the assignment as its own statement, before irm runs."""

    def test_the_assignment_precedes_the_download(self):
        command = _command("Windows", "cfg456")
        self.assertTrue(command.startswith('$env:VIGIL_TOKEN = "tok123"; '), command)
        self.assertIn('; $env:VIGIL_CONFIG_TOKEN = "cfg456"; irm https://relay.test/agent/install.ps1', command)


class FormSourceChecks(SimpleTestCase):
    """A form function that stopped existing is a dialog with dead buttons."""

    def test_the_form_defines_its_entry_points(self):
        js = _js("vigil-agentprofile-form.js")
        for name in ("apfRender", "apfRead", "apfShowErrors", "apfLoadContext"):
            self.assertIn(f"function {name}", js, f"{name} is not defined")

    def test_the_wizard_creates_commands_and_saves_profiles(self):
        js = _js("vigil-enroll.js")
        for name in ("enrollCreateCommand", "enrollSaveProfile"):
            self.assertIn(f"async function {name}", js, f"{name} is not defined")

    def test_the_wizard_no_longer_splices_a_mode(self):
        """The mode is in the profile now. Splicing VIGIL_MODE as well would
        silently override whatever the admin configured."""
        self.assertNotIn("VIGIL_MODE", _js("vigil-enroll.js"))

    def test_the_reimage_tab_lists_profiles(self):
        self.assertIn("async function loadAgentProfiles",
                      _js("vigil-agentprofiles.js"))

    def test_base_html_loads_both_files_and_mounts_the_form(self):
        html = (TEMPLATES / "base.html").read_text()
        for name in ("vigil-agentprofile-form.js", "vigil-agentprofiles.js"):
            self.assertIn(name, html, f"{name} is not loaded by base.html")
        self.assertIn('id="enroll-profile"', html)
        self.assertIn('id="enroll-config"', html)
        self.assertIn('data-act="enrollCreateCommand"', html)
        self.assertIn('data-act="enrollSaveProfile"', html)

    def test_the_command_block_starts_hidden_and_says_how_long_it_lives(self):
        """A command shown before the settings are minted installs defaults."""
        html = (TEMPLATES / "base.html").read_text()
        block = re.search(r'<div[^>]*id="enroll-cmd-block"[^>]*>', html)
        assert block, "no #enroll-cmd-block"
        self.assertIn("hidden", block.group(0))
        self.assertIn("Valid for 24 hours, once.", html)

    def test_reprovision_has_the_agent_profiles_tab(self):
        html = (TEMPLATES / "pages" / "_reprovision.html").read_text()
        self.assertIn('data-tab="repro-tab-agentprofiles"', html)
        self.assertIn('id="agent-profiles-list"', html)
        self.assertIn('id="repro-prof-agentprofile"', html)

    def test_the_install_profile_sends_its_agent_profile(self):
        js = _js("vigil-reprovision.js")
        save = re.search(r"async function _reproSaveProfile\(\).*?\n  \};", js,
                         re.DOTALL)
        assert save, "no _reproSaveProfile"
        self.assertIn("agent_profile:", save.group(0),
                      "the install-profile form does not send agent_profile")


class NoInlineHandlersInChangedFiles(SimpleTestCase):
    INLINE = re.compile(r'\bon(click|change|input|submit|focus|blur)\s*=\s*"')

    def test_the_changed_files_carry_no_inline_handler(self):
        offenders = []
        for path in list(STATIC.glob("vigil-agentprofile*.js")) + [
                STATIC / "vigil-enroll.js", STATIC / "vigil-reprovision.js"]:
            for no, line in enumerate(path.read_text().splitlines(), 1):
                if self.INLINE.search(line):
                    offenders.append(f"{path.name}:{no}")
        self.assertEqual(offenders, [])
