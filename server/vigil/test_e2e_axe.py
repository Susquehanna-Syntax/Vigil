"""axe-core runs on every sidebar page in both themes, against a baseline (sprint M6 phase 5a).

The suite fails on any finding that is not in `scripts/e2e/axe-baseline.json`, so
the accessibility debt can only shrink. These tests read the three checked-in
artifacts — the browser suite, the workflow, and the baseline — and prove they
agree with each other: the same pinned axe-core hash in two places, WCAG 2 A/AA
rule tags, the injection idiom that survives the page's CSP, and a baseline whose
keys are shaped the way `axe_pages` writes them.
"""
import json
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR).parent
RUN_PY = ROOT / "scripts" / "e2e" / "run.py"
WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"
BASELINE = ROOT / "scripts" / "e2e" / "axe-baseline.json"
AXE_SHA256 = "b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3"


def baseline_is_well_formed(keys: list[str]) -> str | None:
    """Return why `keys` is not a usable baseline, or None when it is."""
    if keys != sorted(keys):
        return "the baseline must be sorted so its diffs are readable"
    if len(set(keys)) != len(keys):
        return "the baseline has duplicate finding keys"
    for key in keys:
        parts = key.split()
        if len(parts) < 4:
            return f"{key!r}: expected '<page> <theme> <rule id> <target>'"
        if parts[1] not in ("dark", "light"):
            return f"{key!r}: the theme is neither dark nor light"
    return None


class AxeSuiteTests(SimpleTestCase):
    def test_run_py_pins_axe_and_its_wcag_tags(self):
        source = RUN_PY.read_text()
        self.assertIn(AXE_SHA256, source)
        for tag in ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa"):
            self.assertIn(f'"{tag}"', source)

    def test_axe_pages_runs_after_every_page(self):
        main = RUN_PY.read_text().split("def main()")[1]
        self.assertLess(main.index("every_page(admin)"), main.index("axe_pages(admin)"))

    def test_axe_is_injected_with_evaluate_not_a_script_tag(self):
        # a page-level script tag is rejected by the content Security-Policy
        # headers; page.evaluate runs outside them.
        self.assertNotIn("add_script_tag", RUN_PY.read_text())


class AxeWorkflowTests(SimpleTestCase):
    def test_the_e2e_job_fetches_axe_before_the_browser_suite(self):
        steps = re.findall(r"(?m)^\s+- name: (.+?)\s*$", WORKFLOW.read_text())
        self.assertLess(steps.index("Fetch axe-core"), steps.index("Run the browser suite"))

    def test_the_fetch_step_checks_the_pinned_hash(self):
        fetch = WORKFLOW.read_text().split("Fetch axe-core")[1].split("Run the browser suite")[0]
        self.assertIn(AXE_SHA256, fetch)
        self.assertIn("sha256sum -c", fetch)


class AxeBaselineTests(SimpleTestCase):
    def test_the_committed_baseline_is_well_formed(self):
        keys = json.loads(BASELINE.read_text())
        self.assertIsInstance(keys, list)
        self.assertTrue(all(isinstance(k, str) for k in keys))
        self.assertIsNone(baseline_is_well_formed(keys))

    def test_a_sample_of_plausible_keys_passes_the_rule(self):
        self.assertIsNone(baseline_is_well_formed([
            "audit dark color-contrast #main h1",
            "events light button-name button[aria-label='Refresh']",
            "users dark link-name a[href='#']",
        ]))

    def test_keys_that_are_not_sorted_or_have_no_target_are_rejected(self):
        self.assertEqual(
            baseline_is_well_formed(["users dark link-name a", "audit dark link-name a"]),
            "the baseline must be sorted so its diffs are readable")
        self.assertEqual(
            baseline_is_well_formed(["events dark color-contrast"]),
            "'events dark color-contrast': expected '<page> <theme> <rule id> <target>'")

    def test_the_theme_part_must_be_dark_or_light(self):
        self.assertEqual(
            baseline_is_well_formed(["events sepia color-contrast h1"]),
            "'events sepia color-contrast h1': the theme is neither dark nor light")
