"""install.sh must run under the awk Debian and Ubuntu actually ship.

The digest check added in 2026.12.0 extracted the X-Vigil-SHA256 header with
`awk 'BEGIN{IGNORECASE=1} /^x-vigil-sha256:/ ...'`. IGNORECASE is a gawk
extension. Debian and Ubuntu default to mawk, which accepts the assignment and
ignores it, so the lowercase pattern never matched the real capitalised header.
The digest came back empty and the script refused to install — on the platform
most installs run on.

Nothing caught it: the template rendered, the suite passed, and the failure
only appeared when the script ran on a machine with mawk. These tests are the
cheap half of that lesson. The expensive half is scripts/smoke-browser.py's
sibling problem — some things must actually be executed.
"""
import re
import shutil
import subprocess

from django.template.loader import render_to_string
from django.test import SimpleTestCase

#: Response headers as curl -D writes them: capitalised, CRLF line endings.
HEADERS = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/octet-stream\r\n"
    "X-Vigil-Version: 2026.12.0\r\n"
    "X-Vigil-SHA256: A0839DF80FC21753F173FB6E348691100B2455FB\r\n"
    "\r\n"
)
EXPECTED = "a0839df80fc21753f173fb6e348691100b2455fb"

#: Constructs only GNU awk understands. mawk ignores or rejects these.
GAWK_ONLY = ("IGNORECASE", "gensub(", "asort(", "asorti(", "strftime(",
             "systime(", "toupper($0) ~ /" "", "ENVIRON[")


