"""M8 must be findable by an admin and by the AI assistant.

The wiki's Policies section is checked for its headings and nav link; its
worked example is real: the JSON body is POSTed to the API and the task it
compiles to must equal the YAML printed beside it, so the page cannot drift
from what Vigil does.
"""
import html
import json
import re
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase, TestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.tasks.spec import parse_and_validate

from .models import UpdatePolicy
from .test_policy_api import admin_client


def _wiki() -> str:
    return (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")


def _section(text: str) -> str:
    start = text.index('<section class="section" id="policies">')
    return text[start:text.index("<section", start + 10)]


def _examples(section: str) -> list[str]:
    return [html.unescape(re.sub(r"<[^>]+>", "", b))
            for b in re.findall(r"<pre>(.*?)</pre>", section, re.S)]


class PoliciesWikiTests(SimpleTestCase):
    def test_section_and_nav(self):
        text = _wiki()
        self.assertLess(text.index('<a href="#apps" class="nav-link">'),
                        text.index('<a href="#policies" class="nav-link">'))
        section = _section(text)
        for heading in ("What a policy holds", "Drift: only hosts that need it",
                        "The Patching tab", "Approve each change", "The generated task",
                        "By update", "Compliance", "Limits"):
            self.assertIn(f">{heading}</h3>", section)
        for word in ("app_ensure", "present", "latest", "pinned", "absent", "deferral",
                     "Licensed to", "Business", "TOTP", "/api/v1/policies/compliance/"):
            self.assertIn(word, section)
        for anchor in re.findall(r'href="#([a-z-]+)"', section):
            self.assertIn(f'id="{anchor}"', text, anchor)


class PoliciesWikiExampleTests(TestCase):
    def test_example_body_compiles_to_the_printed_task(self):
        body, printed = _examples(_section(_wiki()))
        client, _ = admin_client()
        resp = client.post("/api/v1/policies/", json.loads(body), format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        definition = UpdatePolicy.objects.get().task_definition
        self.assertEqual(yaml.safe_load(definition.yaml_source), yaml.safe_load(printed))
        self.assertEqual(parse_and_validate(printed)["risk"], "standard")


class PoliciesAiRulesTests(SimpleTestCase):
    def test_ai_rules(self):
        self.assertIn("- To keep one app in a state use app_ensure with app + state", SYSTEM_PROMPT)
        self.assertIn('Never write or edit a task named "Policy: ..."', SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.rstrip().endswith("- Never propose update_agent."))
