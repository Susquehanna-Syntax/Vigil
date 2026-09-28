"""M6 must be findable by an admin and by the AI assistant.

The wiki's Relevance and branches section documents relevant:, task
if/then/else, use:, outcome: and playbook branches; its two worked examples
are parsed straight out of the page, so an example that stops validating
fails here instead of misleading a reader. The assistant's rules are checked
verbatim.
"""
import html
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.playbooks.community_yaml import parse as parse_playbook
from apps.tasks.spec import parse_and_validate


def _wiki() -> str:
    return (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")


def _section(text: str) -> str:
    start = text.index('<section class="section" id="relevance-and-branches">')
    return text[start:text.index("<section", start + 10)]


def _examples(section: str) -> dict[str, str]:
    """Each <pre> in the section as plain text, keyed by its first line."""
    blocks = [html.unescape(re.sub(r"<[^>]+>", "", b))
              for b in re.findall(r"<pre>(.*?)</pre>", section, re.S)]
    return {b.splitlines()[0]: b + "\n" for b in blocks}


class M6WikiTests(SimpleTestCase):
    def test_section_and_nav(self):
        text = _wiki()
        nav = text.index('href="#relevance-and-branches" class="nav-link"')
        self.assertGreater(nav, text.index('href="#hunts" class="nav-link"'))
        self.assertGreater(text.index('id="relevance-and-branches"'), text.index('id="hunts"'))

    def test_section_covers_the_language(self):
        section = _section(_wiki())
        for needle in ("relevant:", "all:", "any:", "not:", "then:", "else:", "use:", "outcome:",
                       "on_not_applicable", "on_failure", "color:", "not applicable", "handled",
                       'id: "yes"'):
            self.assertIn(needle, html.unescape(section), needle)

    def test_examples_parse(self):
        examples = _examples(_section(_wiki()))
        task = parse_and_validate(examples["name: Keep nginx running"])
        branch = next(n for n in task["flow"] if "if" in n)
        self.assertEqual(branch["else"], [{"use": "Collect nginx status"}])
        self.assertEqual({a["id"]: a.get("outcome") for a in task["actions"]}["restart"], "Restarted")
        self.assertIsNotNone(task["relevant"])

        playbook = parse_playbook(examples["name: Keep the web tier healthy"])
        steps = {s["id"]: s for s in playbook["steps"]}
        self.assertEqual(list(steps), ["detect", "ensure", "recover", "nothing", "baseline"])
        self.assertEqual(steps["ensure"]["on_failure"], "continue")
        self.assertEqual(steps["detect"]["on_not_applicable"], "skip")
        self.assertEqual(steps["recover"]["outcome"], "Needed recovery")

    def test_expression_list_mentions_ordering(self):
        text = _wiki()
        start = text.index("Expression language")
        listing = text[start:text.index("</ul>", start)]
        self.assertIn("&lt;=", listing)
        self.assertIn("numbers", listing)

    def test_playbooks_link_to_the_section(self):
        text = _wiki()
        deployments = text[text.index('id="deployments"'):text.index("<h3>The completion tag</h3>")]
        self.assertIn('href="#relevance-and-branches"', deployments)


class M6PromptTests(SimpleTestCase):
    def test_ai_rules(self):
        for line in (
            "- To make a task apply only to some hosts, add a top-level relevant: tree",
            "- To choose between steps, use an actions item with if: <expression>, then: [steps], else: [steps];",
            "- <, <=, >, >= compare numbers only.",
            "- A step may carry outcome: <short label>",
            "- Write if: and when: conditions bare, never inside ${{ }}; booleans are True and False.",
            "every hunt_* step outputs matched, count and truncated;",
        ):
            self.assertIn(line, SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.rstrip().endswith("- Never propose update_agent."))
