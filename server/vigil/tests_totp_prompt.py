"""Every confirm-with-a-code goes through totpPrompt (vigil-utils.js).

One dialog means one look, one Escape behaviour, and one place for a second
kind of factor (a hardware key) to land later. The only code field left on a
page is first-run setup's, where the code proves the app was enrolled."""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]


class TotpPromptTests(SimpleTestCase):
    def test_no_page_has_its_own_code_field(self):
        allowed = {"setup.html"}
        found = [str(p.relative_to(ROOT)) for p in (ROOT / "templates").rglob("*.html")
                 if p.name not in allowed and "one-time-code" in p.read_text(encoding="utf-8")]
        found += [str(p.relative_to(ROOT)) for p in (ROOT / "static/js").glob("*.js")
                  if p.name != "vigil-utils.js" and "one-time-code" in p.read_text(encoding="utf-8")]
        self.assertEqual(found, [])

    def test_no_code_is_asked_with_the_browser_prompt(self):
        for p in (ROOT / "static/js").glob("*.js"):
            for line in re.findall(r"[^.\w]prompt\([^)]*\)", p.read_text(encoding="utf-8")):
                self.assertNotRegex(line.lower(), r"code|totp", f"{p.name}: {line}")

    def test_the_prompt_takes_a_title_and_keeps_escape_to_itself(self):
        js = (ROOT / "static/js/vigil-utils.js").read_text(encoding="utf-8")
        body = js[js.index("function totpPrompt("):js.index("function yamlToHtml(")]
        self.assertIn("opts.title", body)
        self.assertIn("ev.stopPropagation()", body)