class InstallScriptUsesPortableAwk(SimpleTestCase):
    def setUp(self):
        self.script = render_to_string(
            "agent_install.sh", {"base_url": "https://vigil.example.com"})

    @staticmethod
    def _without_comments(script):
        """Whole-line shell comments dropped.

        The comment above the fixed line names IGNORECASE to explain why it is
        not used. Scanning raw text reports that explanation as the offence —
        the third time in this codebase that a guard has flagged the prose
        documenting it, after the CSP and inline-handler sweeps.
        """
        return "\n".join(
            ln for ln in script.splitlines() if not ln.lstrip().startswith("#")
        )

    def test_the_script_uses_no_gawk_only_constructs(self):
        code = self._without_comments(self.script)
        found = [c for c in GAWK_ONLY if c and c in code]
        self.assertEqual(
            found, [],
            "install.sh uses gawk-only awk features, which silently do nothing "
            f"under the mawk that Debian and Ubuntu ship: {found}",
        )

    def _awk_program(self):
        """The single-quoted awk program out of the EXPECTED_SHA line."""
        for line in self.script.splitlines():
            if line.startswith("EXPECTED_SHA=") and "awk" in line:
                return line.split("'")[1]
        self.fail("no EXPECTED_SHA awk line in install.sh")

    def _run_under(self, awk_bin):
        return subprocess.run(
            [awk_bin, self._awk_program()],
            input=HEADERS, capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    def test_the_digest_is_extracted_by_the_system_awk(self):
        self.assertEqual(self._run_under("awk"), EXPECTED)

    def test_the_digest_is_extracted_by_mawk(self):
        """The one that would have caught the bug. Skipped where mawk is
        absent, because then the system awk above is already gawk."""
        mawk = shutil.which("mawk")
        if not mawk:
            self.skipTest("mawk not installed")
        self.assertEqual(self._run_under(mawk), EXPECTED)

    def test_the_digest_is_extracted_by_gawk(self):
        gawk = shutil.which("gawk")
        if not gawk:
            self.skipTest("gawk not installed")
        self.assertEqual(self._run_under(gawk), EXPECTED)


class BothInstallersVerifyBeforeInstalling(SimpleTestCase):
    """install.ps1 must refuse an unverified binary, like install.sh does.

    It did not, for the whole of 2026. The digest check landed on the Linux
    side in 3083b15 and the Windows side was never brought along, so Windows
    downloaded whatever bytes arrived and ran them as a LocalSystem service —
    the same attack the Linux script refuses, at a higher privilege level.

    Nothing caught it because nothing compared the two installers. That is what
    this class is for.
    """

    #: PowerShell comments: <# block #> and # to end of line.
    PS_BLOCK = re.compile(r"<#.*?#>", re.DOTALL)

    @classmethod
    def _strip_ps_comments(cls, script):
        """Comments blanked, newlines kept.

        This is the fourth guard in this codebase to flag the prose documenting
        it — after the CSP sweep, the inline-handler sweep and the gawk scan.
        The comment below the download `catch` says "Write-Host, not
        Write-Error" and explains why; scanning raw text reads that as the
        offence. If you add a guard here, strip comments first.
        """
        script = cls.PS_BLOCK.sub(
            lambda m: re.sub(r"[^\n]", " ", m.group(0)), script)
        return "\n".join(
            ln for ln in script.splitlines() if not ln.lstrip().startswith("#")
        )

    def setUp(self):
        self.sh = render_to_string(
            "agent_install.sh", {"base_url": "https://vigil.example.com"})
        self.ps1 = render_to_string(
            "agent_install.ps1", {"base_url": "https://vigil.example.com"})
        self.ps1_code = self._strip_ps_comments(self.ps1)

    def test_powershell_installer_computes_a_sha256(self):
        self.assertIn("Get-FileHash", self.ps1)
        self.assertIn("SHA256", self.ps1)

    def test_powershell_installer_reads_the_digest_header(self):
        self.assertIn("x-vigil-sha256", self.ps1.lower())

    def test_powershell_installer_does_not_download_straight_to_the_service_binary(self):
        """Download to temp, verify, then move.

        Writing the download directly to $BinaryPath means a failed or tampered
        download has already replaced the installed agent by the time anything
        is checked — and the check then has nothing left to protect.
        """
        for line in self.ps1_code.splitlines():
            if "Invoke-WebRequest" in line and "-OutFile" in line:
                self.assertNotIn(
                    "$BinaryPath", line,
                    "install.ps1 downloads directly onto the service binary; "
                    "download to a temp path and Move-Item after verifying:\n"
                    f"  {line.strip()}",
                )

    def test_powershell_installer_restricts_the_config_acl(self):
        """agent.yml carries the agent token.

        install.sh writes it 0600. C:\\ProgramData's default ACL lets any local
        user read it, so the Windows side needs an explicit icacls.
        """
        self.assertIn("icacls", self.ps1)
        self.assertIn("/inheritance:r", self.ps1)

    def test_both_installers_offer_the_same_override(self):
        """One documented escape hatch, spelled the same way in both."""
        for name, script in (("install.sh", self.sh), ("install.ps1", self.ps1)):
            self.assertIn(
                "VIGIL_ALLOW_UNVERIFIED_AGENT", script,
                f"{name} has no documented override for the digest check",
            )

    def test_powershell_installer_reports_failure_without_a_stack_trace(self):
        """Write-Error emits CategoryInfo, FullyQualifiedErrorId and a caret
        diagram of the failing line. install.sh prints one line; a download
        failure is an ordinary condition, not a crash."""
        self.assertNotIn("Write-Error", self.ps1_code)


class TheGeneratedAgentConfigIsValidYaml(SimpleTestCase):
    """install.ps1 must write a config the agent can actually parse.

    It did not. `data_dir: "C:\\ProgramData\\Vigil\\data"` is a double-quoted
    YAML scalar, which processes backslash escapes the way JSON does — so it
    fails on \\P with

        yaml.scanner.ScannerError: found unknown escape character 'V'

    and the agent exits before its first check-in. Nothing caught it because
    the Windows service could not start for an unrelated reason, so the broken
    config was written and never read.

    Rendering the template is not enough; the YAML has to be parsed.
    """

    #: Realistic values for the PowerShell variables the heredoc interpolates.
    PS_VARS = {
        "$VigilServer": "https://vigil.example.com",
        "$token": "a" * 64,
        "$DataDir": r"C:\ProgramData\Vigil\data",
        "$ConfigPath": r"C:\ProgramData\Vigil\agent.yml",
    }

    def _config_block(self, script):
        """The YAML between the PowerShell here-string markers."""
        m = re.search(r'@"\n(.*?)\n"@', script, re.DOTALL)
        self.assertIsNotNone(m, "no here-string found in install.ps1")
        block = m.group(1)
        for var, val in self.PS_VARS.items():
            block = block.replace(var, val)
        return block

    def test_the_config_install_ps1_writes_parses_as_yaml(self):
        import yaml

        script = render_to_string(
            "agent_install.ps1", {"base_url": "https://vigil.example.com"})
        block = self._config_block(script)
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            self.fail(
                "install.ps1 writes an agent.yml the agent cannot parse — it "
                f"will exit at startup:\n{exc}\n\n--- config ---\n{block}"
            )
        self.assertIn("server_url", parsed)
        self.assertIn("agent_token", parsed)
        self.assertEqual(
            parsed.get("data_dir"), r"C:\ProgramData\Vigil\data",
            "data_dir did not survive YAML parsing with its backslashes intact",
        )
