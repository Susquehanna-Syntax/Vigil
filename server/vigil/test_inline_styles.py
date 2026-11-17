"""A ratchet on inline `style="` attributes (S6 08a).

The CSP keeps `style-src 'unsafe-inline'` for exactly one reason: templates and
generated markup still carry inline style attributes. Dropping it takes several
batches, so this file holds the line while they happen — no file may gain an
inline style, and a file that loses them stays at zero.

The baseline is generated (see the phase doc), sorted by path, and lists only
files with a count above zero. When you move a look into a class in
static/css/vigil/, lower or delete the file's entry; when a batch is finished,
test_the_first_batch_is_done keeps it finished."""

import json
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
BASELINE_PATH = Path(__file__).with_name("inline_style_baseline.json")

TEMPLATE_GLOB = "templates/**/*.html"
SCRIPT_GLOB = "static/js/*.js"

FIRST_BATCH = [
    "templates/login.html",
    "templates/login_verify.html",
    "templates/setup.html",
    "templates/_host_card.html",
    "templates/pages/_alerts.html",
    "templates/pages/_community.html",
    "templates/pages/_inventory.html",
    "templates/pages/_firewall.html",
]

SECOND_BATCH = [
    "templates/pages/_about.html",
    "templates/pages/_reprovision.html",
    # Still ratcheted at its 2 remaining attributes, both bare
    # style="display:none;": scripts show those elements with
    # element.style.display = '', which a class could not undo.
    "templates/pages/_vulns.html",
]

# Still ratcheted at their remaining attributes, all bare style="display:none;":
# scripts show those elements with element.style.display = '', which a class
# could not undo (08c).
THIRD_BATCH = [
    "templates/pages/_monitor.html",
    "templates/pages/_playbooks.html",
]

_ALLOWED_LEFTOVER = re.compile(r'^style="display:none;"?$')


def scanned_files() -> list[Path]:
    """Every file the ratchet covers, sorted by its baseline-relative path."""
    return sorted(
        list(ROOT.glob(TEMPLATE_GLOB)) + list(ROOT.glob(SCRIPT_GLOB)),
        key=lambda p: str(p.relative_to(ROOT)),
    )


def count_inline_styles(path: Path) -> int:
    return path.read_text(encoding="utf-8").count('style="')


def load_baseline() -> dict[str, int]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


class InlineStyleRatchetTests(SimpleTestCase):
    def test_no_file_gains_an_inline_style(self):
        baseline = load_baseline()
        for path in scanned_files():
            rel = str(path.relative_to(ROOT))
            counted = count_inline_styles(path)
            allowed = baseline.get(rel, 0)
            self.assertLessEqual(
                counted, allowed,
                f"{rel} has {counted} inline style attributes and the baseline "
                f"allows {allowed}. Move the style into a class in "
                "static/css/vigil/, or set element.style from a script "
                "(allowed by the CSP).")

    def test_the_baseline_is_lowered_when_styles_go(self):
        baseline = load_baseline()
        for rel, expected in baseline.items():
            path = ROOT / rel
            self.assertTrue(path.is_file(),
                            f"{rel} is in vigil/inline_style_baseline.json but "
                            "no longer exists. Remove its entry.")
            counted = count_inline_styles(path)
            self.assertEqual(
                counted, expected,
                f"{rel} now has {counted} inline style attributes and the "
                f"baseline still says {expected}. Lower this file's number in "
                "vigil/inline_style_baseline.json.")

    def test_the_baseline_is_sorted_and_has_no_zeroes(self):
        raw = BASELINE_PATH.read_text(encoding="utf-8")
        baseline = json.loads(raw)
        self.assertEqual(list(baseline), sorted(baseline),
                         "vigil/inline_style_baseline.json must be sorted by path.")
        self.assertEqual([k for k, v in baseline.items() if v == 0], [],
                         "A file with no inline styles is left out of "
                         "vigil/inline_style_baseline.json rather than listed as 0.")
        self.assertIn('\n  "', raw,
                      "the baseline is 2-space indented, so its diffs read file by file")
        self.assertTrue(raw.endswith("\n"), "the baseline ends with a newline")

    def test_the_first_batch_is_done(self):
        for rel in FIRST_BATCH:
            text = (ROOT / rel).read_text(encoding="utf-8")
            leftover = [m.group(0) for m in re.finditer(r'style="[^"]*"', text)
                        if not _ALLOWED_LEFTOVER.match(m.group(0))]
            self.assertEqual(leftover, [],
                             f"{rel} was moved into classes in phase 08a; only "
                             'style="display:none;" may remain.')

    def test_the_second_batch_is_done(self):
        for rel in SECOND_BATCH:
            text = (ROOT / rel).read_text(encoding="utf-8")
            leftover = [m.group(0) for m in re.finditer(r'style="[^"]*"', text)
                        if not _ALLOWED_LEFTOVER.match(m.group(0))
                        and "{{" not in m.group(0) and "{%" not in m.group(0)]
            self.assertEqual(leftover, [],
                             f"{rel} was moved into classes in phase 08b; only "
                             'style="display:none;" may remain.')

    def test_the_third_batch_is_done(self):
        for rel in THIRD_BATCH:
            text = (ROOT / rel).read_text(encoding="utf-8")
            leftover = [m.group(0) for m in re.finditer(r'style="[^"]*"', text)
                        if not _ALLOWED_LEFTOVER.match(m.group(0))
                        and "{{" not in m.group(0) and "{%" not in m.group(0)]
            self.assertEqual(leftover, [],
                             f"{rel} was moved into classes in phase 08c; only "
                             'style="display:none;" may remain.')
