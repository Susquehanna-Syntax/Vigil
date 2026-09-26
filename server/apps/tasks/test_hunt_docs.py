"""Hunts must be findable by an admin who has never heard of them.

Phase 08 ships three surfaces: the wiki's Hunts section (prose plus a
table of every hunt action), the AI assistant's rules for reaching for a
hunt_*, and the three ready-made hunt templates in ``builtin_templates/``.
These tests pin all three: the wiki table is sliced out of the hand-written
section so the generated action reference cannot satisfy it, the prompt
rules are checked verbatim, and each template must parse and seed as an
owner-less community task.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import parse_and_validate


def _wiki() -> str:
    path = Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _hunts_section(wiki_text: str) -> str:
    """The hand-written Hunts section, up to the next section."""
    start = wiki_text.index('id="hunts"')
    end = wiki_text.index('<section', start)
    return wiki_text[start:end]


def _builtin_dir() -> Path:
    return Path(settings.BASE_DIR) / "apps" / "tasks" / "builtin_templates"


class HuntWikiTests(SimpleTestCase):
    def test_wiki_has_hunts_section_and_nav(self):
        text = _wiki()
        self.assertIn('<section class="section" id="hunts">', text)
        self.assertIn('href="#hunts"', text)

    def test_wiki_hunt_table_lists_every_hunt(self):
        section = _hunts_section(_wiki())
        hunts = sorted(
            name for name in ACTION_REGISTRY if name.startswith("hunt_")
        )
        self.assertTrue(hunts)
        for action in hunts:
            self.assertIn(
                f"<code>{action}</code>", section,
                f"{action} is missing from the wiki Hunts section",
            )


class HuntAiPromptTests(SimpleTestCase):
    def test_ai_prompt_prefers_hunts(self):
        self.assertIn("use a hunt_* action rather than run_command or execute_script", SYSTEM_PROMPT)
        self.assertIn("Never set return: text on hunt_content", SYSTEM_PROMPT)
        self.assertLess(
            SYSTEM_PROMPT.index("use a hunt_* action"),
            SYSTEM_PROMPT.index("Prefer low-risk, reversible diagnostics"),
        )


class HuntTemplateTests(SimpleTestCase):
    def test_templates_parse_and_are_low_risk(self):
        paths = sorted(_builtin_dir().glob("hunt-*.yaml"))
        self.assertEqual(len(paths), 3)
        for path in paths:
            spec = parse_and_validate(path.read_text())
            self.assertEqual(spec["risk"], "low", f"{path.name} is not low risk")
            self.assertEqual(spec["author"], "Vigil", f"{path.name} missing author")
            self.assertIn("hunt_", " ".join(a["type"] for a in spec["actions"]))


class HuntTemplateSeedTests(TestCase):
    def test_templates_are_seeded(self):
        from apps.tasks.models import TaskDefinition

        for name in [
            "Find vulnerable log4j jars",
            "Find hosts listening on a port",
            "Find hosts with a package below a version",
        ]:
            rows = TaskDefinition.objects.filter(
                name=name, owner=None, visibility="community"
            )
            self.assertEqual(
                rows.count(), 1, f"{name} is not seeded as an owner-less community task"
            )
