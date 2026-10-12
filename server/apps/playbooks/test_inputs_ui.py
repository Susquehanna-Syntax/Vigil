"""Playbook inputs in the browser: the declaration list, the step mapping, and
the forms that fill values.

The server side (apps/playbooks/inputs.py) is covered by test_playbook_inputs.py.
What this checks is the other half — that the JS and markup the browser runs
actually carry the ids and keys the server reads: the playbook editor sends
``inputs``, ``flowSerialize`` round-trips the step mapping under the same key
the API parses, and the rollout and automation forms send ``inputs`` too.

The markup criteria are read off what Django actually renders (the template
engine's output), not off the file's bytes: the reference text inside the
editor's Inputs note had to be wrapped in ``{% verbatim %}``, and only the
render proves that the literal ``${{ inputs.<id> }}`` still reaches the browser.
"""

import json

from django.contrib.auth import get_user_model
from django.template import engines
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Role, UserProfile
from apps.automations.models import Automation
from apps.playbooks.models import Playbook
from apps.tasks.models import TaskDefinition
from apps.tasks.views import _save_definition_from_yaml

PICKERS = "static/js/vigil-pickers.js"
PLAYBOOKS = "static/js/vigil-playbooks.js"
FLOW = "static/js/vigil-playbook-flow.js"
ROLLOUT = "static/js/vigil-rollout.js"
AUTOMATIONS = "static/js/vigil-automations.js"
PLAYBOOKS_HTML = "pages/_playbooks.html"
BASE = "base.html"
CSS = "static/css/vigil.css"

EDITED_JS = [PICKERS, PLAYBOOKS, FLOW, ROLLOUT, AUTOMATIONS]

DECLARES_DOMAIN = """
name: Says the domain
risk: low
inputs:
  - {id: domain, label: Domain, type: text, required: true}
actions:
  - id: say
    type: restart_service
    params:
      service_name: ${{ inputs.domain }}
"""


def _source(rel: str) -> str:
    """A static file, or a template's own text — for criteria about the source."""
    from django.conf import settings
    root = settings.BASE_DIR
    path = root / rel
    if rel.endswith(".html") and not path.exists():
        path = root / "templates" / rel
    return path.read_text()


def _rendered(rel: str) -> str:
    """What the browser receives for that template."""
    return engines["django"].get_template(rel).render({})


class SharedInputsFormTests(SimpleTestCase):
    """The declared-inputs form both new callers share."""

    def test_the_form_helpers_exist(self):
        code = _source(PICKERS)
        self.assertIn("function inputsFormMount(", code)
        self.assertIn("function inputsFormRead(", code)

    def test_form_rows_carry_the_id_and_type_the_reader_needs(self):
        code = _source(PICKERS)
        for needed in ("inputs-form-row", "inputs-form-label", "inputs-form-control",
                       "dataset.inputId", "dataset.inputType"):
            self.assertIn(needed, code)

    def test_read_returns_values_and_the_first_error(self):
        read = _source(PICKERS)[_source(PICKERS).index("function inputsFormRead("):]
        self.assertIn("return { values, error }", read)
        self.assertIn("is required", read)
        self.assertIn("enter a number", read)


class PlaybookDeclarationTests(SimpleTestCase):
    """The editor's Inputs section: list, add, edit, remove, save."""

    def test_the_field_exists_in_the_markup_the_browser_gets(self):
        html = _rendered(PLAYBOOKS_HTML)
        self.assertIn('id="bl-inputs"', html)
        self.assertIn('id="bl-input-add"', html)
        # The literal reference the note teaches — the template's verbatim
        # block is what keeps Django from eating it.
        self.assertIn("${{ inputs.&lt;id&gt; }}", html)

    def test_the_editor_sends_the_declarations(self):
        self.assertIn("inputs: _blInputs", _source(PLAYBOOKS))

    def test_rows_offer_edit_and_remove(self):
        code = _source(PLAYBOOKS)
        self.assertIn("data-bl-input-edit", code)
        self.assertIn("data-bl-input-rm", code)

    def test_ids_are_checked_against_the_servers_pattern(self):
        # apps/tasks/spec.py: _INPUT_ID_PATTERN is [A-Za-z_][A-Za-z0-9_]*, and
        # a save that violates it is a 400 the user can only read as a toast.
        self.assertIn("[a-zA-Z_][a-zA-Z0-9_]*", _source(PLAYBOOKS))


