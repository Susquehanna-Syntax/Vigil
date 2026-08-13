"""The wiki's action table must name the actions that actually exist.

It had drifted into fiction: `get_status`, `ping`, `run_script` and
`shutdown` were all documented and none of them are real, and
`restart_service` was given a `service` param when it takes `service_name`.
A definition written from that page fails validation the moment it is saved,
which is the worst place to learn the reference was wrong.

The table is generated from ACTION_REGISTRY. This checks it stayed that way.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from .spec import ACTION_REGISTRY


def _wiki() -> str:
    path = Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _action_table(text: str) -> str:
    """Just the Built-in Actions table — other sections list actions too."""
    start = text.index("<h3>Built-in Actions</h3>")
    return text[start:text.index("Conditional execution", start)]


class WikiActionTableTests(TestCase):
    def setUp(self):
        self.text = _wiki()
        if not self.text:
            self.skipTest("wiki not present in this build")

    def test_the_table_lists_exactly_the_registered_actions(self):
        listed = set(re.findall(r"<tr><td><code>(\w+)</code></td>",
                                _action_table(self.text)))
        self.assertEqual(
            sorted(listed), sorted(ACTION_REGISTRY),
            "the wiki's action table no longer matches ACTION_REGISTRY — "
            "regenerate it rather than editing rows by hand")

    def test_no_invented_action_comes_back(self):
        """Named individually: these four were documented for months."""
        table = _action_table(self.text)
        for ghost in ("get_status", "ping", "run_script", "shutdown"):
            self.assertNotIn(
                f"<td><code>{ghost}</code></td>", table,
                f"{ghost!r} is documented but does not exist")

    def test_required_params_match_the_registry(self):
        """The subtler half of the same defect: a real action with a param
        name that was never right."""
        table = _action_table(self.text)
        rows = re.findall(
            r"<tr><td><code>(\w+)</code></td>.*?</span></td><td>(.*?)</td>",
            table)
        self.assertTrue(rows, "parsed no rows — has the table markup changed?")
        for action, params_cell in rows:
            expected = ACTION_REGISTRY[action]["required"]
            found = re.findall(r"<code>([\w.]+)</code>", params_cell)
            self.assertEqual(
                found, expected,
                f"{action}: wiki says required={found}, registry says {expected}")

    def test_the_wiki_version_badge_matches_this_build(self):
        badge = re.search(r'sidebar-version">v([\d.]+)', self.text)
        self.assertIsNotNone(badge, "version badge missing from the wiki")
        self.assertEqual(badge.group(1), settings.VIGIL_VERSION)
