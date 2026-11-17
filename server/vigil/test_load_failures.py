"""Phase 07a: a page whose main load fails says so where the content would be.

Text-based checks: each page script's main-load `catch` draws the failure in
place with the shared `showLoadFailure` helper instead of a vanishing toast or
an ad-hoc grey line, and the helper renders text only (never markup).
"""

from django.test import SimpleTestCase

from vigil.css_text import design_css

# The library's "nothing here yet" panels are not load failures; they stay.
_PAGE_SCRIPTS = (
    "vigil-alerts.js",
    "vigil-tasks.js",
    "vigil-firewall.js",
    "vigil-inventory.js",
    "vigil-vulns.js",
    "vigil-rollout.js",
    "vigil-about.js",
)

# The wording each site used to show for a failed load, and the one the about
# page shared with its other panels.
LEGACY_FAILURE_COPY = (
    "Failed to load alerts",
    "Failed to load library",
    "Failed to load firewall state",
    "Failed to load inventory",
    "Failed to load findings",
    "Failed to load rollouts",
    "'Failed to load: '",
)


def _js(name: str) -> str:
    with open(f"static/js/{name}", encoding="utf-8") as fh:
        return fh.read()


class LoadFailureCopyGoneTests(SimpleTestCase):
    def test_no_page_script_still_shows_the_old_failure_copy(self):
        for name in _PAGE_SCRIPTS:
            text = _js(name)
            for stale in LEGACY_FAILURE_COPY:
                self.assertNotIn(stale, text, f"{name} still says {stale!r}")


class SharedHelperTests(SimpleTestCase):
    def test_every_page_draws_its_failure_with_the_helper(self):
        for name in _PAGE_SCRIPTS:
            self.assertIn("showLoadFailure(", _js(name),
                          f"{name} has no in-place load failure")

    def test_helper_renders_text_and_never_markup(self):
        utils = _js("vigil-utils.js")
        start = utils.index("function showLoadFailure(")
        body = utils[start:utils.index("\n}", start)]
        self.assertIn("replaceChildren(block)", body)
        self.assertNotIn("innerHTML", body, "a server's error text must not be markup")

    def test_helper_offers_retry_when_given_one(self):
        utils = _js("vigil-utils.js")
        start = utils.index("function showLoadFailure(")
        body = utils[start:utils.index("\n}", start)]
        self.assertIn("'Try again'", body)


class LoadFailureStyleTests(SimpleTestCase):
    def test_the_block_is_styled_and_wears_no_side_stripe(self):
        css = design_css()
        for selector in (".load-failed", ".load-failed-title", ".load-failed-desc"):
            self.assertIn(selector, css, f"{selector} missing from the stylesheets")
        start = css.index(".load-failed {")
        block = css[start:css.index("}", css.index(".load-failed-desc", start))]
        self.assertNotIn("border-left", block, ".load-failed must not grow a side stripe")
        self.assertNotIn("border-right", block, ".load-failed must not grow a side stripe")
