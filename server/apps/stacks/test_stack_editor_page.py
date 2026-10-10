"""QA-20: the stack editor is a page of its own, not a modal."""
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


def _text(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


class StackEditorPageTests(SimpleTestCase):
    def test_page_exists_and_is_included(self):
        page = _text("templates/pages/_stack_editor.html")
        for marker in ('id="page-stack-editor"', 'id="stack-compose"', 'id="sted-gutter"',
                       'id="stack-env"', 'id="stack-revisions"'):
            self.assertIn(marker, page)
        self.assertIn("pages/_stack_editor.html", _text("templates/dashboard.html"))

    def test_no_modal_left(self):
        js = _text("static/js/vigil-stacks.js")
        self.assertNotIn("stack-modal", js)
        self.assertNotIn("stack-overlay", js)
        self.assertIn("navigateTo('stack-editor')", js)

    def test_live_validation(self):
        js = _text("static/js/vigil-stacks.js")
        for needle in ("/api/v1/stacks/validate/", "env_keys", "setTimeout"):
            self.assertIn(needle, js)
        # The POST itself, not merely its path: a call that quietly disappears
        # is the regression this test exists to catch.
        source = js[js.index("async function _validateStack"):]
        source = source[:source.index("\nasync function ")]
        for needle in ("apiJson('/api/v1/stacks/validate/'", "method: 'POST'",
                       "compose_yaml: ta.value"):
            self.assertIn(needle, source)

    def test_dirty_guard(self):
        js = _text("static/js/vigil-stacks.js")
        self.assertIn("dirty", js)
        self.assertIn("confirmModal(", js)
        self.assertNotIn("window.confirm(", js)

    def test_env_values_never_in_markup(self):
        js = _text("static/js/vigil-stacks.js")
        source = js[js.index("function _renderStackEnv"):]
        source = source[:source.index("\nfunction ")]
        self.assertIn(".value =", source)
        self.assertNotIn("${e.value}", source)
        self.assertNotIn("${escAttr(e.value)}", source)
