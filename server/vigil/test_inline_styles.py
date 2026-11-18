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

# The Settings page, cleared in 08d: what remains is bare style="display:none;"
# (scripts show those elements with element.style.display = '', which a class
# could not undo). The theme previews' colours are tokens in tokens.css.
FOURTH_BATCH = [
    "templates/pages/_settings.html",
]

# SCRIPT_BATCH_1 is done to nothing: no attribute there is one a class could not
# undo. SCRIPT_BATCH_2 keeps only attributes whose value is worked out per render
# — a state colour, a width — and is held to that exact count in place of the
# baseline entry this phase deleted, so its number stays honest as it goes down.
SCRIPT_BATCH_1 = [
    "static/js/vigil-api-keys.js",
]

SCRIPT_BATCH_2 = {
    # Seven state colours: six attributes that are one colour and nothing else,
    # plus the wave dot's background and glow, whose fixed declarations this
    # phase moved to .rlt-wave-dot.
    "static/js/vigil-rollout.js": 7,
}

# SCRIPT_BATCH_3 is done to nothing but the one alert dot, which stays in the
# baseline at 1: its colour comes from the alert's severity, so a class could
# not work it out per render.
SCRIPT_BATCH_3 = [
    "static/js/vigil-alerts.js",
    "static/js/vigil-instance-settings.js",
    "static/js/vigil-tasks.js",
]

# Phase 08h: the dashboard widget and Community scripts. What the widgets
# still write inline is worked out per render (a state colour, a bar width).
SCRIPT_BATCH_4 = [
    "static/js/vigil-community.js",
    "static/js/vigil-widgets.js",
]

# Phase 08j: the agent profile editor, wave list, AI compare panel and tag
# manager. Every attribute those four wrote moved into a class — nothing there
# was worked out per render, so no file of this batch is ratcheted any more.
SCRIPT_BATCH_5 = [
    "static/js/vigil-agentprofile-form.js",
    "static/js/vigil-waves.js",
    "static/js/vigil-ai.js",
    "static/js/vigil-tags.js",
]

# Phase 08k: the monitor, host drawer, status pages, settings, automations,
# vulns, sites, run history, playbooks and picker scripts. What those ten still
# write inline is a value worked out per render (a state colour, a bar width).
SCRIPT_BATCH_6 = [
    "static/js/vigil-monitor.js",
    "static/js/vigil-host-cards.js",
    "static/js/vigil-statuspages.js",
    "static/js/vigil-settings.js",
    "static/js/vigil-automations.js",
    "static/js/vigil-vulns.js",
    "static/js/vigil-sites.js",
    "static/js/vigil-runhistory.js",
    "static/js/vigil-playbooks.js",
    "static/js/vigil-pickers.js",
]

# Still held to their remaining inline style attributes: vigil-alerts.js,
# vigil-tasks.js and vigil-instance-settings.js are phase 08m. Every other
# finished file is checked against zero, so each may hold only a bare
# style="display:none;" or a value worked out per render written as data-css.
_ALLOWED_LEFTOVER = re.compile(r'^style="display:none;"?$')

# Phase 08l: the eight scripts whose styles are worked out per render. The CSP
# will refuse every style attribute in markup, so each of these writes
# data-css="…" instead and applyDataCss in vigil-utils.js applies it.
DATA_CSS_SCRIPTS = [
    "static/js/vigil-playbook-flow.js",
    "static/js/vigil-widgets.js",
    "static/js/vigil-rollout.js",
    "static/js/vigil-monitor.js",
    "static/js/vigil-charts.js",
    "static/js/vigil-jackil-shapes.js",
    "static/js/vigil-store.js",
    "static/js/vigil-host-cards.js",
]


