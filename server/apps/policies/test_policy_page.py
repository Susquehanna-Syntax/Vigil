"""The Policies page wiring, pinned by source scan (no JS runner)."""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class PolicyPageWiringTests(SimpleTestCase):
    def test_page_is_reachable_and_wired(self):
        base = (ROOT / "templates/base.html").read_text(encoding="utf-8")
        dash = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
        html = (ROOT / "templates/pages/_policies.html").read_text(encoding="utf-8")
        js = (ROOT / "static/js/vigil-policies.js").read_text(encoding="utf-8")
        self.assertIn('data-page="policies"', base)
        self.assertIn("js/vigil-policies.js", base)
        self.assertIn('{% include "pages/_policies.html" %}', dash)
        self.assertIn('id="page-policies"', html)
        for element_id in ("pol-table-body", "pol-changes-list", "pol-name", "pol-rules",
                           "pol-classes", "pol-preview", "pol-run-totp", "pol-high-risk"):
            self.assertIn(f'id="{element_id}"', html)
            self.assertIn(f"'{element_id}'", js)
        for act in ("openPolicyEditor", "closePolicyEditor", "savePolicy", "deletePolicy",
                    "addPolicyRule", "runPolicyNow"):
            self.assertIn(f'data-act="{act}"', html)
            self.assertIn(f"function {act}(", js)
        for call in ("/api/v1/policies/", "/drift/", "/run/", "/api/v1/policies/changes/",
                     "/api/v1/software/apps/"):
            self.assertIn(call, js)

    def test_no_side_stripes(self):
        css = (ROOT / "static/css/vigil.css").read_text(encoding="utf-8")
        start = css.index("/* ── Policies (M8)")
        block = css[start:css.index("/* ── Deploy: hosts", start)]
        # A reset to 0 is fine; any real width on one side is a stripe.
        self.assertIsNone(re.search(r"border-(left|right)\s*:(?!\s*0[;\s])", block))
        self.assertNotIn("box-shadow: inset", block)
