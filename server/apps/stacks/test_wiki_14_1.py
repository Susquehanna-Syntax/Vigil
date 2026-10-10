"""QA-24: 2026.14.1 must be written up in the wiki.

The section placement is checked because a subsection that landed in the wrong
section reads as a promise the feature never made, and the Git subsection's
facts are checked verbatim because they are the security story a reader relies
on ("Only the Vigil server talks to Git").
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

NEW_IDS = {
    "containers-states": "docker",
    "containers-stack-editor": "docker",
    "containers-git-stacks": "docker",
    "containers-sandbox": "docker",
    "parallel-groups": "deployments",
    "security-sessions": "security",
    "security-agents": "security",
    "security-installer": "security",
    "community-validate": "community",
    "api-stacks": "api",
}


def _wiki() -> str:
    return (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")


def _section(text: str, name: str) -> str:
    start = text.index(f'<section class="section" id="{name}">')
    return text[start:text.index("<section", start + 10)]


def _subsection(text: str, anchor_id: str) -> str:
    """The subsection's own markup: its <h3> up to the next heading of any
    level, or (for the last subsection of a section) the section's end."""
    start = text.index(f'<h3 id="{anchor_id}"')
    ends = [e for e in (text.find(next_, start + 5) for next_ in
                        ("<h3", "<h2", "</section>")) if e != -1]
    return text[start:min(ends)] if ends else text[start:]

class Wiki141Tests(SimpleTestCase):
    def test_new_subsections_exist(self):
        text = _wiki()
        for anchor_id in NEW_IDS:
            self.assertEqual(
                text.count(f'id="{anchor_id}"'), 1,
                f'{anchor_id} must appear exactly once',
            )

    def test_subsections_sit_in_their_sections(self):
        text = _wiki()
        for anchor_id, section in NEW_IDS.items():
            pos = text.index(f'<h3 id="{anchor_id}"')
            start = text.rfind('<section class="section" id="', 0, pos)
            found = text[start + len('<section class="section" id="'):].split('"', 1)[0]
            self.assertEqual(found, section, f'{anchor_id} is not in section {section}')

    def test_git_facts(self):
        git = _subsection(_wiki(), "containers-git-stacks")
        for fact in ("Only the Vigil server talks to Git", "SHA-256", "read-only",
                     "ssh-keyscan", "TOTP"):
            self.assertIn(fact, git)

    def test_old_check_sentence_is_gone(self):
        text = _wiki()
        self.assertNotIn("may not climb out with", text)
        self.assertIn('href="#containers-sandbox"', text)

    def test_env_rows(self):
        section = _section(_wiki(), "env")
        for name in ("VIGIL_SESSION_IDLE_MINUTES", "VIGIL_SESSION_MAX_HOURS"):
            self.assertIn(name, section)

    def test_no_inline_style_or_handlers_added(self):
        text = _wiki()
        start = text.index('id="containers-states"')
        end = text.index('<h3 id="containers-registries"', start)
        regions = [text[start:end]] + [_subsection(text, i) for i in
                                       ("security-sessions", "security-agents", "security-installer")]
        for region in regions:
            self.assertNotIn('style="', region)
            self.assertIsNone(re.search(r"\son[a-z]+=\"", region))