def _leftover_styles(rel: str, script: bool) -> list[str]:
    """Every style attribute in a finished file that is neither bare
    display:none nor worked out per render. A template's per-render value is a
    ``{{`` or a ``{%``; a script's is a template literal's ``${``."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    markers = ("${",) if script else ("{{", "{%")
    return [m.group(0) for m in re.finditer(r'style="[^"]*"', text)
            if not _ALLOWED_LEFTOVER.match(m.group(0))
            and not any(marker in m.group(0) for marker in markers)]


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
            # SCRIPT_BATCH_2 files left the baseline as they were cleared; their
            # pinned counts live with the batch list instead.
            allowed = baseline.get(rel, SCRIPT_BATCH_2.get(rel, 0))
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
            self.assertEqual(_leftover_styles(rel, script=False), [],
                             f"{rel} was moved into classes in phase 08a; only "
                             'style="display:none;" may remain.')

    def test_the_second_batch_is_done(self):
        for rel in SECOND_BATCH:
            self.assertEqual(_leftover_styles(rel, script=False), [],
                             f"{rel} was moved into classes in phase 08b; only "
                             'style="display:none;" may remain.')

    def test_scripts_write_per_render_styles_as_data_css(self) -> None:
        for rel in DATA_CSS_SCRIPTS:
            with self.subTest(script=rel):
                self.assertEqual(
                    _leftover_styles(rel, script=False), [],
                    f"{rel} works a style out per render and must write it as "
                    'data-css="…" for applyDataCss to apply; only '
                    'style="display:none;" may remain.')

    def test_the_third_batch_is_done(self):
        for rel in THIRD_BATCH:
            self.assertEqual(_leftover_styles(rel, script=False), [],
                             f"{rel} was moved into classes in phase 08c; only "
                             'style="display:none;" may remain.')

    def test_the_fourth_batch_is_done(self):
        for rel in FOURTH_BATCH:
            self.assertEqual(_leftover_styles(rel, script=False), [],
                             f"{rel} was moved into classes in phase 08d; only "
                             'style="display:none;" may remain.')

    def test_the_first_script_batch_is_done(self):
        for rel in SCRIPT_BATCH_1:
            self.assertEqual(_leftover_styles(rel, script=True), [],
                             f"{rel} was moved into classes in phase 08g; only "
                             'style="display:none;" or a value worked out per '
                             "render may remain.")

    def test_the_second_script_batch_is_cleared_to_its_per_render_colours(self):
        for rel in SCRIPT_BATCH_2:
            self.assertEqual(_leftover_styles(rel, script=True), [],
                             f"{rel} was moved into classes in phase 08g; only "
                             "a state colour worked out per render may remain.")

    def test_the_third_script_batch_is_done(self):
        for rel in SCRIPT_BATCH_3:
            self.assertEqual(_leftover_styles(rel, script=True), [],
                             f"{rel} was moved into classes in phase 08i; only "
                             "a state colour worked out per render may remain.")

    def test_base_is_done(self):
        self.assertEqual(_leftover_styles("templates/base.html", script=False), [],
                         "templates/base.html was moved into classes in phases "
                         '08e and 08f; only style="display:none;" may remain.')

    def test_the_fourth_script_batch_is_done(self):
        for rel in SCRIPT_BATCH_4:
            self.assertEqual(_leftover_styles(rel, script=True), [],
                             f"{rel} was moved into classes in phase 08h; only "
                             'style="display:none;" or a value worked out per '
                             "render may remain.")

    def test_the_fifth_script_batch_is_done(self):
        for rel in SCRIPT_BATCH_5:
            self.assertEqual(_leftover_styles(rel, script=True), [],
                             f"{rel} was moved into classes in phase 08j; only "
                             'style="display:none;" or a value worked out per '
                             "render may remain.")

    def test_the_sixth_script_batch_is_done(self):
        for rel in SCRIPT_BATCH_6:
            self.assertEqual(_leftover_styles(rel, script=True), [],
                             f"{rel} was moved into classes in phase 08k; only "
                             'style="display:none;" or a value worked out per '
                             "render may remain.")

    def test_templates_carry_only_display_none(self):
        # Phase 08m: the last per-render template values moved to scripts and
        # classes. A display:none stays because scripts show those elements by
        # clearing element.style.display; the CSP allows exactly that string by
        # hash ('unsafe-hashes'), so nothing else may appear.
        for path in sorted(ROOT.glob(TEMPLATE_GLOB)):
            rel = str(path.relative_to(ROOT))
            self.assertEqual(_leftover_styles(rel, script=False), [],
                             f"{rel}: only style=\"display:none;\" may remain")

    def test_every_style_block_carries_the_csp_nonce(self):
        for path in sorted(ROOT.glob(TEMPLATE_GLOB)):
            for tag in re.findall(r"<style\b[^>]*>", path.read_text(encoding="utf-8")):
                self.assertIn('nonce="{{ request.csp_nonce }}"', tag,
                              f"{path.relative_to(ROOT)}: a <style> block without the CSP nonce")
