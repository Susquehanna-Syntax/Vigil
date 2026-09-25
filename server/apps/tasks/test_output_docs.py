"""Authors can see what a step hands to the next one without reading code.

Every declared output is documented (the wiki render refuses otherwise), the
wiki lists each action's outputs, the editor preview shows them per step, and
the AI prompt knows the steps.<id>.result.<field> form.
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.tasks.management.commands.render_wiki_actions import OUTPUT_NOTES
from apps.tasks.spec import ACTION_REGISTRY, action_outputs, parse_and_validate

REPO = Path(settings.BASE_DIR).parent


class OutputDocsTests(SimpleTestCase):
    def test_every_declared_output_has_a_note(self):
        missing = [f"{a}.{f}" for a in ACTION_REGISTRY for f in action_outputs(a)
                   if f not in OUTPUT_NOTES.get(a, {})]
        self.assertEqual(missing, [])

    def test_wiki_lists_outputs(self):
        wiki = (REPO / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")
        self.assertTrue("<th>Output</th>" in wiki, "outputs table missing")
        self.assertTrue("steps.&lt;id&gt;.result" in wiki, "step-ref hint missing")

    def test_parsed_actions_list_their_outputs(self):
        spec = parse_and_validate(
            "name: t\nrisk: low\nactions:\n"
            "  - id: a\n    type: check_service\n    params:\n      service_name: nginx\n"
            "  - id: b\n    type: restart_service\n    params:\n      service_name: nginx\n")
        self.assertEqual(spec["actions"][0]["outputs"], ["active", "state"])
        self.assertEqual(spec["actions"][1]["outputs"], [])

    def test_editor_shows_outputs_escaped(self):
        src = (Path(settings.BASE_DIR) / "static" / "js" / "vigil-tasks.js").read_text(encoding="utf-8")
        self.assertTrue("preview-step-outputs" in src, "editor outputs line missing")
        self.assertTrue("escHtml(o)" in src, "outputs must be escaped")

    def test_ai_prompt_teaches_step_refs(self):
        self.assertTrue("steps.<id>.result.<field>" in SYSTEM_PROMPT, "AI prompt must teach step refs")