class PlaybookApiTests(TestCase):
    """The exact body the editor posts — declared inputs plus a step mapping
    written by the inspector — is what the API stores and hands back, keys
    included."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("ui-sender", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)

    def test_the_saved_declaration_and_mapping_round_trip_the_api(self):
        definition = TaskDefinition(owner=self.user)
        _save_definition_from_yaml(definition, DECLARES_DOMAIN)
        definition.save()
        playbook = Playbook.objects.create(name="Round trip", created_by=self.user)
        bad = self.client.patch(
            f"/api/v1/playbooks/{playbook.id}/",
            {"inputs": [{"id": "domain", "type": "text", "required": True}],
             "flow_steps": [{"definition_id": str(definition.id),
                             "inputs": {"domain": "${{ inputs.nope }}"}}]},
            content_type="application/json")
        self.assertEqual(bad.status_code, 400, bad.content[:300])
        playbook.refresh_from_db()
        self.assertEqual(playbook.inputs, [])

        good = self.client.patch(
            f"/api/v1/playbooks/{playbook.id}/",
            {"inputs": [{"id": "domain", "type": "text", "required": True}],
             "flow_steps": [{"definition_id": str(definition.id),
                             "inputs": {"domain": "${{ inputs.domain }}"}}]},
            content_type="application/json")
        self.assertEqual(good.status_code, 200, good.content[:300])
        body = good.json()
        self.assertEqual([i["id"] for i in body["inputs"]], ["domain"])
        self.assertEqual(body["steps"][0]["inputs"], {"domain": "${{ inputs.domain }}"})


class StepMappingTests(SimpleTestCase):
    """The step node carries its mapping under the key the API parses."""

    def test_serialize_sends_the_mapping(self):
        self.assertIn("inputs: n.inp || {}", _source(FLOW))

    def test_tree_from_row_reads_it_back(self):
        self.assertIn("inp: s.inputs || {}", _source(FLOW))

    def test_the_inspector_fields_are_wired_by_id(self):
        code = _source(FLOW)
        self.assertIn('data-flow-inp="', code)
        self.assertIn('data-inp-id="', code)
        self.assertIn("data-flow-inp-ins", code)


class RolloutStartTests(SimpleTestCase):

    def test_the_dialog_has_a_place_for_the_form(self):
        self.assertIn('id="rollout-start-inputs"', _rendered(BASE))

    def test_the_form_is_filled_from_the_target_and_sent(self):
        code = _source(ROLLOUT)
        self.assertIn("rollout-start-inputs", code)
        self.assertIn("inputsFormMount(", code)
        self.assertIn("inputsFormRead(", code)
        self.assertIn("{ inputs: values }", code)


class AutomationTests(SimpleTestCase):

    def test_the_stored_values_are_sent(self):
        self.assertIn("inputs: _autoInputs", _source(AUTOMATIONS))

    def test_the_old_override_editor_stays_reachable_for_tasks(self):
        code = _source(AUTOMATIONS)
        self.assertIn("openInputsModal(", code)
        self.assertIn("Advanced: override action params", code)

    def test_a_playbook_without_inputs_says_so(self):
        self.assertIn("This playbook has no inputs", _source(AUTOMATIONS))


class AutomationSaveTests(TestCase):
    """What the Inputs dialog sends is what the automation stores: values
    checked against the target's declarations, so a mistake is fixed in the
    form and not at 2am when the automation fires."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("ui-auto", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)

    def _post(self, playbook, inputs):
        return self.client.post(
            "/api/v1/automations/",
            {"name": "Nightly", "trigger": "schedule",
             "cron": {"minute": "0", "hour": "2", "dom": "*", "month": "*", "dow": "*"},
             "action_kind": "playbook", "playbook_name": playbook.name,
             "target": "all", "inputs": inputs},
            content_type="application/json")

    def test_the_values_the_dialog_sends_are_the_ones_stored(self):
        playbook = Playbook.objects.create(
            name="Declaring", created_by=self.user,
            inputs=[{"id": "domain", "type": "text", "required": True,
                     "default": "lab"}])
        saved = self._post(playbook, {"domain": "corp"})
        self.assertEqual(saved.status_code, 201, saved.content[:300])
        self.assertEqual(Automation.objects.get(name="Nightly").inputs,
                         {"domain": "corp"})

    def test_a_choice_value_off_the_list_is_refused(self):
        # The dialog offers a choice input's values as a select, so an
        # automation can only carry one of them — which is what the save
        # checks, and what an old free-text field could not promise.
        playbook = Playbook.objects.create(
            name="Choosing", created_by=self.user,
            inputs=[{"id": "window", "type": "choice", "default": "nightly",
                     "choices": [{"value": "nightly", "label": "Nightly"},
                                 {"value": "weekend", "label": "Weekend"}]}])
        refused = self._post(playbook, {"window": "lunchtime"})
        self.assertEqual(refused.status_code, 400, refused.content[:300])
        self.assertIn("window", json.loads(refused.content)["detail"])
        self.assertFalse(Automation.objects.filter(name="Nightly").exists())


class StyleTests(SimpleTestCase):
    def test_the_inputs_block_landed_in_the_stylesheet(self):
        css = _source(CSS)
        self.assertIn("/* ── Playbook inputs (14.4) */", css)
        for selector in (".bl-inputs {", ".inputs-form {", ".inputs-form-row"):
            self.assertIn(selector, css)

    def test_step_input_rows_are_rule_based_not_striped(self):
        rules = "\n".join(
            line for line in _source(CSS).splitlines()
            if line.strip().startswith((".fci-ctl", ".fci-insert"))
        )
        for banned in ("border-left", "border-right"):
            self.assertNotIn(banned, rules)


class CSPTests(SimpleTestCase):
    """script-src has no 'unsafe-inline': a handler written into markup is
    dropped by the browser, so the control would look fine and do nothing."""

    def test_no_inline_handlers_in_the_edited_scripts(self):
        for rel in EDITED_JS:
            self.assertNotIn("onclick=", _source(rel), rel)
            self.assertNotIn("onchange=", _source(rel), rel)
            self.assertNotIn("oninput=", _source(rel), rel)
