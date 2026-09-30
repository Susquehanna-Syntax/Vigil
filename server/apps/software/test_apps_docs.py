"""M7 Apps must be findable by an admin and by the AI assistant.

The wiki's Apps section documents the inventory, the Apps page, the six
app_* actions, the allowlist warning and the limits; its worked example is
parsed straight out of the page, so an example that stops validating fails
here instead of misleading a reader. The assistant's rules are checked
verbatim.
"""
import html
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.tasks.spec import parse_and_validate


def _wiki() -> str:
    return (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")


def _section(text: str) -> str:
    start = text.index('<section class="section" id="apps">')
    return text[start:text.index("<section", start + 10)]


def _examples(section: str) -> dict[str, str]:
    """Each <pre> in the section as plain text, keyed by its first line."""
    blocks = [html.unescape(re.sub(r"<[^>]+>", "", b))
              for b in re.findall(r"<pre>(.*?)</pre>", section, re.S)]
    return {b.splitlines()[0]: b + "\n" for b in blocks}


class AppsWikiTests(SimpleTestCase):
    def test_section_and_nav(self):
        text = _wiki()
        nav = text.index('<a href="#apps" class="nav-link">')
        self.assertLess(text.index('href="#relevance-and-branches" class="nav-link"'), nav)
        self.assertLess(text.index('id="relevance-and-branches"'), text.index('id="apps"'))
        section = _section(text)
        for heading in ("What Vigil sees", "Outdated and unmanaged", "The Apps page", "The app actions",
                        "Refusals and the allowlist", "Limits"):
            self.assertIn(f"<h3>{heading}</h3>", section)
        self.assertIn("keep-firefox-current.yaml", section)

    def test_section_covers_apps(self):
        text = _wiki()
        section = _section(text)
        for word in ("app_inventory", "app_install", "app_upgrade", "app_uninstall", "app_pin",
                     "app_install_custom", "unmanaged", "outdated", "LocalSystem", "Send to them anyway"):
            self.assertIn(word, section)
        self.assertIn("software_interval", text)
        self.assertIn('<span class="hl-var">software_interval</span>', text, "agent.yml example")

    def test_example_parses(self):
        spec = parse_and_validate(_examples(_section(_wiki()))["name: Keep Firefox current"])
        self.assertEqual(spec["risk"], "standard")
        steps = {a["id"]: a for a in spec["actions"]}
        self.assertEqual(steps["up"]["type"], "app_upgrade")
        self.assertTrue(steps["up"].get("when"))
        self.assertEqual(steps["up"]["outcome"], "Upgraded")


class AppsAiRulesTests(SimpleTestCase):
    def test_ai_rules(self):
        for line in (
            "- To install, upgrade, pin or remove software use app_install / app_upgrade / app_pin / app_uninstall",
            "- Refresh the inventory with app_inventory (outputs count, outdated, unmanaged) and branch on it.",
            "- Use app_install_custom (high risk, needs url + sha256) only when no package manager has the software.",
        ):
            self.assertIn(line, SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.rstrip().endswith("- Never propose update_agent."))
