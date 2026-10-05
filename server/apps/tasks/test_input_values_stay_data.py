"""An input value is data, never a template.

QA (2026-09-24) typed `${{ inputs.pick }} and ${{ steps.dump.status }}` into a
text input used in a write_file param, and the file on the host read
`alpha and ok`: the server pasted the value into the param and the agent's
templater expanded the markers inside it. The server now writes any `${{`
inside a value as the escaped `$${{`, and substitutes both marker forms in a
single pass so an inserted value is never rescanned.
"""
from django.test import SimpleTestCase

from apps.tasks.spec import parse_and_validate, resolve_inputs

YAML = """
name: Values are data
risk: high
inputs:
  - id: txt
    type: text
  - id: pick
    type: text
actions:
  - id: w
    type: run_command
    params:
      command: "echo ${{ inputs.txt }}"
"""


class InputValuesStayDataTests(SimpleTestCase):
    def _command(self, txt: str) -> str:
        parsed = parse_and_validate(YAML)
        return resolve_inputs(parsed, {"txt": txt, "pick": "alpha"})["actions"][0]["params"]["command"]

    def test_new_marker_in_a_value_is_escaped(self):
        self.assertEqual(self._command("${{ inputs.pick }}"), "echo $${{ inputs.pick }}")

    def test_old_marker_in_a_value_is_not_expanded(self):
        # A single pass: the legacy form inside an inserted value is left alone
        # (and the agent never templates bare {{ }}).
        self.assertEqual(self._command("{{ inputs.pick }}"), "echo {{ inputs.pick }}")

    def test_author_can_write_a_literal_marker(self):
        parsed = parse_and_validate(YAML.replace("echo ${{ inputs.txt }}", "echo $${{ inputs.nope }} ${{ inputs.txt }}"))
        cmd = resolve_inputs(parsed, {"txt": "x", "pick": "p"})["actions"][0]["params"]["command"]
        self.assertEqual(cmd, "echo $${{ inputs.nope }} x")
