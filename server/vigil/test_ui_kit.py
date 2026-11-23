"""The /ui-kit/ page (docs/UI-STANDARDS.md): admins only, and it may not break
the rules the CSP and the stylesheet tests exist to hold."""

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile

User = get_user_model()
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "ui_kit.html"
SCRIPTS = [
    ROOT / "static" / "js" / name
    for name in ("vigil-ui-kit.js", "vigil-utils.js", "vigil-modal.js")
]

SECTIONS = [
    "kit-header",
    "kit-colours",
    "kit-buttons",
    "kit-chips",
    "kit-tabs",
    "kit-forms",
    "kit-settings-rows",
    "kit-tables",
    "kit-empty-state",
    "kit-dialogs",
    "kit-toasts",
]


def _user(username: str, role: Role):
    user = User.objects.create_user(username=username, password="pw-for-tests")
    UserProfile.objects.create(user=user, role=role)
    return user


class UiKitAccessTests(TestCase):
    def setUp(self):
        self.admin = _user("kit-admin", Role.ADMIN)
        self.operator = _user("kit-operator", Role.OPERATOR)
        self.viewer = _user("kit-viewer", Role.VIEWER)

    def test_an_admin_sees_every_section(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/ui-kit/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # The CSP allows no inline script anywhere (apps/accounts/csp.py), and
        # this page carries none: every handler is a data-act resolved by the
        # external vigil-ui-kit.js, so the response must be 200 under that policy.
        script = next(d for d in resp.headers["Content-Security-Policy"].split(";")
                      if d.strip().startswith("script-src"))
        self.assertNotIn("'unsafe-inline'", script)
        for section in SECTIONS:
            self.assertIn(f'id="{section}"', body, f"{section} is missing")
        self.assertIn("Every shared component, with the live stylesheets. "
                      "Admins only.", body)
        # One row per size plus the disabled row, and every colour in each.
        kit = body.split('id="kit-buttons">')[1].split("</section>")[0]
        self.assertEqual(kit.count('class="kit-row"'), 4)
        for size_row in ("btn-sm", "btn-xs"):
            self.assertIn(size_row, kit)
        for colour in ("btn-mint", "btn-rose", "btn-sky", "btn-lav",
                       "btn-peach", "btn-lemon", "btn-outline", "btn-ghost"):
            self.assertGreaterEqual(kit.count(colour), 4, f"{colour} is not in every row")
        self.assertIn('<div class="tab-bar" data-tab-group="kit"', body)
        self.assertEqual(body.count('class="toggle"'), 2)
        self.assertIn('aria-checked="true"', body)
        self.assertIn('aria-checked="false"', body)
        for variant in ("kit-rise-modal", "kit-pop-modal", "kit-sheet-modal"):
            self.assertIn(f'id="{variant}"', body, f"{variant} is missing")
        for name in ("vigil-utils.js", "vigil-modal.js", "vigil-nav.js",
                     "vigil-ui-kit.js"):
            self.assertIn(f"js/{name}", body, f"{name} is not loaded")

    def test_an_operator_gets_404(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/ui-kit/").status_code, 404)

    def test_a_viewer_gets_404(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get("/ui-kit/").status_code, 404)

    def test_anonymous_is_sent_to_the_login_page(self):
        resp = self.client.get("/ui-kit/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["location"])


class UiKitTemplateTests(TestCase):
    """The CSP forbids inline scripts, inline styles and on...= handlers, so a
    violation is not a warning — it is a section of the page that stops working."""

    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_no_inline_script_style_or_handler(self):
        # An element with a src attribute is external and allowed by the CSP;
        # anything else named <script> is an inline block, which it refuses.
        self.assertFalse(re.search(r"<script(?![^>]*\ssrc=)", self.text),
                         "a <script> element without src — the CSP will refuse it")
        self.assertNotIn(' style="', self.text)
        self.assertFalse(re.search(r'\son[a-z]+\s*=', self.text),
                         "an inline event handler — the CSP will refuse it")

    def test_every_data_act_names_a_function_the_page_loads(self):
        declared = set()
        for script in SCRIPTS:
            declared.update(re.findall(r"^(?:async )?function ([A-Za-z_$][\w$]*)",
                                       script.read_text(encoding="utf-8"),
                                       flags=re.MULTILINE))
        used = set(re.findall(r'data-(?:act|change)="([^"]+)"', self.text))
        self.assertTrue(used, "the page has no data-act handlers at all")
        missing = sorted(used - declared)
        self.assertEqual(missing, [], f"no function declares these handlers: {missing}")

    def test_the_page_loads_the_design_through_the_include(self):
        self.assertIn('{% include "_stylesheets.html" %}', self.text)
