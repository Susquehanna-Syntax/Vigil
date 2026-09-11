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
