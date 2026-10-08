"""QA-17: one styled TOTP dialog, a labelled host control, aligned columns.

The dialog is a promise built in a browser, so these tests read the sources
as text: what matters is that no caller slipped back to the bare
`window.prompt`, that the dialog's markup keeps the accessibility and
autofill attributes, and that the containers table declares the column widths
that make every stack card line up.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"

#: Every file that used to ask for a code with window.prompt().
TOTP_CALLERS = (
    "vigil-host-cards.js",
    "vigil-containers.js",
    "vigil-firewall.js",
    "vigil-policies.js",
    "vigil-vulns.js",
    "vigil-stacks.js",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TotpDialogTests(SimpleTestCase):

    def test_no_bare_prompt_for_totp(self):
        for js in sorted(JS.glob("*.js")):
            src = _read(js)
            for match in re.finditer(r"window\.prompt\(", src):
                window = src[match.start():match.start() + 200]
                self.assertNotIn(
                    "TOTP", window.upper(),
                    f"{js.name} still prompts for a TOTP in a bare dialog",
                )

    def test_every_caller_uses_the_shared_dialog(self):
        for name in TOTP_CALLERS:
            src = _read(JS / name)
            self.assertIn("totpPrompt(", src, f"{name} never uses totpPrompt(")
            self.assertNotIn("window.prompt(", src,
                             f"{name} still calls window.prompt(")

    def test_totp_dialog_shape(self):
        utils = _read(JS / "vigil-utils.js")
        self.assertIn("function totpPrompt(", utils)
        self.assertIn('autocomplete="one-time-code"', utils)
        self.assertIn('inputmode="numeric"', utils)
        self.assertIn('maxlength="6"', utils)
        dialog = utils.split("function totpPrompt(", 1)[1]
        self.assertTrue(
            re.search(r"(length\s*[!=]==?\s*6|/\\\^\\d\{6\}\\$/)", dialog),
            "Confirm is not gated on exactly six digits",
        )

    def test_reason_goes_in_as_text_not_markup(self):
        body = _read(JS / "vigil-utils.js").split("function totpPrompt(", 1)[1]
        self.assertIn(".textContent = reason", body)
        self.assertNotIn("innerHTML = reason", body)


class ContainersControlTests(SimpleTestCase):

    def test_containers_host_control(self):
        page = _read(TEMPLATES / "pages" / "_containers.html")
        self.assertIn('class="fw-controls-label" for="containers-host"', page)
        self.assertIn('id="containers-host"', page)
        self.assertIn('data-change="selectContainersHost"', page)
        self.assertIn('data-a1="$value"', page)
        self.assertIn('class="form-control ctr-host-select"', page)


class ContainersColumnTests(SimpleTestCase):

    def test_columns_line_up(self):
        js = _read(JS / "vigil-containers.js")
        self.assertIn("<colgroup>", js)
        for cls in ("ctr-c-name", "ctr-c-image", "ctr-c-state",
                    "ctr-c-cpu", "ctr-c-mem", "ctr-c-acts"):
            self.assertIn(f'col class="{cls}"', js)

        css = _read(ROOT / "static" / "css" / "vigil.css")
        self.assertRegex(
            css, r"\.ctr-table\s*\{[^}]*table-layout:\s*fixed",
            ".ctr-table never switches to fixed layout",
        )
        for cls in ("ctr-c-name", "ctr-c-image", "ctr-c-state",
                    "ctr-c-cpu", "ctr-c-mem", "ctr-c-acts"):
            self.assertRegex(css, rf"\.{cls}\s*\{{[^}}]*width:\s*\d+%")
